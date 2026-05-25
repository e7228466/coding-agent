"""
tests/test_reviewer.py

Unit tests for agent/reviewer.py.
All tests mock the Anthropic client — no real API calls are made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.models import FixStrategy, Severity, Verdict
from agent.reviewer import (
    ReviewContext,
    ReviewerError,
    _build_user_prompt,
    _call_llm,
    _extract_text,
    _parse_response,
    _truncate_diff,
    review_diff,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

CONTEXT = ReviewContext(
    repo="acme/backend",
    pr_number="42",
    pr_branch="feat/login",
    pr_sha="abc1234",
)

VALID_APPROVE_JSON = """{
  "verdict": "approve",
  "summary": "Looks good. No issues found.",
  "issues": []
}"""

VALID_REQUEST_CHANGES_JSON = """{
  "verdict": "request_changes",
  "summary": "Found one security issue.",
  "issues": [
    {
      "file": "app/auth.py",
      "line": 42,
      "severity": "error",
      "message": "SQL query is vulnerable to injection.",
      "fix": "cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))"
    }
  ]
}"""

VALID_NO_FIX_JSON = """{
  "verdict": "request_changes",
  "summary": "Logic error detected but fix is complex.",
  "issues": [
    {
      "file": "app/worker.py",
      "line": 10,
      "severity": "warning",
      "message": "Race condition possible under high concurrency.",
      "fix": null
    }
  ]
}"""


def _make_mock_client(response_text: str) -> MagicMock:
    """Build a mock anthropic.AsyncAnthropic client that returns response_text."""
    block = MagicMock()
    block.type = "text"
    block.text = response_text

    message = MagicMock()
    message.content = [block]

    client = MagicMock()
    client.messages.create = AsyncMock(return_value=message)  # async — must be AsyncMock
    return client


# ── _truncate_diff ────────────────────────────────────────────────────────────


def test_truncate_diff_short_diff_unchanged():
    diff = "x" * 100
    assert _truncate_diff(diff) == diff


def test_truncate_diff_long_diff_truncated():
    diff = "x" * (8_000 * 4 + 200)  # well over the limit so trailer doesn't push result above original
    result = _truncate_diff(diff)
    assert "[diff truncated" in result
    assert len(result) < len(diff)


def test_truncate_diff_custom_max():
    diff = "a" * 100
    result = _truncate_diff(diff, max_tokens=10)  # 10 tokens = 40 chars
    assert len(result) <= 40 + len("\n\n[diff truncated — file too large for single review]")


# ── _build_user_prompt ────────────────────────────────────────────────────────


def test_build_user_prompt_contains_metadata():
    prompt = _build_user_prompt("diff content", CONTEXT, nonce="deadbeef12345678")
    assert "acme/backend" in prompt
    assert "#42" in prompt
    assert "feat/login" in prompt
    assert "abc1234" in prompt
    assert "diff content" in prompt


def test_build_user_prompt_uses_nonce_delimiter():
    import re
    prompt = _build_user_prompt("some diff", CONTEXT, nonce="deadbeef12345678")
    assert "--- diff-deadbeef12345678 ---" in prompt
    assert "--- end diff-deadbeef12345678 ---" in prompt
    # Must not use the old static delimiter
    assert "--- diff ---" not in prompt


def test_review_diff_uses_unique_nonce_per_call():
    """Two back-to-back review_diff calls must embed different nonces."""
    import asyncio
    import re

    prompts: list[str] = []

    async def _run():
        for _ in range(2):
            mock_client = _make_mock_client(VALID_APPROVE_JSON)
            await review_diff("some diff", CONTEXT, client=mock_client)
            call_kwargs = mock_client.messages.create.call_args.kwargs
            prompts.append(call_kwargs["messages"][0]["content"])

    asyncio.get_event_loop().run_until_complete(_run())
    nonces = [re.search(r"--- diff-([0-9a-f]+) ---", p).group(1) for p in prompts]
    assert nonces[0] != nonces[1], "Each review_diff call must generate a fresh nonce"


# ── _parse_response ───────────────────────────────────────────────────────────


def test_parse_approve():
    result = _parse_response(VALID_APPROVE_JSON)
    assert result.verdict == Verdict.APPROVE
    assert result.issues == []
    assert result.summary == "Looks good. No issues found."


def test_parse_request_changes_with_fix():
    result = _parse_response(VALID_REQUEST_CHANGES_JSON)
    assert result.verdict == Verdict.REQUEST_CHANGES
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.file == "app/auth.py"
    assert issue.line == 42
    assert issue.severity == Severity.ERROR
    assert issue.fix is not None


def test_parse_request_changes_no_fix():
    result = _parse_response(VALID_NO_FIX_JSON)
    assert result.issues[0].fix is None
    assert result.fix_strategy() == FixStrategy.COMMENT_ONLY


def test_parse_invalid_json_raises():
    with pytest.raises(ReviewerError, match="non-JSON"):
        _parse_response("not json at all")


def test_parse_missing_verdict_raises():
    bad = '{"summary": "ok", "issues": []}'
    with pytest.raises(ReviewerError, match="schema mismatch"):
        _parse_response(bad)


def test_parse_invalid_severity_raises():
    bad = """{
      "verdict": "approve",
      "summary": "ok",
      "issues": [{"file": "f.py", "severity": "INVALID", "message": "x", "fix": null}]
    }"""
    with pytest.raises(ReviewerError, match="schema mismatch"):
        _parse_response(bad)


# ── review_diff (integration with mock client) ────────────────────────────────


@pytest.mark.asyncio
async def test_review_diff_approve():
    client = _make_mock_client(VALID_APPROVE_JSON)
    result = await review_diff("some diff", CONTEXT, client=client)
    assert result.verdict == Verdict.APPROVE
    client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_review_diff_request_changes():
    client = _make_mock_client(VALID_REQUEST_CHANGES_JSON)
    result = await review_diff("some diff", CONTEXT, client=client)
    assert result.verdict == Verdict.REQUEST_CHANGES
    assert result.error_count == 1


@pytest.mark.asyncio
async def test_review_diff_api_error_raises():
    import anthropic as _anthropic

    client = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=_anthropic.APIError(
            message="rate limit", request=MagicMock(), body={}
        )
    )
    with pytest.raises(ReviewerError, match="Claude API error"):
        await review_diff("diff", CONTEXT, client=client)


@pytest.mark.asyncio
async def test_review_diff_passes_correct_model():
    from agent.reviewer import SONNET_MODEL

    client = _make_mock_client(VALID_APPROVE_JSON)
    await review_diff("diff", CONTEXT, client=client)
    call_kwargs = client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == SONNET_MODEL


@pytest.mark.asyncio
async def test_review_diff_fix_strategy_auto_fix():
    client = _make_mock_client(VALID_REQUEST_CHANGES_JSON)
    result = await review_diff("diff", CONTEXT, client=client)
    assert result.fix_strategy() == FixStrategy.AUTO_FIX


# ── _extract_text ─────────────────────────────────────────────────────────────


def test_extract_text_no_text_block_raises():
    message = MagicMock()
    block = MagicMock()
    block.type = "tool_use"   # not "text"
    message.content = [block]
    with pytest.raises(ReviewerError, match="no text content block"):
        _extract_text(message)


# ── _parse_response — model validator paths ───────────────────────────────────


def test_parse_empty_issue_message_raises():
    bad = '{"verdict":"approve","summary":"ok","issues":[{"file":"f.py","severity":"error","message":"","fix":null}]}'
    with pytest.raises(ReviewerError, match="schema mismatch"):
        _parse_response(bad)


def test_parse_empty_summary_raises():
    bad = '{"verdict":"approve","summary":"","issues":[]}'
    with pytest.raises(ReviewerError, match="schema mismatch"):
        _parse_response(bad)


# ── _call_llm — Ollama path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_llm_ollama_path_returns_content():
    from unittest.mock import patch

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = '{"verdict":"approve","summary":"ok","issues":[]}'

    mock_instance = MagicMock()
    mock_instance.chat.completions.create = AsyncMock(return_value=mock_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_instance):
        result = await _call_llm(
            "ollama/llama3.1",
            system="system",
            user_prompt="user",
            max_tokens=256,
        )

    assert "approve" in result
    mock_instance.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_call_llm_ollama_empty_content_returns_empty_string():
    from unittest.mock import patch

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = None  # model returned nothing

    mock_instance = MagicMock()
    mock_instance.chat.completions.create = AsyncMock(return_value=mock_resp)

    with patch("openai.AsyncOpenAI", return_value=mock_instance):
        result = await _call_llm(
            "ollama/deepseek-r1",
            system="system",
            user_prompt="user",
            max_tokens=256,
        )

    assert result == ""
