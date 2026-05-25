"""
agent/models.py

All Pydantic models used across the coding agent.
Every piece of data crossing module boundaries must use one of these models.

Model hierarchy:
  Incoming webhook  → WebhookPayload
  Claude raw output → RawReviewResponse (internal, for parsing only)
  Parsed review     → ReviewResult, Issue
  Fix output        → PatchResult
  Final response    → WebhookResponse
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ── Enums ────────────────────────────────────────────────────────────────────


class Verdict(str, Enum):
    """Top-level decision from the code reviewer."""
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"


class Severity(str, Enum):
    """Severity level of a single issue found during review."""
    ERROR = "error"      # Must be fixed before merge (e.g. security flaw, crash)
    WARNING = "warning"  # Should be fixed (e.g. bad practice, performance)
    INFO = "info"        # Suggestion, style, or nitpick


class FixStrategy(str, Enum):
    """How the agent will handle a review that requests changes."""
    AUTO_FIX = "auto_fix"        # Agent pushes a fix commit automatically
    COMMENT_ONLY = "comment_only"  # Agent posts a comment but does not commit
    SKIP = "skip"                # Review passed; no action needed


# ── Incoming webhook payload ──────────────────────────────────────────────────


class WebhookPayload(BaseModel):
    """
    Payload sent by the Harness CI step to POST /webhook.

    All fields come from Harness trigger expressions:
      pr_number  → <+trigger.prNumber>
      pr_branch  → <+trigger.sourceBranch>
      pr_sha     → <+trigger.commitSha>
      repo       → secret: github_repo
    """
    pr_number: str = Field(..., description="PR number as a string, e.g. '42'")
    pr_branch: str = Field(..., description="Head branch name of the PR")
    pr_sha: str = Field(..., description="Head commit SHA at trigger time (40 chars)")
    repo: str = Field(..., description="GitHub repo in owner/name format")

    @field_validator("pr_sha")
    @classmethod
    def sha_looks_valid(cls, v: str) -> str:
        if len(v) < 7 or not all(c in "0123456789abcdefABCDEF" for c in v):
            raise ValueError(f"pr_sha does not look like a git SHA: {v!r}")
        return v.lower()

    @field_validator("repo")
    @classmethod
    def repo_has_slash(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(f"repo must be in owner/name format, got: {v!r}")
        return v

    @property
    def pr_number_int(self) -> int:
        return int(self.pr_number)


# ── Claude review output ──────────────────────────────────────────────────────


class Issue(BaseModel):
    """
    A single code problem identified by Claude during review.

    The `fix` field is Claude's suggested replacement — it is the raw string
    that patcher.py will attempt to apply. It may be None if Claude identified
    a problem but could not suggest a mechanical fix.
    """
    file: str = Field(..., description="Relative path to the affected file")
    line: int | None = Field(None, description="Line number (1-indexed), if known")
    severity: Severity
    message: str = Field(..., description="Human-readable description of the issue")
    fix: str | None = Field(
        None,
        description="Claude's suggested replacement code, or None if no fix is possible",
    )

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Issue message must not be empty")
        return v.strip()


class ReviewResult(BaseModel):
    """
    Parsed output from Claude after reviewing a PR diff.

    This is the authoritative representation of a review inside the agent.
    It is produced by reviewer.py and consumed by patcher.py and main.py.
    """
    verdict: Verdict
    issues: list[Issue] = Field(default_factory=list)
    summary: str = Field(..., description="One-paragraph review summary for the PR comment")
    needs_supervision: bool = Field(
        False,
        description="True if Opus should double-check before any auto-fix is applied",
    )
    supervision_reason: str | None = Field(
        None,
        description="Why supervision is needed (required when needs_supervision is True)",
    )

    @field_validator("summary")
    @classmethod
    def summary_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Review summary must not be empty")
        return v.strip()

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.WARNING)

    @property
    def has_fixable_issues(self) -> bool:
        """True if at least one issue has a non-None fix suggestion."""
        return any(i.fix is not None for i in self.issues)

    def fix_strategy(self) -> FixStrategy:
        """
        Decide what the agent should do with this review result.

        Rules (in priority order):
          1. APPROVE  → SKIP
          2. REQUEST_CHANGES + at least one fixable issue → AUTO_FIX
          3. REQUEST_CHANGES + no fixable issues           → COMMENT_ONLY
        """
        if self.verdict == Verdict.APPROVE:
            return FixStrategy.SKIP
        if self.has_fixable_issues:
            return FixStrategy.AUTO_FIX
        return FixStrategy.COMMENT_ONLY


# ── Patch / fix output ────────────────────────────────────────────────────────


class PatchResult(BaseModel):
    """
    Result produced by patcher.py after attempting to apply fixes.

    `commit_sha` is populated only when the fix commit was successfully pushed.
    `failed_files` lists files where the patch could not be applied cleanly.
    """
    success: bool
    commit_sha: str | None = Field(None, description="SHA of the fix commit, if pushed")
    patched_files: list[str] = Field(
        default_factory=list,
        description="Files that were successfully patched",
    )
    failed_files: list[str] = Field(
        default_factory=list,
        description="Files where patch application failed",
    )
    error_message: str | None = Field(
        None,
        description="Top-level error message if the entire patch attempt failed",
    )


# ── Webhook response ──────────────────────────────────────────────────────────


class WebhookResponse(BaseModel):
    """
    Response returned by POST /webhook to the Harness CI step.

    Harness reads `fix_applied` to decide whether to wait and re-trigger.
    The agent always returns HTTP 200; errors are surfaced via `status`.
    """
    status: Literal["ok", "error"]
    verdict: Verdict | None = None
    fix_applied: bool = False
    fix_commit_sha: str | None = None
    summary: str | None = None
    error: str | None = None

    @classmethod
    def from_review(
        cls,
        review: ReviewResult,
        patch: PatchResult | None = None,
    ) -> "WebhookResponse":
        """Convenience constructor from a completed review (and optional patch)."""
        fix_applied = patch is not None and patch.success and patch.commit_sha is not None
        return cls(
            status="ok",
            verdict=review.verdict,
            fix_applied=fix_applied,
            fix_commit_sha=patch.commit_sha if fix_applied else None,
            summary=review.summary,
        )

    @classmethod
    def error_response(cls, message: str) -> "WebhookResponse":
        """Convenience constructor for internal error responses."""
        return cls(status="error", error=message)
