"""
tests/test_review_script.py

Unit tests for scripts/review.py — pure functions only.
No real GitHub API or Claude CLI calls are made.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow importing from scripts/ without installing it as a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from review import build_comment, call_claude, parse_json_output  # noqa: E402


# ── parse_json_output ─────────────────────────────────────────────────────────


def test_parse_json_output_clean_json():
    """Valid bare JSON returns a dict."""
    raw = '{"verdict": "approve", "summary": "All good.", "issues": []}'
    result = parse_json_output(raw)
    assert isinstance(result, dict)
    assert result["verdict"] == "approve"
    assert result["issues"] == []


def test_parse_json_output_fenced_json():
    """JSON wrapped in a ```json ... ``` fence is extracted correctly."""
    raw = '```json\n{"verdict": "request_changes", "summary": "Bug found.", "issues": []}\n```'
    result = parse_json_output(raw)
    assert isinstance(result, dict)
    assert result["verdict"] == "request_changes"
    assert result["summary"] == "Bug found."


def test_parse_json_output_invalid_json():
    """Non-JSON input returns a fallback dict with verdict 'unknown'."""
    raw = "This is not JSON at all."
    result = parse_json_output(raw)
    assert isinstance(result, dict)
    assert result["verdict"] == "unknown"
    # The raw text should be captured in summary (truncated to 500 chars).
    assert raw in result["summary"]
    assert result["issues"] == []


# ── build_comment ─────────────────────────────────────────────────────────────


def test_build_comment_approve_verdict():
    """Approve verdict produces ✅ emoji and 'approve' text in the comment."""
    sonnet_result = {
        "verdict": "approve",
        "summary": "No issues detected.",
        "issues": [],
    }
    opus_result = {
        "verdict": "approve_fix",
        "reasoning": "Sonnet's conclusion is sound.",
        "concerns": [],
    }
    comment = build_comment(sonnet_result, opus_result, pr_number=7)
    assert "✅" in comment
    assert "approve" in comment.lower()
    assert "PR #7" in comment


def test_build_comment_escalate_verdict():
    """Escalate verdict produces 🚨 emoji and escalation message."""
    sonnet_result = {
        "verdict": "request_changes",
        "summary": "Critical security flaw found.",
        "issues": [],
    }
    opus_result = {
        "verdict": "escalate",
        "reasoning": "Sonnet missed a deeper vulnerability.",
        "concerns": ["Possible RCE via eval()"],
    }
    comment = build_comment(sonnet_result, opus_result, pr_number=12)
    assert "🚨" in comment
    assert "人間によるレビューが必要" in comment


def test_build_comment_skipped_opus():
    """When Opus is skipped, ⏭️ appears and the skip message is present."""
    sonnet_result = {
        "verdict": "approve",
        "summary": "Minor style nit only.",
        "issues": [],
    }
    opus_result = {
        "verdict": "skipped",
        "reasoning": "Sonnet が監督不要と判断しました（軽微な問題のみ）。",
        "concerns": [],
    }
    comment = build_comment(sonnet_result, opus_result, pr_number=99)
    assert "⏭️" in comment
    assert "skipped" in comment.lower() or "スキップ" in comment


def test_build_comment_lists_issues():
    """Issues with a fix suggestion appear formatted in the comment."""
    sonnet_result = {
        "verdict": "request_changes",
        "summary": "SQL injection detected.",
        "issues": [
            {
                "severity": "error",
                "file": "app/db.py",
                "line": 42,
                "message": "Unsanitised user input passed to query.",
                "fix": "Use parameterised queries.",
            }
        ],
    }
    opus_result = {
        "verdict": "approve_fix",
        "reasoning": "Fix looks correct.",
        "concerns": [],
    }
    comment = build_comment(sonnet_result, opus_result, pr_number=3)
    assert "app/db.py" in comment
    assert "line 42" in comment
    assert "Use parameterised queries." in comment
    assert "ERROR" in comment


# ── call_claude ───────────────────────────────────────────────────────────────


def test_call_claude_timeout():
    """When subprocess.run raises TimeoutExpired, call_claude returns fallback JSON with verdict 'error'."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude.cmd", timeout=180)):
        raw = call_claude("some prompt", "claude-sonnet-4-6")

    result = json.loads(raw)
    assert result["verdict"] == "error"
    assert "タイムアウト" in result["summary"]
    assert result["issues"] == []


def test_call_claude_nonzero_returncode():
    """When subprocess.run returns a non-zero exit code, call_claude returns fallback JSON with verdict 'error'."""
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stderr = "claude: command not found"

    with patch("subprocess.run", return_value=mock_proc):
        raw = call_claude("some prompt", "claude-sonnet-4-6")

    result = json.loads(raw)
    assert result["verdict"] == "error"
    assert "claude: command not found" in result["summary"]
    assert result["issues"] == []
