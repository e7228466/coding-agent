"""
agent/git_client.py

Thin async wrapper around PyGithub for all GitHub operations needed by the agent.

Public API:
    GitClient                  — main class, inject into patcher / main
      .get_pr_diff()           — fetch the unified diff of a PR
      .get_file_content()      — fetch a single file at a given ref
      .commit_files()          — push one or more file changes as a single commit
      .post_pr_comment()       — post a markdown comment on a PR

All methods raise GitClientError on failure.
PyGithub is always run in a thread (asyncio.to_thread) — it is synchronous.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field

from github import Github, GithubException
from github.Repository import Repository

logger = logging.getLogger(__name__)


# ── Custom exception ──────────────────────────────────────────────────────────


class GitClientError(Exception):
    """Raised when any GitHub API operation fails."""


# ── Settings (injected from pydantic-settings in main.py) ────────────────────


@dataclass(frozen=True)
class GitClientSettings:
    token: str
    default_repo: str = ""          # owner/name; can be overridden per-call


# ── Main client ───────────────────────────────────────────────────────────────


class GitClient:
    """
    Async-safe GitHub client.

    All PyGithub calls are wrapped in asyncio.to_thread() because PyGithub
    uses the requests library which blocks the event loop.

    Usage:
        git = GitClient(GitClientSettings(token=os.environ["GITHUB_TOKEN"]))
        diff = await git.get_pr_diff(repo="acme/backend", pr_number=42)
    """

    def __init__(self, settings: GitClientSettings) -> None:
        self._settings = settings
        self._gh = Github(settings.token)

    # ── Public methods ────────────────────────────────────────────────────────

    async def get_pr_diff(self, *, repo: str, pr_number: int) -> str:
        """
        Return the unified diff of a pull request as a plain string.

        The diff is fetched file-by-file from the PR's changed files list
        and assembled into a single unified diff string that mirrors
        `git diff base...head` output.

        Args:
            repo:       Repository in owner/name format.
            pr_number:  PR number (integer).

        Returns:
            Unified diff string. Empty string if the PR has no changed files.

        Raises:
            GitClientError: On any GitHub API failure.
        """
        def _fetch() -> str:
            try:
                gh_repo = self._get_repo(repo)
                pr = gh_repo.get_pull(pr_number)
                files = pr.get_files()

                parts: list[str] = []
                for f in files:
                    if f.patch:   # binary files have no patch
                        parts.append(
                            f"--- a/{f.filename}\n"
                            f"+++ b/{f.filename}\n"
                            f"{f.patch}"
                        )
                return "\n".join(parts)
            except GithubException as exc:
                raise GitClientError(
                    f"Failed to fetch diff for PR #{pr_number} in {repo}: {exc}"
                ) from exc

        logger.info("Fetching diff for %s PR #%s", repo, pr_number)
        return await asyncio.to_thread(_fetch)

    async def get_file_content(self, *, repo: str, path: str, ref: str) -> str:
        """
        Return the decoded text content of a file at a given git ref.

        Args:
            repo:  Repository in owner/name format.
            path:  Relative file path, e.g. "app/auth.py".
            ref:   Branch name or commit SHA.

        Returns:
            File content as a UTF-8 string.

        Raises:
            GitClientError: If the file does not exist, is binary, or the
                            API call fails.
        """
        def _fetch() -> str:
            try:
                gh_repo = self._get_repo(repo)
                contents = gh_repo.get_contents(path, ref=ref)

                # get_contents can return a list for directories
                if isinstance(contents, list):
                    raise GitClientError(f"{path!r} is a directory, not a file")

                if contents.encoding != "base64":
                    raise GitClientError(
                        f"{path!r} has unsupported encoding: {contents.encoding}"
                    )

                return base64.b64decode(contents.content).decode("utf-8")

            except GithubException as exc:
                raise GitClientError(
                    f"Failed to fetch {path!r} at ref {ref!r} in {repo}: {exc}"
                ) from exc

        logger.debug("Fetching file %s@%s in %s", path, ref, repo)
        return await asyncio.to_thread(_fetch)

    async def commit_files(
        self,
        *,
        repo: str,
        branch: str,
        files: dict[str, str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        """
        Push one or more file changes as a single commit on the given branch.

        Files are updated sequentially. If any update fails, the operation
        aborts and raises GitClientError. Partial commits (some files updated,
        some not) may occur on failure — callers should log PatchResult details.

        Args:
            repo:         Repository in owner/name format.
            branch:       Branch to commit to (must already exist).
            files:        Mapping of relative file path → new full file content.
            message:      Commit message (should start with "[agent-fix]").
            author_name:  Git author name for the commit.
            author_email: Git author email for the commit.

        Returns:
            The SHA of the new commit.

        Raises:
            GitClientError: If any file update or the commit fails.
        """
        if not files:
            raise GitClientError("commit_files called with empty files dict")

        def _commit() -> str:
            try:
                gh_repo = self._get_repo(repo)
                last_sha = ""

                for path, new_content in files.items():
                    # Get current file SHA (needed by GitHub Contents API for updates)
                    try:
                        existing = gh_repo.get_contents(path, ref=branch)
                        if isinstance(existing, list):
                            raise GitClientError(f"{path!r} is a directory")
                        file_sha = existing.sha
                    except GithubException as exc:
                        raise GitClientError(
                            f"Cannot get current SHA for {path!r}: {exc}"
                        ) from exc

                    result = gh_repo.update_file(
                        path=path,
                        message=message,
                        content=new_content.encode("utf-8"),
                        sha=file_sha,
                        branch=branch,
                        author={
                            "name": author_name,
                            "email": author_email,
                        },
                    )
                    last_sha = result["commit"].sha
                    logger.debug("Updated %s → commit %s", path, last_sha)

                return last_sha

            except GithubException as exc:
                raise GitClientError(f"Commit failed in {repo}: {exc}") from exc

        logger.info(
            "Committing %d file(s) to %s/%s", len(files), repo, branch
        )
        return await asyncio.to_thread(_commit)

    async def get_pr_author_association(self, *, repo: str, pr_number: int) -> str:
        """
        Return the PR author's association to the repository.

        Possible values: OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | FIRST_TIMER |
                         FIRST_TIME_CONTRIBUTOR | MANNEQUIN | NONE

        Used by main.py to gate auto-fix on trusted contributors only.

        Raises:
            GitClientError: If the GitHub API call fails.
        """
        def _fetch() -> str:
            try:
                gh_repo = self._get_repo(repo)
                pr = gh_repo.get_pull(pr_number)
                return str(pr.author_association)
            except GithubException as exc:
                raise GitClientError(
                    f"Failed to fetch author association for PR #{pr_number} in {repo}: {exc}"
                ) from exc

        logger.debug("Fetching author association for %s PR #%s", repo, pr_number)
        return await asyncio.to_thread(_fetch)

    async def post_pr_comment(
        self,
        *,
        repo: str,
        pr_number: int,
        body: str,
    ) -> None:
        """
        Post a markdown comment on a pull request.

        Args:
            repo:       Repository in owner/name format.
            pr_number:  PR number.
            body:       Markdown comment body.

        Raises:
            GitClientError: If the API call fails.
        """
        def _post() -> None:
            try:
                gh_repo = self._get_repo(repo)
                pr = gh_repo.get_pull(pr_number)
                pr.create_issue_comment(body)
            except GithubException as exc:
                raise GitClientError(
                    f"Failed to post comment on PR #{pr_number} in {repo}: {exc}"
                ) from exc

        logger.info("Posting comment on %s PR #%s", repo, pr_number)
        await asyncio.to_thread(_post)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_repo(self, repo: str) -> Repository:
        """Resolve a repo string to a PyGithub Repository object."""
        try:
            return self._gh.get_repo(repo)
        except GithubException as exc:
            raise GitClientError(f"Cannot access repository {repo!r}: {exc}") from exc


# ── Comment builder ───────────────────────────────────────────────────────────


def build_review_comment(
    review_summary: str,
    issues: list,           # list[Issue] — avoid circular import with models
    fix_commit_sha: str | None,
    *,
    verdict_emoji: str = "",
) -> str:
    """
    Build the markdown body for the PR review comment posted by the agent.

    Args:
        review_summary:  One-paragraph summary from ReviewResult.
        issues:          List of Issue objects from ReviewResult.
        fix_commit_sha:  SHA of the fix commit, or None if no fix was pushed.
        verdict_emoji:   Optional emoji prefix for the header line.

    Returns:
        Formatted markdown string ready to post as a PR comment.
    """
    from agent.models import Severity

    SEVERITY_LABEL = {
        Severity.ERROR:   "Error",
        Severity.WARNING: "Warning",
        Severity.INFO:    "Info",
    }

    lines: list[str] = []

    header = f"{verdict_emoji} **Coding Agent Review**" if verdict_emoji else "**Coding Agent Review**"
    lines.append(header)
    lines.append("")
    lines.append(review_summary)

    if issues:
        lines.append("")
        lines.append("### Issues found")
        lines.append("")
        for issue in issues:
            label = SEVERITY_LABEL.get(issue.severity, issue.severity.value)
            location = f"`{issue.file}`"
            if issue.line:
                location += f" line {issue.line}"
            lines.append(f"- **{label}** — {location}: {issue.message}")

    if fix_commit_sha:
        lines.append("")
        lines.append(
            f"> Fix commit pushed automatically: `{fix_commit_sha[:8]}`  "
        )
        lines.append("> Please review the changes before merging.")
    else:
        lines.append("")
        lines.append(
            "> No automatic fix was applied. Please address the issues above manually."
        )

    lines.append("")
    lines.append("---")
    lines.append("*Posted by coding-agent*")

    return "\n".join(lines)
