"""
tests/test_supervisor.py

Unit tests for agent/supervisor.py.
All tests mock the Anthropic client — no real API calls are made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.models import Issue, ReviewResult, Severity, Verdict
from agent.supervisor import (
    SupervisorError,
    SupervisorVerdict,
    SupervisionResult,
    _build_supervision_prompt,
    _parse_supervision,
    supervise,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_DIFF = "--- a/app/auth.py\n+++ b/app/auth.py\n@@ -1 +1 @@\n-bad\n+good"

SAMPLE_REVIEW = ReviewResult(
    verdict=Verdict.REQUEST_CHANGES,
    summary="SQL injection found.",
    issues=[
        Issue(
            file="app/auth.py",
            line=10,
            severity=Severity.ERROR,
            message="SQL injection risk",
            fix='cursor.execute("SELECT * FROM users WHERE id = ?", (uid,))',
        )
    ],
)

APPROVE_JSON = '{"verdict": "approve_fix", "reasoning": "Fix is correct and safe.", "concerns": []}'
OVERRIDE_JSON = '{"verdict": "override_to_comment", "reasoning": "Fix changes too much context.", "concerns": ["Replacement may break line 11"]}'
ESCALATE_JSON = '{"verdict": "escalate", "reasoning": "Sonnet missed a critical auth bypass.", "concerns": ["Missing auth check on line 5", "Token not validated"]}'


def _make_client(response_text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = response_text
    message = MagicMock()
    message.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=message)
    return client


# ── _parse_supervision ────────────────────────────────────────────────────────

def test_parse_approve():
    result = _parse_supervision(APPROVE_JSON)
    assert result.verdict == SupervisorVerdict.APPROVE_FIX
    assert result.approved is True
    assert result.needs_human is False
    assert result.concerns == []


def test_parse_override():
    result = _parse_supervision(OVERRIDE_JSON)
    assert result.verdict == SupervisorVerdict.OVERRIDE_TO_COMMENT
    assert result.approved is False
    assert "line 11" in result.concerns[0]


def test_parse_escalate():
    result = _parse_supervision(ESCALATE_JSON)
    assert result.verdict == SupervisorVerdict.ESCALATE
    assert result.needs_human is True
    assert len(result.concerns) == 2


def test_parse_invalid_json_raises():
    with pytest.raises(SupervisorError, match="non-JSON"):
        _parse_supervision("not json")


def test_parse_invalid_verdict_raises():
    with pytest.raises(SupervisorError, match="schema mismatch"):
        _parse_supervision('{"verdict": "bad_value", "reasoning": "x", "concerns": []}')


def test_parse_missing_reasoning_raises():
    with pytest.raises(SupervisorError, match="schema mismatch"):
        _parse_supervision('{"verdict": "approve_fix", "concerns": []}')


# ── _build_supervision_prompt ─────────────────────────────────────────────────

def test_prompt_contains_diff_and_review():
    prompt = _build_supervision_prompt(SAMPLE_DIFF, SAMPLE_REVIEW, nonce="aabbccdd")
    assert "app/auth.py" in prompt
    assert "SQL injection" in prompt
    assert "request_changes" in prompt
    assert "--- a/app/auth.py" in prompt


def test_prompt_includes_fix_suggestion():
    prompt = _build_supervision_prompt(SAMPLE_DIFF, SAMPLE_REVIEW, nonce="aabbccdd")
    assert "fix:" in prompt


def test_prompt_handles_no_issues():
    review = ReviewResult(verdict=Verdict.APPROVE, summary="All good.", issues=[])
    prompt = _build_supervision_prompt(SAMPLE_DIFF, review, nonce="aabbccdd")
    assert "(none)" in prompt


def test_prompt_uses_nonce_delimiter():
    prompt = _build_supervision_prompt(SAMPLE_DIFF, SAMPLE_REVIEW, nonce="aabbccdd")
    assert "--- original diff-aabbccdd ---" in prompt
    assert "--- end diff-aabbccdd ---" in prompt
    assert "--- original diff ---" not in prompt


# ── supervise ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supervise_approve():
    client = _make_client(APPROVE_JSON)
    result = await supervise(SAMPLE_DIFF, SAMPLE_REVIEW, client=client)
    assert result.verdict == SupervisorVerdict.APPROVE_FIX
    client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_supervise_uses_opus_model():
    from agent.supervisor import OPUS_MODEL
    client = _make_client(APPROVE_JSON)
    await supervise(SAMPLE_DIFF, SAMPLE_REVIEW, client=client)
    call_kwargs = client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == OPUS_MODEL


@pytest.mark.asyncio
async def test_supervise_escalate():
    client = _make_client(ESCALATE_JSON)
    result = await supervise(SAMPLE_DIFF, SAMPLE_REVIEW, client=client)
    assert result.needs_human is True
    assert len(result.concerns) == 2


@pytest.mark.asyncio
async def test_supervise_api_error_raises():
    import anthropic as _anthropic
    client = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=_anthropic.APIError(message="overloaded", request=MagicMock(), body={})
    )
    with pytest.raises(SupervisorError, match="Opus API error"):
        await supervise(SAMPLE_DIFF, SAMPLE_REVIEW, client=client)


@pytest.mark.asyncio
async def test_supervise_empty_response_raises():
    block = MagicMock()
    block.type = "tool_use"   # not "text"
    message = MagicMock()
    message.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=message)
    with pytest.raises(SupervisorError, match="no text content"):
        await supervise(SAMPLE_DIFF, SAMPLE_REVIEW, client=client)
