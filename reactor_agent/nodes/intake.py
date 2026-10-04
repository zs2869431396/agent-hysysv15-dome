"""The `intake` node: natural language in, checked facts out.

This is the first model call. A separate review node checks omissions and meaning
against the original request before deterministic planning.

The node deliberately does not decide anything about reactors or specs. Its output is
a fact dictionary plus a list of complaints - missing required fields and values that
cannot be traced back to the user's own words. Those complaints become blocking
questions in `plan`, which is what stops a fabricated number from reaching HYSYS.
"""
from __future__ import annotations

from typing import Any

from ..extraction import extract_verified
from ..llm import ChatClient
from .state import AgentState


def make_intake(client: ChatClient):
    """Build the intake node for a given model client."""

    def intake(state: AgentState) -> dict[str, Any]:
        extraction, problems = extract_verified(
            client, state['text'], state.get('kind', 'conversion'),
            allowed=set(state.get('allowed_ungrounded') or []), review=False)
        return {
            'facts': extraction.facts,
            'extraction_error': extraction.error,
            'extraction_attempts': extraction.attempts,
            # Carried separately because these must BLOCK, not merely be reported.
            'ungrounded': list(extraction.ungrounded),
            'problems': list(problems),
        }

    return intake
