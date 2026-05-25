"""
tests/test_git_client.py

Unit tests for agent/git_client.py.
All PyGithub objects are mocked — no real API calls are made.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest
from github import GithubException

from agent.git_client import GitClient, GitClientError, GitClientSettings, build_review_comment
from agent.models import Issue, ReviewResult, Severity, Verdict


# ── Fixtures ──────────────────────────────────────────────────────────────────

SETTINGS = GitClientSettings(token="ghp_fake", default_repo="acme/backend")


def _make_client(mock_gh: MagicMock) -> GitClient:
    client = GitClient(SETTINGS)
    client._gh = mock_gh
    return client


def _make_gh_file(content: str, sha: str = "abc123", encoding: str = "base64") -> MagicMock:
    f = MagicMock()
    f.encoding = encoding
    f.content = base64.b64encode(content.encode()).decode()
    f.sha = sha
    return f


def _make_pr_file(filename: str, patch: str | None) -> MagicMock:
    f = MagicMock()
    f.filename = filename
    f.patch = patch
    return f


# ── get_pr_diff ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pr_diff_returns_combined_diff():
    mock_gh = MagicMock()
    pr_file = _make_pr_file("app/auth.py", "@@ -1 +1 @@\n-old\n+new")
    mock_gh.get_repo.return_value.get_pull.return_value.get_files.return_value = [pr_file]

    client = _make_client(mock_gh)
    diff = await client.get_pr_diff(repo="acme/backend", pr_number=42)

    assert "--- a/app/auth.py" in diff
    assert "+++ b/app/auth.py" in diff
    assert "-old" in diff
    assert "+new" in diff


@pytest.mark.asyncio
async def test_get_pr_diff_skips_binary_files():
    mock_gh = MagicMock()
    binary_file = _make_pr_file("image.png", patch=None)   # binary → no patch
    text_file = _make_pr_file("app/main.py", "@@ -1 +1 @@\n-x\n+y")
    mock_gh.get_repo.return_value.get_pull.return_value.get_files.return_value = [
        binary_file, text_file
    ]

    client = _make_client(mock_gh)
    diff = await client.get_pr_diff(repo="acme/backend", pr_number=1)

    assert "image.png" not in diff
    assert "app/main.py" in diff


@pytest.mark.asyncio
async def test_get_pr_diff_github_error_raises():
    mock_gh = MagicMock()
    mock_gh.get_repo.side_effect = GithubException(404, "not found", {})

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="Cannot access repository"):
        await client.get_pr_diff(repo="acme/backend", pr_number=1)


@pytest.mark.asyncio
async def test_get_pr_diff_get_pull_error_raises():
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value.get_pull.side_effect = GithubException(500, "server error", {})

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="Failed to fetch diff"):
        await client.get_pr_diff(repo="acme/backend", pr_number=1)


# ── get_file_content ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_file_content_returns_decoded_text():
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value.get_contents.return_value = _make_gh_file("hello world\n")

    client = _make_client(mock_gh)
    content = await client.get_file_content(
        repo="acme/backend", path="app/auth.py", ref="main"
    )
    assert content == "hello world\n"


@pytest.mark.asyncio
async def test_get_file_content_directory_raises():
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value.get_contents.return_value = [MagicMock(), MagicMock()]

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="is a directory"):
        await client.get_file_content(repo="acme/backend", path="app/", ref="main")


@pytest.mark.asyncio
async def test_get_file_content_unsupported_encoding_raises():
    mock_gh = MagicMock()
    f = _make_gh_file("data", encoding="utf-8")  # GitHub only returns base64
    mock_gh.get_repo.return_value.get_contents.return_value = f

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="unsupported encoding"):
        await client.get_file_content(repo="acme/backend", path="f.py", ref="main")


@pytest.mark.asyncio
async def test_get_file_content_inner_github_error_raises():
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value.get_contents.side_effect = GithubException(404, "not found", {})

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="Failed to fetch"):
        await client.get_file_content(repo="acme/backend", path="missing.py", ref="main")


# ── commit_files ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_commit_files_returns_commit_sha():
    mock_gh = MagicMock()
    existing = _make_gh_file("old content", sha="file-sha-001")
    mock_gh.get_repo.return_value.get_contents.return_value = existing
    mock_gh.get_repo.return_value.update_file.return_value = {
        "commit": MagicMock(sha="new-commit-sha")
    }

    client = _make_client(mock_gh)
    sha = await client.commit_files(
        repo="acme/backend",
        branch="feat/fix",
        files={"app/auth.py": "new content\n"},
        message="[agent-fix] Fix injection",
        author_name="coding-agent[bot]",
        author_email="coding-agent@users.noreply.github.com",
    )
    assert sha == "new-commit-sha"


@pytest.mark.asyncio
async def test_commit_files_empty_files_raises():
    client = GitClient(SETTINGS)
    with pytest.raises(GitClientError, match="empty files dict"):
        await client.commit_files(
            repo="acme/backend", branch="main", files={},
            message="msg", author_name="bot", author_email="bot@x.com",
        )


@pytest.mark.asyncio
async def test_commit_files_path_is_directory_raises():
    mock_gh = MagicMock()
    # get_contents returns a list → path is a directory
    mock_gh.get_repo.return_value.get_contents.return_value = [MagicMock(), MagicMock()]

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="is a directory"):
        await client.commit_files(
            repo="acme/backend", branch="feat/fix",
            files={"app/": "content"},
            message="[agent-fix] x",
            author_name="bot", author_email="bot@x.com",
        )


@pytest.mark.asyncio
async def test_commit_files_get_sha_github_error_raises():
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value.get_contents.side_effect = GithubException(500, "error", {})

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="Cannot get current SHA"):
        await client.commit_files(
            repo="acme/backend", branch="feat/fix",
            files={"app/auth.py": "new content\n"},
            message="[agent-fix] x",
            author_name="bot", author_email="bot@x.com",
        )


@pytest.mark.asyncio
async def test_commit_files_update_failure_raises():
    mock_gh = MagicMock()
    existing = _make_gh_file("old", sha="sha-001")
    mock_gh.get_repo.return_value.get_contents.return_value = existing
    mock_gh.get_repo.return_value.update_file.side_effect = GithubException(
        422, "conflict", {}
    )

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="Commit failed"):
        await client.commit_files(
            repo="acme/backend", branch="feat/fix",
            files={"app/auth.py": "new\n"},
            message="[agent-fix] x",
            author_name="bot", author_email="bot@x.com",
        )


# ── get_pr_author_association ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pr_author_association_returns_value():
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value.get_pull.return_value.author_association = "OWNER"

    client = _make_client(mock_gh)
    assoc = await client.get_pr_author_association(repo="acme/backend", pr_number=42)
    assert assoc == "OWNER"


@pytest.mark.asyncio
async def test_get_pr_author_association_github_error_raises():
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value.get_pull.side_effect = GithubException(404, "not found", {})

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="author association"):
        await client.get_pr_author_association(repo="acme/backend", pr_number=1)


# ── post_pr_comment ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_pr_comment_calls_create_issue_comment():
    mock_gh = MagicMock()
    mock_pr = MagicMock()
    mock_gh.get_repo.return_value.get_pull.return_value = mock_pr

    client = _make_client(mock_gh)
    await client.post_pr_comment(repo="acme/backend", pr_number=42, body="hello")

    mock_pr.create_issue_comment.assert_called_once_with("hello")


@pytest.mark.asyncio
async def test_post_pr_comment_github_error_raises():
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value.get_pull.side_effect = GithubException(404, "not found", {})

    client = _make_client(mock_gh)
    with pytest.raises(GitClientError, match="Failed to post comment"):
        await client.post_pr_comment(repo="acme/backend", pr_number=1, body="hi")


# ── build_review_comment ──────────────────────────────────────────────────────

def _make_review_with_issues() -> tuple[ReviewResult, list[Issue]]:
    issues = [
        Issue(file="app/auth.py", line=10, severity=Severity.ERROR,
              message="SQL injection risk", fix="safe_query()"),
        Issue(file="app/utils.py", line=None, severity=Severity.WARNING,
              message="Unused import", fix=None),
    ]
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Two issues found.",
        issues=issues,
    )
    return review, issues


def test_build_review_comment_contains_summary():
    review, issues = _make_review_with_issues()
    body = build_review_comment(review.summary, issues, fix_commit_sha=None)
    assert "Two issues found." in body


def test_build_review_comment_lists_issues():
    review, issues = _make_review_with_issues()
    body = build_review_comment(review.summary, issues, fix_commit_sha=None)
    assert "app/auth.py" in body
    assert "SQL injection risk" in body
    assert "Unused import" in body


def test_build_review_comment_with_fix_sha():
    review, issues = _make_review_with_issues()
    body = build_review_comment(review.summary, issues, fix_commit_sha="deadbeef1234")
    assert "deadbeef" in body
    assert "pushed automatically" in body


def test_build_review_comment_without_fix_sha():
    review, issues = _make_review_with_issues()
    body = build_review_comment(review.summary, issues, fix_commit_sha=None)
    assert "manually" in body


def test_build_review_comment_no_issues():
    body = build_review_comment("All good.", [], fix_commit_sha=None)
    assert "Issues found" not in body
    assert "All good." in body
