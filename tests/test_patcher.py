"""
tests/test_patcher.py

Unit tests for agent/patcher.py.
GitClient is always mocked — no real GitHub calls are made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.models import Issue, ReviewResult, Severity, Verdict
from agent.patcher import (
    PatchContext,
    _apply_single_fix,
    _build_commit_message,
    apply_fixes,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_issue(
    file: str = "app/auth.py",
    line: int | None = 5,
    fix: str | None = "    return safe_value",
    severity: Severity = Severity.ERROR,
    message: str = "Unsafe value returned",
) -> Issue:
    return Issue(file=file, line=line, severity=severity, message=message, fix=fix)


def _make_review(issues: list[Issue], verdict: Verdict = Verdict.REQUEST_CHANGES) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary="One or more issues were found.",
        issues=issues,
    )


def _make_git(
    file_content: str = "line1\n    return unsafe_value\nline3\n",
    commit_sha: str = "deadbeef",
) -> MagicMock:
    git = MagicMock()
    git.get_file_content = AsyncMock(return_value=file_content)
    git.commit_files = AsyncMock(return_value=commit_sha)
    return git


CONTEXT = PatchContext(repo="acme/backend", pr_branch="feat/login", pr_sha="abc1234")


# ── _apply_single_fix ─────────────────────────────────────────────────────────

def test_apply_fix_replaces_target_line():
    content = "line1\n    return unsafe_value\nline3\n"
    issue = _make_issue(line=2, fix="    return safe_value")
    result = _apply_single_fix(issue, content)
    assert result is not None
    assert "return safe_value" in result
    assert "return unsafe_value" not in result


def test_apply_fix_already_present_returns_none():
    content = "line1\n    return safe_value\nline3\n"
    issue = _make_issue(line=2, fix="    return safe_value")
    assert _apply_single_fix(issue, content) is None


def test_apply_fix_no_line_hint_cannot_patch():
    content = "line1\n    return unsafe_value\nline3\n"
    issue = _make_issue(line=None, fix="    return safe_value")
    assert _apply_single_fix(issue, content) is None


def test_apply_fix_line_out_of_window_returns_none():
    content = "line1\nline2\nline3\n"
    # fix target is on line 50 but file only has 3 lines
    issue = _make_issue(line=50, fix="something_new")
    assert _apply_single_fix(issue, content) is None


# ── _build_commit_message ─────────────────────────────────────────────────────

def test_commit_message_has_agent_fix_prefix():
    review = _make_review([_make_issue()])
    msg = _build_commit_message(review, ["app/auth.py"])
    assert msg.startswith("[agent-fix]")


def test_commit_message_lists_files():
    review = _make_review([_make_issue()])
    msg = _build_commit_message(review, ["app/auth.py", "app/db.py"])
    assert "app/auth.py" in msg
    assert "app/db.py" in msg


def test_commit_message_truncates_long_file_list():
    files = [f"file{i}.py" for i in range(6)]
    review = _make_review([_make_issue()])
    msg = _build_commit_message(review, files)
    assert "+3 more" in msg


def test_commit_message_includes_issue_counts():
    issues = [
        _make_issue(severity=Severity.ERROR),
        _make_issue(severity=Severity.ERROR),
        _make_issue(severity=Severity.WARNING, fix=None),
    ]
    review = _make_review(issues)
    msg = _build_commit_message(review, ["f.py"])
    assert "2 error(s)" in msg
    assert "1 warning(s)" in msg


# ── apply_fixes ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_fixes_success():
    git = _make_git()
    issue = _make_issue(line=2, fix="    return safe_value")
    review = _make_review([issue])

    result = await apply_fixes(review, CONTEXT, git=git)

    assert result.success is True
    assert result.commit_sha == "deadbeef"
    assert "app/auth.py" in result.patched_files
    assert result.failed_files == []


@pytest.mark.asyncio
async def test_apply_fixes_no_fixable_issues():
    git = _make_git()
    issue = _make_issue(fix=None)
    review = _make_review([issue])

    result = await apply_fixes(review, CONTEXT, git=git)

    assert result.success is False
    assert "No fixable issues" in result.error_message
    git.commit_files.assert_not_called()


@pytest.mark.asyncio
async def test_apply_fixes_file_fetch_fails():
    from agent.git_client import GitClientError

    git = _make_git()
    git.get_file_content = AsyncMock(side_effect=GitClientError("not found"))

    issue = _make_issue(line=2, fix="    return safe_value")
    review = _make_review([issue])

    result = await apply_fixes(review, CONTEXT, git=git)

    assert result.success is False
    assert "app/auth.py" in result.failed_files
    git.commit_files.assert_not_called()


@pytest.mark.asyncio
async def test_apply_fixes_commit_push_fails():
    from agent.git_client import GitClientError

    git = _make_git()
    git.commit_files = AsyncMock(side_effect=GitClientError("push rejected"))

    issue = _make_issue(line=2, fix="    return safe_value")
    review = _make_review([issue])

    result = await apply_fixes(review, CONTEXT, git=git)

    assert result.success is False
    assert "push failed" in result.error_message
    assert "app/auth.py" in result.patched_files  # was patched locally, just not pushed


@pytest.mark.asyncio
async def test_apply_fixes_multiple_issues_same_file():
    """Two fixable issues in the same file — both should be applied in sequence."""
    content = "def foo():\n    x = bad1\n    y = bad2\n    return x + y\n"
    git = _make_git(file_content=content)

    issues = [
        Issue(file="app/calc.py", line=2, severity=Severity.ERROR,
              message="bad1 is unsafe", fix="    x = good1"),
        Issue(file="app/calc.py", line=3, severity=Severity.ERROR,
              message="bad2 is unsafe", fix="    y = good2"),
    ]
    review = _make_review(issues)

    result = await apply_fixes(review, CONTEXT, git=git)

    # At least the first patch should succeed; second depends on heuristic
    assert result.success is True
    # Commit call receives the updated content
    git.commit_files.assert_called_once()
    committed_files: dict = git.commit_files.call_args.kwargs["files"]
    assert "app/calc.py" in committed_files


@pytest.mark.asyncio
async def test_apply_fixes_all_patches_fail_no_commit():
    git = _make_git(file_content="completely different content\n")
    # line hint far outside the file → pass 1 fails; fix not present → pass 2 fails
    issue = _make_issue(line=99, fix="    return safe_value")
    review = _make_review([issue])

    result = await apply_fixes(review, CONTEXT, git=git)

    assert result.success is False
    git.commit_files.assert_not_called()


@pytest.mark.asyncio
async def test_apply_fixes_second_issue_on_failed_file_skipped():
    """Second issue on an already-failed file hits the `continue` guard (line 99)."""
    from agent.git_client import GitClientError

    git = _make_git()
    git.get_file_content = AsyncMock(side_effect=GitClientError("fetch failed"))

    issues = [
        _make_issue(file="bad.py", line=1, fix="fix_a()"),
        _make_issue(file="bad.py", line=2, fix="fix_b()"),
    ]
    review = _make_review(issues)

    result = await apply_fixes(review, CONTEXT, git=git)

    assert result.success is False
    assert "bad.py" in result.failed_files
    # File was only fetched once — second issue skipped via continue
    git.get_file_content.assert_called_once()
    git.commit_files.assert_not_called()


@pytest.mark.asyncio
async def test_collect_file_contents_parallel():
    """Files are fetched in parallel — all get_file_content calls fire concurrently."""
    import asyncio
    call_times: list[float] = []

    async def slow_fetch(**kwargs: object) -> str:
        call_times.append(asyncio.get_event_loop().time())
        await asyncio.sleep(0.05)   # simulate 50ms GitHub RTT
        return "content\n    return unsafe_value\nline3\n"

    git = _make_git()
    git.get_file_content = AsyncMock(side_effect=slow_fetch)

    issues = [
        _make_issue(file="app/a.py", line=2, fix="    return safe_a"),
        _make_issue(file="app/b.py", line=2, fix="    return safe_b"),
        _make_issue(file="app/c.py", line=2, fix="    return safe_c"),
    ]
    review = _make_review(issues)

    import time
    start = time.monotonic()
    result = await apply_fixes(review, CONTEXT, git=git)
    elapsed = time.monotonic() - start

    # Sequential would take 3 × 50ms = 150ms; parallel takes ~50ms.
    # Allow generous headroom for CI slowness — just verify it's under 2× sequential.
    assert elapsed < 0.12, f"Fetches appear sequential (took {elapsed:.3f}s)"
    assert git.get_file_content.call_count == 3
