"""
agent/patcher.py

Takes a ReviewResult and attempts to apply Claude's fix suggestions
to the actual files in the repository, then commits and pushes via git_client.

Public API:
    apply_fixes(review, payload, *, git) -> PatchResult

Internal helpers (all private):
    _apply_single_fix(issue, file_content) -> str | None
    _build_commit_message(review, patched_files) -> str
    _collect_file_contents(issues, git) -> dict[str, str]
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from agent.git_client import GitClient, GitClientError
from agent.models import Issue, PatchResult, ReviewResult, WebhookPayload

logger = logging.getLogger(__name__)

COMMIT_AUTHOR_NAME = "coding-agent[bot]"
COMMIT_AUTHOR_EMAIL = "coding-agent@users.noreply.github.com"


# ── Context passed into apply_fixes ──────────────────────────────────────────


@dataclass
class PatchContext:
    """Everything the patcher needs beyond the ReviewResult."""
    repo: str
    pr_branch: str
    pr_sha: str

    @classmethod
    def from_payload(cls, payload: WebhookPayload) -> "PatchContext":
        return cls(
            repo=payload.repo,
            pr_branch=payload.pr_branch,
            pr_sha=payload.pr_sha,
        )


# ── Public entry point ────────────────────────────────────────────────────────


async def apply_fixes(
    review: ReviewResult,
    context: PatchContext,
    *,
    git: GitClient,
) -> PatchResult:
    """
    Apply all fixable issues from a ReviewResult and push a single commit.

    Strategy:
      1. Collect all unique files that have fixable issues.
      2. Fetch the current content of each file from the PR branch.
      3. For each fixable issue, attempt a string-replacement patch.
      4. If at least one patch succeeded, push a single commit with all changes.
      5. Return a PatchResult describing what worked and what didn't.

    A file is skipped (added to failed_files) if:
      - It cannot be fetched from GitHub (deleted, binary, etc.)
      - The fix string is not found in the current file content (stale fix)

    Args:
        review:  A ReviewResult with verdict == REQUEST_CHANGES.
        context: Repo and branch info from the webhook payload.
        git:     GitClient instance (injected; mock in tests).

    Returns:
        PatchResult — always returned, never raises. Errors are captured inside.
    """
    fixable = [i for i in review.issues if i.fix is not None]

    if not fixable:
        logger.info("No fixable issues — nothing to patch")
        return PatchResult(success=False, error_message="No fixable issues in review")

    # Step 1: fetch current file contents for all affected files
    unique_files = list(dict.fromkeys(i.file for i in fixable))  # preserve order, dedupe
    file_contents = await _collect_file_contents(unique_files, context, git=git)

    # Step 2: apply fixes file by file
    patched: dict[str, str] = {}   # file path → new content
    failed: list[str] = []

    for issue in fixable:
        path = issue.file

        if path in failed:
            continue  # already failed for this file, skip further issues on it

        if path not in file_contents:
            logger.warning("Cannot patch %s — file fetch failed", path)
            if path not in failed:
                failed.append(path)
            continue

        original = file_contents[path]
        working = patched.get(path, original)  # apply on top of earlier patches

        patched_content = _apply_single_fix(issue, working)
        if patched_content is None:
            logger.warning(
                "Fix for %s:%s not applicable — original string not found in file",
                path,
                issue.line,
            )
            if path not in failed:
                failed.append(path)
            continue

        patched[path] = patched_content
        logger.info("Patched %s (line ~%s)", path, issue.line)

    if not patched:
        return PatchResult(
            success=False,
            failed_files=failed,
            error_message="All fix attempts failed — no files were changed",
        )

    # Step 3: commit and push
    commit_message = _build_commit_message(review, list(patched))
    try:
        commit_sha = await git.commit_files(
            repo=context.repo,
            branch=context.pr_branch,
            files=patched,
            message=commit_message,
            author_name=COMMIT_AUTHOR_NAME,
            author_email=COMMIT_AUTHOR_EMAIL,
        )
    except GitClientError as exc:
        logger.error("Failed to push fix commit: %s", exc)
        return PatchResult(
            success=False,
            patched_files=list(patched),
            failed_files=failed,
            error_message=f"Commit push failed: {exc}",
        )

    logger.info(
        "Fix commit pushed: %s (%d file(s) patched, %d failed)",
        commit_sha,
        len(patched),
        len(failed),
    )
    return PatchResult(
        success=True,
        commit_sha=commit_sha,
        patched_files=list(patched),
        failed_files=failed,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _apply_single_fix(issue: Issue, file_content: str) -> str | None:
    """
    Attempt to apply issue.fix to file_content by replacing the target line.

    Requires issue.line to identify the line to replace. If the fix is already
    present verbatim, or if the line number is missing or out of range, returns
    None (conservative — skip rather than corrupt).
    """
    assert issue.fix is not None  # caller guarantees this

    # If the fix is already present verbatim, skip (idempotent)
    if issue.fix in file_content:
        logger.debug("Fix for %s already present — skipping", issue.file)
        return None

    if issue.line is None:
        logger.debug("No line hint for fix in %s — skipping", issue.file)
        return None

    lines = file_content.splitlines(keepends=True)
    target_idx = issue.line - 1  # convert to 0-indexed

    if not (0 <= target_idx < len(lines)):
        logger.debug(
            "Line %s out of range for %s (%d lines) — skipping",
            issue.line,
            issue.file,
            len(lines),
        )
        return None

    # Replace the target line, preserving the original line ending
    ending = "\n" if lines[target_idx].endswith("\n") else ""
    lines[target_idx] = issue.fix + ending
    return "".join(lines)


async def _collect_file_contents(
    paths: list[str],
    context: PatchContext,
    *,
    git: GitClient,
) -> dict[str, str]:
    """
    Fetch the current content of each file from the PR branch in parallel.

    Uses asyncio.gather so all GitHub API calls fire simultaneously instead
    of sequentially — N files take ~1 RTT instead of N RTTs.

    Files that fail to fetch are silently omitted — callers check membership.
    """
    async def _fetch_one(path: str) -> tuple[str, str | None]:
        try:
            content = await git.get_file_content(
                repo=context.repo,
                path=path,
                ref=context.pr_branch,
            )
            return path, content
        except GitClientError as exc:
            logger.warning("Could not fetch %s: %s", path, exc)
            return path, None

    results = await asyncio.gather(*[_fetch_one(p) for p in paths])
    return {path: content for path, content in results if content is not None}


def _build_commit_message(review: ReviewResult, patched_files: list[str]) -> str:
    """
    Build the commit message for the agent fix commit.

    The [agent-fix] prefix is load-bearing — the Harness pipeline guard step
    uses it to detect agent-generated commits and skip the review loop.
    See harness/pipeline.yaml § check-agent-fix-commit.
    """
    files_summary = ", ".join(patched_files[:3])
    if len(patched_files) > 3:
        files_summary += f" (+{len(patched_files) - 3} more)"

    error_count = review.error_count
    warning_count = review.warning_count
    issue_summary = f"{error_count} error(s), {warning_count} warning(s)"

    return (
        f"[agent-fix] Auto-fix {len(patched_files)} file(s): {files_summary}\n\n"
        f"Issues addressed: {issue_summary}\n"
        f"Review summary: {review.summary}\n\n"
        f"Generated by coding-agent. Review before merging."
    )
