"""Tests for the model client: fallback chain, throttling, and error reporting.

Everything runs against an injected transport, so these tests are offline, fast and
deterministic. That matters because the behaviours under test - a rate limit, a
truncated answer, a fenced JSON reply - are exactly the ones that are awkward to
produce on demand against a live endpoint.
"""
from __future__ import annotations

import json
import unittest

from reactor_agent.llm import (
    RATE_LIMIT_BACKOFF_SECONDS,
    ChatClient,
    LlmConfig,
    LlmError,
    SlidingWindow,
    describe_config,
    strip_code_fence,
)

SCHEMA = {'type': 'object',
          'properties': {'a': {'type': 'string'}},
          'required': ['a'], 'additionalProperties': False}


def reply(content: str, finish: str = 'stop', reasoning: int = 0) -> str:
    """Build a chat-completion body the way the endpoint does."""
    return json.dumps({
        'choices': [{'finish_reason': finish,
                     'message': {'role': 'assistant', 'content': content}}],
        'usage': {'completion_tokens_details': {'reasoning_tokens': reasoning}}})


class FakeTransport:
    """Returns queued (status, body) pairs and records the payloads it received."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, payload, headers, timeout):
        self.requests.append({'url': url, 'payload': payload, 'headers': headers})
        if not self.responses:
            raise AssertionError('transport called more often than expected')
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(responses, **config_kwargs):
    config = LlmConfig(base='https://example.test/v1', key='sk-test-' + 'x' * 30,
                       min_interval=0, **config_kwargs)
    transport = FakeTransport(responses)
    # A sleeper that does nothing keeps the suite instant while still exercising the
    # retry paths.
    return ChatClient(config, transport=transport, sleeper=lambda _s: None), transport


class FallbackChain(unittest.TestCase):

    def test_schema_call_succeeds_directly(self):
        c, t = client([(200, reply('{"a": "ok"}'))])
        self.assertEqual(c.complete('s', 'u', schema=SCHEMA), {'a': 'ok'})
        self.assertEqual(len(t.requests), 1)
        self.assertIn('response_format', t.requests[0]['payload'])

    def test_falls_back_to_plain_text_when_schema_call_yields_nothing(self):
        """The chain GWOA uses: structured output fails, then plain + JSON extraction."""
        c, t = client([
            (200, reply('', finish='length', reasoning=3000)),   # empty -> fallback
            (200, reply('```json\n{"a": "from-plain"}\n```')),   # fenced JSON
        ])
        self.assertEqual(c.complete('s', 'u', schema=SCHEMA), {'a': 'from-plain'})
        self.assertEqual(len(t.requests), 2)
        self.assertNotIn('response_format', t.requests[1]['payload'])

    def test_falls_back_when_the_schema_reply_is_not_json(self):
        c, t = client([
            (200, reply('sorry, I cannot')),
            (200, reply('Sure! {"a": "recovered"}')),
        ])
        self.assertEqual(c.complete('s', 'u', schema=SCHEMA), {'a': 'recovered'})

    def test_raises_when_both_paths_fail(self):
        c, _ = client([(200, reply('nope')), (200, reply('still nope'))])
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(ctx.exception.kind, 'bad_json')

    def test_json_is_reported_as_a_structured_dict(self):
        c, _ = client([(200, reply('nope')), (200, reply('nope'))])
        try:
            c.complete('s', 'u', schema=SCHEMA)
        except LlmError as exc:
            payload = exc.to_dict()
            self.assertIn('kind', payload)
            self.assertIn('error', payload)


class RetryPolicy(unittest.TestCase):
    """What is worth retrying, and how long to wait.

    All of this was measured against a real gateway on 2026-10-03: a burst of retries
    after the first 429 kept the limiter tripped, and the 401 that followed (a spent
    credential) was retried because 401 used to be in the retryable list - adding
    requests to an endpoint that had already refused us.
    """

    def test_a_401_is_not_retried(self):
        c, t = client([(401, '{"error": "unauthorized"}')])
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u')
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(t.requests), 1, 'one request, no retries')

    def test_the_fallback_chain_does_not_swallow_a_401(self):
        """No plain-text second attempt either: the credential is the problem."""
        c, t = client([(401, '{}')])
        with self.assertRaises(LlmError):
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(len(t.requests), 1)

    def test_a_429_waits_the_long_backoff_not_the_short_one(self):
        slept: list[float] = []
        config = LlmConfig(base='https://example.test/v1', key='sk-test-' + 'x' * 30,
                           min_interval=0, attempts=2)
        transport = FakeTransport([(429, '{"code":"RATE_LIMITED"}'),
                                   (200, reply('{"a": "ok"}'))])
        c = ChatClient(config, transport=transport, sleeper=slept.append)
        self.assertEqual(c.complete('s', 'u'), {'a': 'ok'})
        self.assertIn(RATE_LIMIT_BACKOFF_SECONDS, slept)
        self.assertNotIn(2.0, slept, 'the transient backoff must not be used for 429')

    def test_a_503_still_uses_the_short_backoff(self):
        slept: list[float] = []
        config = LlmConfig(base='https://example.test/v1', key='sk-test-' + 'x' * 30,
                           min_interval=0, attempts=2)
        transport = FakeTransport([(503, 'busy'), (200, reply('{"a": "ok"}'))])
        c = ChatClient(config, transport=transport, sleeper=slept.append)
        self.assertEqual(c.complete('s', 'u'), {'a': 'ok'})
        self.assertEqual(slept, [2.0])

    def test_the_request_budget_stops_a_runaway_retry(self):
        """The ceiling is global for the client, and a spent budget stops the chain.

        Before this, an exhausted budget still fell through to the plain-text path,
        which sent one more request - to an endpoint that had already been asked as
        often as it was allowed to be.
        """
        c, t = client([(503, 'busy')] * 20, attempts=3)
        c.max_requests = 3
        for _ in range(2):
            with self.assertRaises(LlmError) as ctx:
                c.complete('s', 'u', schema=SCHEMA)
            self.assertEqual(ctx.exception.kind, 'budget')
        self.assertEqual(len(t.requests), 3, 'exactly the budget, and no fallback extra')
        self.assertTrue(c.budget_exhausted)

    def test_one_call_never_exceeds_the_budget(self):
        c, t = client([(503, 'busy')] * 10, attempts=5)
        c.max_requests = 2
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u')
        self.assertEqual(ctx.exception.kind, 'budget')
        self.assertLessEqual(len(t.requests), 2)

    def test_the_budget_error_is_terminal(self):
        """It must not be swallowed into a fallback attempt."""
        c, t = client([(503, 'busy')] * 10, attempts=3)
        c.max_requests = 1
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(ctx.exception.kind, 'budget')
        self.assertEqual(len(t.requests), 1)


class ThinkingDisabled(unittest.TestCase):

    def test_thinking_is_disabled_by_default(self):
        """Measured: leaving it on cost 7x the time and 20 points of accuracy."""
        c, t = client([(200, reply('{"a": "ok"}'))])
        c.complete('s', 'u', schema=SCHEMA)
        self.assertIs(t.requests[0]['payload']['enable_thinking'], False)

    def test_it_can_be_enabled_explicitly(self):
        c, t = client([(200, reply('{"a": "ok"}'))], enable_thinking=True)
        c.complete('s', 'u', schema=SCHEMA)
        self.assertNotIn('enable_thinking', t.requests[0]['payload'])


class RateLimitAndRetry(unittest.TestCase):

    def test_a_rate_limit_is_retried(self):
        c, t = client([
            (429, '{"code":"RATE_LIMITED"}'),
            (200, reply('{"a": "after retry"}')),
        ], attempts=3)
        self.assertEqual(c.complete('s', 'u', schema=SCHEMA), {'a': 'after retry'})
        self.assertEqual(len(t.requests), 2)

    def test_a_401_is_treated_as_a_credential_problem_not_as_load(self):
        """401 used to be retried, on the theory that the endpoint returns it under
        load and then recovers.

        That theory was measured on 2026-10-03 and does not hold: once the gateway had
        throttled a burst it answered `401 UNAUTHORIZED 未认证或登录已过期` for every
        subsequent request, and the retries only added requests to an endpoint that was
        already refusing us. A rejected credential is now terminal.
        """
        c, t = client([
            (401, '{"code":"UNAUTHORIZED"}'),
            (200, reply('{"a": "should never be reached"}')),
        ], attempts=3)
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(ctx.exception.kind, 'auth')
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(t.requests), 1, 'one request, then stop')

    def test_a_persistent_http_error_surfaces_its_status(self):
        # The fallback path makes its own attempts, so allow two full rounds.
        c, _ = client([(503, 'unavailable')] * 6, attempts=3)
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.attempts, 3)

    def test_a_non_retryable_status_fails_at_once(self):
        c, t = client([(400, '{"code":"MODEL_NOT_AVAILABLE"}')] * 2, attempts=3)
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(ctx.exception.status, 400)
        # Non-retryable: one request per path, never three.
        self.assertEqual(len(t.requests), 2)

    def test_an_endpoint_failure_is_not_reported_as_bad_json(self):
        """A 503 followed by a parse error must still say 503.

        Without this, an outage reads as "the model returned bad JSON" and sends the
        operator looking in the wrong place.
        """
        c, _ = client([(503, 'down')] * 6, attempts=3)
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(ctx.exception.kind, 'http')
        self.assertIn('503', str(ctx.exception))
        self.assertNotEqual(ctx.exception.kind, 'bad_json')

    def test_both_paths_are_named_in_the_detail(self):
        c, _ = client([(503, 'down')] * 6, attempts=3)
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u', schema=SCHEMA)
        self.assertIn('also tried', ctx.exception.detail)


class EmptyContentIsExplained(unittest.TestCase):

    def test_an_empty_reply_points_at_reasoning_tokens(self):
        """The failure that made the reformer look like a model limitation."""
        c, _ = client([
            (200, reply('', finish='length', reasoning=3000)),
            (200, reply('', finish='length', reasoning=3000)),
        ])
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(ctx.exception.kind, 'empty')
        self.assertIn('reasoning', str(ctx.exception))
        self.assertIn('enable_thinking=False', str(ctx.exception))


class CredentialHandling(unittest.TestCase):

    def test_a_missing_key_fails_fast_without_calling_out(self):
        config = LlmConfig(base='https://example.test/v1', key='', min_interval=0)
        transport = FakeTransport([])
        c = ChatClient(config, transport=transport, sleeper=lambda _s: None)
        with self.assertRaises(LlmError) as ctx:
            c.complete('s', 'u')
        self.assertEqual(ctx.exception.kind, 'auth')
        self.assertEqual(transport.requests, [])

    def test_a_placeholder_key_is_rejected(self):
        config = LlmConfig(base='https://example.test/v1', key='sk-xxxx', min_interval=0)
        c = ChatClient(config, transport=FakeTransport([]), sleeper=lambda _s: None)
        with self.assertRaises(LlmError):
            c.complete('s', 'u')

    def test_the_key_is_sent_but_never_described(self):
        c, t = client([(200, reply('{"a": "ok"}'))])
        c.complete('s', 'u', schema=SCHEMA)
        self.assertTrue(t.requests[0]['headers']['Authorization'].startswith('Bearer '))
        described = json.dumps(describe_config(c.config))
        self.assertNotIn(c.config.key, described)
        self.assertIn('"key_present": true', described)

    def test_config_comes_from_the_environment(self):
        env = {'TR_BASE': 'https://elsewhere/v1', 'TR_KEY': 'sk-' + 'y' * 30,
               'TR_MODEL': 'some-model', 'TR_GAP': '1.5'}
        config = LlmConfig.from_env(env)
        self.assertEqual(config.base, 'https://elsewhere/v1')
        self.assertEqual(config.model, 'some-model')
        self.assertEqual(config.min_interval, 1.5)


class Throttling(unittest.TestCase):

    def test_the_window_sleeps_once_the_budget_is_spent(self):
        now = [0.0]
        slept: list[float] = []

        def clock():
            return now[0]

        def sleeper(seconds):
            slept.append(seconds)
            now[0] += seconds

        window = SlidingWindow(max_calls=1, window_seconds=2.0, clock=clock,
                               sleeper=sleeper)
        self.assertEqual(window.acquire(), 0.0)      # free
        self.assertGreater(window.acquire(), 0.0)    # had to wait
        self.assertTrue(slept)

    def test_calls_are_spaced_by_the_configured_interval(self):
        """A burst is what triggers 429, so the client must space calls itself."""
        times: list[float] = []
        now = [0.0]

        def sleeper(seconds):
            now[0] += seconds

        def transport(url, payload, headers, timeout):
            times.append(now[0])
            return 200, reply('{"a": "ok"}')

        config = LlmConfig(base='https://example.test/v1', key='sk-' + 'z' * 30,
                           min_interval=2.5)
        c = ChatClient(config, transport=transport, sleeper=sleeper)
        for _ in range(3):
            c.complete('s', 'u', schema=SCHEMA)
        self.assertEqual(len(times), 3)
        self.assertGreaterEqual(times[1] - times[0], 2.5)
        self.assertGreaterEqual(times[2] - times[1], 2.5)


class JsonExtraction(unittest.TestCase):

    def test_fenced_json_is_unwrapped(self):
        self.assertEqual(strip_code_fence('```json\n{"a": 1}\n```'), '{"a": 1}')

    def test_prose_around_the_object_is_discarded(self):
        self.assertEqual(strip_code_fence('Here you go: {"a": 1} hope that helps'),
                         '{"a": 1}')

    def test_plain_json_is_untouched(self):
        self.assertEqual(strip_code_fence('{"a": 1}'), '{"a": 1}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
