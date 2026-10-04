"""Model client: one OpenAI-compatible endpoint, with the failure handling this
project actually measured rather than the failure handling one might assume.

Three things here are the result of real experiments (see TECH_STACK.md 4.3):

  1. **Thinking is disabled.** `qwen3.7-flash` is a reasoning model. Left on, it spent
     the whole token budget on `reasoning_tokens` and returned an empty or truncated
     `content` - 28s and 60% on the reformer. With it off: 3.6s and 80%. Reasoning
     hurt both latency and accuracy, because a truncated answer is no answer.

  2. **Bursts are throttled locally.** The endpoint returns `429 RATE_LIMITED` for
     concurrent bursts, and 24 back-to-back requests failed outright while a single
     call succeeded. So calls are serialised through a sliding window instead of
     hoping the caller remembers to sleep. A transient `401` also appears under load
     and clears itself, so it is retried, not treated as a bad credential.

  3. **There is a fallback chain, not a single attempt.** When schema-constrained
     output fails, the client retries as plain text and pulls the JSON out of
     whatever wrapper the model produced (models like to fence it in ```json).

Credentials come from the environment and never touch disk, logs or checkpoints.

This module imports nothing outside the standard library, and its HTTP transport is
injectable, so the whole fallback chain can be tested without a network.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_BASE = 'https://tokenrhythm.studio/v1'
DEFAULT_MODEL = 'qwen3.7-flash'

# Measured: 2.5s between calls kept a long run under the limit.
DEFAULT_MIN_INTERVAL = 2.5
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_TOKENS = 4000
DEFAULT_ATTEMPTS = 3

# Requests one client may send, across every retry and every repair round. Four covers
# the legitimate shape of a run: one request normally, plus a repair or a gap retry.
# Anything beyond that is a runaway, and a runaway is what turned a throttled endpoint
# into one that refused the credential.
DEFAULT_MAX_REQUESTS = 4

# The `response_format` strategies. `json_schema` is the strict OpenAI extension;
# `json_object` is the portable subset; `none` sends no constraint at all.
RESPONSE_FORMAT_JSON_SCHEMA = 'json_schema'
RESPONSE_FORMAT_JSON_OBJECT = 'json_object'
RESPONSE_FORMAT_NONE = 'none'
RESPONSE_FORMATS = (RESPONSE_FORMAT_JSON_SCHEMA, RESPONSE_FORMAT_JSON_OBJECT,
                    RESPONSE_FORMAT_NONE)

# Statuses that mean "your request shape is wrong", as opposed to "the endpoint is
# busy". A 400 during a schema-constrained attempt is what an endpoint that does not
# implement `response_format` answers, and retrying it unchanged is pointless.
_REQUEST_SHAPE_STATUSES = (400, 422)

# Statuses worth another attempt. 429 is here because the endpoint does recover, but
# it is handled with a much longer wait than the transient 5xx family: retrying a rate
# limit after two seconds is what turned one throttled intake into a burst.
#
# 401 is deliberately NOT here. It used to be, on the theory that the endpoint returns
# UNAUTHORIZED under load and then recovers. That theory was measured against a gateway
# where a spent or revoked credential also answers 401, and the retries only added
# requests to an endpoint that was already refusing us. An authentication failure is
# now final: `require_key`/`_send` report it and the caller stops.
RETRYABLE_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)

# Seconds to wait before retrying a 429 when the endpoint sends no Retry-After header
# (this client's transport returns only status and body, so the header is not visible
# to it). Long enough for a per-minute window to roll over, short enough to give up in
# a reasonable time.
RATE_LIMIT_BACKOFF_SECONDS = 30.0


class LlmError(Exception):
    """The model could not be reached or could not produce usable output."""

    def __init__(self, message: str, *, kind: str = 'unknown', status: int | None = None,
                 attempts: int = 1, detail: str = '') -> None:
        super().__init__(message)
        self.kind = kind              # transport | http | empty | bad_json | auth | budget
        self.status = status
        self.attempts = attempts
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {'error': str(self), 'kind': self.kind, 'status': self.status,
                'attempts': self.attempts, 'detail': self.detail[:300]}


# Failures where asking again cannot help, so the fallback chain must not swallow them:
#   auth   - the credential is missing, spent or revoked;
#   budget - this client has already sent as many requests as it is allowed to.
TERMINAL_ERROR_KINDS = ('auth', 'budget')


@dataclass
class LlmConfig:
    """Runtime configuration. Built from the environment; never serialised."""
    base: str = DEFAULT_BASE
    key: str = ''
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: int = DEFAULT_TIMEOUT
    min_interval: float = DEFAULT_MIN_INTERVAL
    attempts: int = DEFAULT_ATTEMPTS
    # Thinking is off by default for the reason in the module docstring.
    enable_thinking: bool = False
    review: bool = True
    # How the reply's shape is constrained. `json_schema` is OpenAI's Structured
    # Outputs extension: a compatible gateway may accept it and ignore it, or answer
    # `HTTP 400`. `json_object` is the widely supported subset (some vendors require
    # the word "json" to appear in the prompt). `none` sends no constraint and relies
    # on the prompt plus the caller's own validation.
    response_format: str = RESPONSE_FORMAT_JSON_OBJECT
    max_requests: int = DEFAULT_MAX_REQUESTS
    # Optional JSONL path for a verbatim record of every request and reply. Used to
    # verify a real run; the credential is never written into it.
    transcript: str = ''
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LlmConfig:
        env = os.environ if env is None else env
        requested = str(env.get('TR_RESPONSE_FORMAT', RESPONSE_FORMAT_JSON_OBJECT))
        requested = requested.strip().casefold() or RESPONSE_FORMAT_JSON_OBJECT
        return cls(
            base=env.get('TR_BASE', DEFAULT_BASE),
            key=env.get('TR_KEY', ''),
            model=env.get('TR_MODEL', DEFAULT_MODEL),
            max_tokens=int(env.get('TR_MAX_TOKENS', DEFAULT_MAX_TOKENS)),
            timeout=int(env.get('TR_TIMEOUT', DEFAULT_TIMEOUT)),
            min_interval=float(env.get('TR_GAP', DEFAULT_MIN_INTERVAL)),
            # One attempt on demand: the real-model validation runs are meant to send
            # exactly one request, and a verification that quietly retries three times
            # is neither one request nor a clean measurement.
            attempts=int(env.get('TR_ATTEMPTS', DEFAULT_ATTEMPTS)),
            response_format=(requested if requested in RESPONSE_FORMATS
                             else RESPONSE_FORMAT_JSON_OBJECT),
            max_requests=int(env.get('TR_MAX_REQUESTS', DEFAULT_MAX_REQUESTS)),
            transcript=str(env.get('TR_TRANSCRIPT', '') or ''),
            review=str(env.get('TR_REVIEW', '1')).strip().casefold() not in ('0', 'false', 'off'),
        )

    def require_key(self) -> None:
        """fail-fast, like GWOA's LLM factory: no silent fallback to a dummy key."""
        if not self.key or self.key.startswith('sk-xxxx') or len(self.key) < 20:
            raise LlmError(
                'TR_KEY is not configured: set it in the environment before running. '
                'Credentials are deliberately never read from a file.',
                kind='auth')


class SlidingWindow:
    """Allow at most `max_calls` per `window_seconds`, sleeping when needed.

    Borrowed from GWOA's rate limiter (which guards logins); the direction is
    reversed here - instead of rejecting callers, it slows them down so the remote
    endpoint never has cause to reject us.
    """

    def __init__(self, max_calls: int = 1, window_seconds: float = DEFAULT_MIN_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.max_calls = max(1, int(max_calls))
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._sleep = sleeper
        self._recent: list[float] = []

    def acquire(self) -> float:
        """Block until a slot is free. Returns the seconds slept."""
        slept = 0.0
        while True:
            now = self._clock()
            self._recent = [t for t in self._recent if now - t < self.window_seconds]
            if len(self._recent) < self.max_calls:
                self._recent.append(now)
                return slept
            wait = self.window_seconds - (now - self._recent[0])
            wait = max(0.05, min(wait, 30.0))
            self._sleep(wait)
            slept += wait


def strip_code_fence(text: str) -> str:
    """Pull a JSON object out of whatever wrapper the model used.

    Models often answer with ```json ... ``` or a sentence before the object. Taking
    the span from the first '{' to the last '}' is crude but has proved reliable, and
    it is exactly what GWOA's supervisor does after its structured call fails.
    """
    raw = (text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw).strip()
    start, end = raw.find('{'), raw.rfind('}')
    if start >= 0 and end > start:
        return raw[start:end + 1]
    return raw


def _http_post(url: str, payload: dict, headers: dict, timeout: int):
    """Default transport. Returns (status, body_text)."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'), method='POST')
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')
    except Exception as exc:                        # noqa: BLE001 - report everything
        raise LlmError('%s: %s' % (type(exc).__name__, exc), kind='transport',
                       detail=str(exc)) from exc


class ChatClient:
    """Minimal client for `POST /chat/completions`."""

    def __init__(self, config: LlmConfig | None = None,
                 transport: Callable[[str, dict, dict, int], tuple[int, str]] | None = None,
                 limiter: SlidingWindow | None = None,
                 sleeper: Callable[[float], None] = time.sleep,
                 logger: Callable[[str], None] | None = None,
                 max_requests: int | None = None,
                 transcript: Path | str | None = None) -> None:
        self.config = config or LlmConfig.from_env()
        self._post = transport or _http_post
        self._limiter = limiter or SlidingWindow(
            max_calls=1, window_seconds=self.config.min_interval, sleeper=sleeper)
        self._sleep = sleeper
        self._log = logger or (lambda msg: None)
        self.calls = 0                # for tests and for run manifests
        # A hard ceiling on HTTP requests for this client's lifetime. Without it a
        # single intake could emit far more requests than it needed - the observed
        # worst case was 18 - which is exactly the behaviour that turned a throttled
        # endpoint into a refused one.
        budget = self.config.max_requests if max_requests is None else max_requests
        self.max_requests = max(1, int(budget))
        self.budget_exhausted = False
        # Set once the endpoint has answered 400/422 to a response_format request: from
        # then on this client sends no constraint at all, because the constraint is what
        # the endpoint objected to.
        self.response_format_degraded = False
        # Optional JSONL record of every request and reply, for verifying a real run.
        # The credential is deliberately NOT included.
        self.transcript = Path(transcript or self.config.transcript) \
            if (transcript or self.config.transcript) else None

    def _record(self, payload: dict, status: int | None, body: str) -> None:
        """Append one exchange to the transcript, if one was asked for.

        Used by the real-model validation: the raw reply has to be archived, because
        "the model returned the wrong keys" is only checkable against the reply itself.
        """
        if self.transcript is None:
            return
        try:
            self.transcript.parent.mkdir(parents=True, exist_ok=True)
            entry = {'request': payload, 'status': status, 'body': body}
            with self.transcript.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except OSError:
            pass

    # ------------------------------------------------------------------ public
    def complete(self, system: str, user: str, *, schema: dict | None = None,
                 schema_name: str = 'Extraction', max_tokens: int | None = None,
                 temperature: float | None = None) -> dict[str, Any]:
        """Return the parsed JSON object, or raise LlmError.

        Fallback chain (each step only runs if the previous one produced nothing):
          1. schema-constrained request
          2. plain request, then extract JSON from the reply
        Both are retried on retryable statuses, with backoff.

        When both fail, the most diagnostic error wins. That matters: a 503 on the
        schema path followed by a JSON parse error on the plain path must not be
        reported as "bad JSON", or the real cause (the endpoint was down) is lost.
        """
        self.config.require_key()
        failures: list[LlmError] = []
        mode = RESPONSE_FORMAT_NONE if self.response_format_degraded \
            else self.config.response_format

        if schema is not None and mode != RESPONSE_FORMAT_NONE:
            try:
                return self._call_with_schema(system, user, schema, schema_name,
                                              max_tokens, temperature, mode)
            except LlmError as exc:
                if exc.kind in TERMINAL_ERROR_KINDS:
                    # A spent credential or an exhausted budget: the plain-text request
                    # would be sent with the same credential and the same budget, so
                    # falling back only adds a request to an endpoint that has already
                    # said no.
                    raise
                if exc.kind == 'request_shape' and not self.response_format_degraded:
                    # The endpoint does not accept this response_format. Drop it for the
                    # rest of this client's life, log it, and ask once more with no
                    # constraint at all - the prompt carries the field names, so the
                    # contract still travels.
                    self.response_format_degraded = True
                    mode = RESPONSE_FORMAT_NONE
                    self._log('response_format=%s rejected (HTTP %s); degrading to none '
                              'for the rest of this run'
                              % (self.config.response_format, exc.status))
                else:
                    failures.append(exc)
                    self._log('schema call failed (%s), falling back to plain text'
                              % exc.kind)

        # The unconstrained request is the LAST step of the chain, never a constrained
        # one: it exists precisely to drop the constraint, and retrying it with a
        # response_format would just repeat the request the endpoint rejected. That is
        # why it is `_call_plain` regardless of the configured mode - the mode decides
        # what the FIRST attempt asks for, not what the fallback does.
        try:
            text = self._call_plain(system, user, max_tokens, temperature)
            return json.loads(strip_code_fence(text))
        except LlmError as exc:
            if exc.kind in TERMINAL_ERROR_KINDS:
                raise
            failures.append(exc)
        except Exception as exc:                        # noqa: BLE001
            failures.append(LlmError('reply was not JSON: %s' % exc, kind='bad_json',
                                     detail=str(exc)))

        raise _most_diagnostic(failures)

    # --------------------------------------------------------------- internals
    def _payload(self, system: str, user: str, max_tokens: int | None,
                 temperature: float | None, schema: dict | None,
                 schema_name: str, response_format: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'model': self.config.model,
            'max_tokens': max_tokens or self.config.max_tokens,
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': user}],
        }
        # The one flag that mattered most in benchmarking.
        if not self.config.enable_thinking:
            payload['enable_thinking'] = False
        if temperature is not None:
            payload['temperature'] = temperature
        # `RESPONSE_FORMAT_NONE` means "send nothing", not "use the configured mode":
        # the unconstrained fallback passes it explicitly so that it cannot inherit a
        # constraint it exists to drop.
        mode = self.config.response_format if response_format is None else response_format
        if schema is not None and mode == RESPONSE_FORMAT_JSON_SCHEMA:
            payload['response_format'] = {
                'type': 'json_schema',
                'json_schema': {'name': schema_name, 'strict': True, 'schema': schema}}
        elif mode == RESPONSE_FORMAT_JSON_OBJECT:
            # Portable subset. The schema is not transmitted, so the prompt has to
            # carry the field names - which it does.
            payload['response_format'] = {'type': 'json_object'}
        payload.update(self.config.extra_body)
        return payload

    def _send(self, payload: dict) -> dict[str, Any]:
        """One request with retries on retryable statuses, inside a request budget.

        401 is not retried (see `RETRYABLE_STATUS`), and 429 waits for
        `RATE_LIMIT_BACKOFF_SECONDS` rather than the short transient backoff - the
        endpoint has just told us it is busy, so waiting two seconds and asking again
        is the worst possible response.
        """
        url = self.config.base.rstrip('/') + '/chat/completions'
        headers = {'Authorization': 'Bearer ' + self.config.key,
                   'Content-Type': 'application/json'}
        last: LlmError | None = None
        for attempt in range(1, self.config.attempts + 1):
            # Budget first: this is a hard ceiling, so the request that would cross it
            # is never sent. The very first request of a call is always allowed, so an
            # exhausted budget reports itself rather than looking like a transport
            # failure.
            if self.calls >= self.max_requests:
                self.budget_exhausted = True
                raise LlmError(
                    'request budget exhausted after %d requests; stopping instead of '
                    'asking the endpoint again. Raise TR_MAX_REQUESTS only if the '
                    'endpoint is known to tolerate it.' % self.calls,
                    kind='budget', attempts=attempt - 1)
            self._limiter.acquire()
            self.calls += 1
            try:
                status, text = self._post(url, payload, headers, self.config.timeout)
            except LlmError as exc:
                self._record(payload, None, str(exc))
                last = exc
                if attempt < self.config.attempts:
                    self._sleep(min(2 ** attempt, 15))
                    continue
                raise
            self._record(payload, status, text)

            if status == 200:
                try:
                    return json.loads(text)
                except Exception as exc:                # noqa: BLE001
                    last = LlmError('HTTP 200 but unreadable body: %s' % exc,
                                    kind='bad_json', attempts=attempt, detail=text[:200])
            elif status in RETRYABLE_STATUS:
                last = LlmError('HTTP %d from the model endpoint' % status,
                                kind='http', status=status, attempts=attempt,
                                detail=text[:200])
            elif status in _REQUEST_SHAPE_STATUSES:
                # 400/422 while a response_format was sent: the endpoint is rejecting
                # the request's shape. Retrying unchanged cannot help, so it is marked
                # for the one-time degradation in `complete` instead of being retried.
                raise LlmError('HTTP %d from the model endpoint: %s'
                               % (status, text[:200]),
                               kind='request_shape', status=status, attempts=attempt,
                               detail=text[:200])
            else:
                # 401 is reported as `auth`, not `http`, so the fallback chain treats it
                # as terminal: a rejected credential cannot be fixed by asking again in
                # a different format.
                kind = 'auth' if status == 401 else 'http'
                raise LlmError('HTTP %d from the model endpoint' % status, kind=kind,
                               status=status, attempts=attempt, detail=text[:200])
            if attempt < self.config.attempts:
                self._sleep(RATE_LIMIT_BACKOFF_SECONDS if status == 429
                            else min(2 ** attempt, 15))
        if self.calls >= self.max_requests:
            self.budget_exhausted = True
            raise LlmError(
                'request budget exhausted after %d requests; the last attempt failed '
                'with %s. Stopping instead of asking the endpoint again.'
                % (self.calls, last), kind='budget', attempts=self.config.attempts)
        raise last or LlmError('request failed', kind='http',
                               attempts=self.config.attempts)

    def _content_of(self, body: dict) -> tuple[str, dict]:
        choices = body.get('choices') or []
        if not choices:
            raise LlmError('reply contained no choices', kind='empty',
                           detail=json.dumps(body)[:200])
        choice = choices[0]
        message = choice.get('message') or {}
        return (message.get('content') or ''), {
            'finish_reason': choice.get('finish_reason'),
            'usage': body.get('usage') or {},
            'reasoning_tokens': ((body.get('usage') or {}).get(
                'completion_tokens_details') or {}).get('reasoning_tokens', 0),
        }

    def _empty_reason(self, meta: dict) -> LlmError:
        """Explain an empty content in terms of what actually caused it."""
        reasoning = meta.get('reasoning_tokens') or 0
        hint = ''
        if reasoning:
            hint = (' - %d tokens went to reasoning, so the answer was cut off. '
                    'enable_thinking=False avoids this.' % reasoning)
        return LlmError('model returned empty content (finish=%s)%s'
                        % (meta.get('finish_reason'), hint), kind='empty')

    def _call_with_schema(self, system: str, user: str, schema: dict,
                          schema_name: str, max_tokens: int | None,
                          temperature: float | None,
                          response_format: str | None = None) -> dict[str, Any]:
        body = self._send(self._payload(system, user, max_tokens, temperature,
                                        schema, schema_name, response_format))
        text, meta = self._content_of(body)
        if not text.strip():
            raise self._empty_reason(meta)
        try:
            return json.loads(strip_code_fence(text))
        except Exception as exc:                        # noqa: BLE001
            raise LlmError('schema-constrained reply was not valid JSON: %s' % exc,
                           kind='bad_json', detail=text[:200]) from exc

    def _call_plain(self, system: str, user: str, max_tokens: int | None,
                    temperature: float | None) -> str:
        # Explicitly unconstrained, whatever `config.response_format` says: this is the
        # fallback that exists to drop the constraint.
        body = self._send(self._payload(system, user, max_tokens, temperature,
                                        None, 'plain', RESPONSE_FORMAT_NONE))
        text, meta = self._content_of(body)
        if not text.strip():
            raise self._empty_reason(meta)
        return text


def _most_diagnostic(failures: list[LlmError]) -> LlmError:
    """Pick the failure that best explains what went wrong.

    A terminal failure outranks everything: "we stopped because the credential was
    refused / the request budget ran out" is the operator's actual problem, and it must
    not be reported as the transport error that happened to come first - which is what
    made an exhausted budget look like a 503.

    Below that, a transport or HTTP failure tells the operator something actionable
    (the endpoint is down, the model does not exist) where a JSON parse error does not,
    and 'empty' - which specifically means the answer was truncated - outranks
    'bad_json'.
    """
    if not failures:
        return LlmError('no usable reply', kind='unknown')

    def rank(error: LlmError) -> int:
        if error.kind in TERMINAL_ERROR_KINDS:
            return 4
        if error.status is not None:
            return 3
        return {'transport': 3, 'empty': 2, 'http': 2, 'bad_json': 1}.get(error.kind, 0)

    best = max(failures, key=rank)
    if len(failures) > 1:
        best.detail = (best.detail + ' | also tried: '
                       + '; '.join('%s: %s' % (e.kind, e) for e in failures
                                   if e is not best))[:400]
    return best


def describe_config(config: LlmConfig) -> dict[str, Any]:
    """Non-sensitive view of the configuration, safe for logs and manifests."""
    return {'base': config.base, 'model': config.model,
            'max_tokens': config.max_tokens, 'timeout': config.timeout,
            'min_interval': config.min_interval, 'attempts': config.attempts,
            'enable_thinking': config.enable_thinking,
            'key_present': bool(config.key)}


def _demo() -> int:
    """Manual smoke test: python -m reactor_agent.llm"""
    config = LlmConfig.from_env()
    print(json.dumps(describe_config(config), ensure_ascii=False, indent=2))
    if not config.key:
        print('TR_KEY not set; nothing to do', file=sys.stderr)
        return 2
    client = ChatClient(config, logger=lambda m: print('[llm] ' + m, file=sys.stderr))
    out = client.complete(
        'You extract chemical-engineering facts. Copy numbers and units exactly.',
        '甲苯转化率50%，进料10000 kg/h，380 摄氏度，2.5 MPa。',
        schema={'type': 'object',
                'properties': {'conversion_percent': {'type': ['number', 'null']},
                               'feed_total': {'type': ['number', 'null']},
                               'feed_unit': {'type': 'string'}},
                'required': ['conversion_percent', 'feed_total', 'feed_unit'],
                'additionalProperties': False},
        schema_name='Smoke')
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(_demo())
