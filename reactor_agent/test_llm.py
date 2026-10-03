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

    def test_a_transient_401_is_retried_not_treated_as_a_bad_credential(self):
        """Under load the endpoint returns UNAUTHORIZED and then recovers."""
        c, t = client([
            (401, '{"code":"UNAUTHORIZED"}'),
            (200, reply('{"a": "recovered"}')),
        ], attempts=3)
        self.assertEqual(c.complete('s', 'u', schema=SCHEMA), {'a': 'recovered'})
        self.assertEqual(len(t.requests), 2)

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
