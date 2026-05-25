"""
agent/main.py

FastAPI application — the webhook entry point for the coding agent.

Endpoints:
    POST /webhook   — called by Harness CI step for every PR event
    GET  /health    — liveness probe for Kubernetes / Harness infrastructure

Startup / shutdown:
    - Builds shared client instances once on startup (lifespan context).
    - Closes HTTP connections cleanly on shutdown.

Security:
    - Every /webhook request is validated with HMAC-SHA256 against WEBHOOK_SECRET.
    - Invalid signatures are rejected with HTTP 401 before any processing.

Error contract:
    - /webhook always returns HTTP 200 (Harness retries on non-200).
    - Internal errors are captured and returned as {"status": "error", ...}.
    - Exceptions are never allowed to propagate to FastAPI's default handler.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent.git_client import GitClient, GitClientSettings, build_review_comment
from agent.harness_client import HarnessClient, HarnessClientSettings
from agent.models import FixStrategy, PatchResult, ReviewResult, Verdict, WebhookPayload, WebhookResponse
from agent.patcher import PatchContext, apply_fixes
from agent.reviewer import ReviewContext, ReviewerError, review_diff
from agent.supervisor import SupervisorError, SupervisorVerdict, supervise

logger = logging.getLogger(__name__)

# PR author associations that are allowed to trigger an auto-fix commit.
# External contributors (CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, NONE, …) get
# a comment-only review — they cannot have the agent push code on their behalf.
_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


# ── Settings (loaded from environment / .env) ─────────────────────────────────


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""  # optional — only needed for Opus supervisor
    github_token: str
    github_repo: str
    webhook_secret: str

    harness_api_key: str
    harness_account_id: str
    harness_org_id: str
    harness_project_id: str
    harness_pipeline_id: str

    log_level: str = "INFO"


# ── Application state ─────────────────────────────────────────────────────────


class AppState:
    """Holds shared client instances created once at startup."""
    git: GitClient
    harness: HarnessClient
    settings: Settings


state = AppState()


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build clients on startup, close connections on shutdown."""
    cfg = Settings()
    state.settings = cfg

    logging.basicConfig(
        level=cfg.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    state.git = GitClient(
        GitClientSettings(token=cfg.github_token, default_repo=cfg.github_repo)
    )
    state.harness = HarnessClient(
        HarnessClientSettings(
            api_key=cfg.harness_api_key,
            account_id=cfg.harness_account_id,
            org_id=cfg.harness_org_id,
            project_id=cfg.harness_project_id,
            pipeline_id=cfg.harness_pipeline_id,
        )
    )

    logger.info("Coding agent started")
    yield

    await state.harness.aclose()
    logger.info("Coding agent shut down")


# ── FastAPI app ───────────────────────────────────────────────────────────────


app = FastAPI(
    title="Coding Agent",
    description="AI-powered code review and auto-fix agent for Harness CI/CD",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Security ──────────────────────────────────────────────────────────────────


def _verify_signature(body: bytes, signature_header: str, secret: str) -> bool:
    """
    Validate HMAC-SHA256 signature from Harness webhook call.

    Expected header format: "sha256=<hex-digest>"
    Uses secrets.compare_digest to prevent timing attacks.
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return secrets.compare_digest(expected, provided)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is running."""
    return {"status": "ok"}


@app.post("/webhook", response_model=WebhookResponse)
async def webhook(
    request: Request,
    x_harness_signature: str = Header(default=""),
) -> WebhookResponse:
    """
    Main webhook handler called by the Harness CI step.

    Flow:
      1. Validate HMAC signature.
      2. Parse WebhookPayload.
      3. Fetch PR diff from GitHub.
      4. Call Claude for code review.
      5a. If verdict == APPROVE   → post approval comment, return.
      5b. If strategy == COMMENT_ONLY → post issues comment, return.
      5c. If strategy == AUTO_FIX    → apply fixes, push commit, post comment,
                                       trigger Harness pipeline re-run.

    Always returns HTTP 200. Errors are captured inside WebhookResponse.
    """
    # ── Step 1: Signature validation ──────────────────────────────────────────
    body = await request.body()
    if not _verify_signature(body, x_harness_signature, state.settings.webhook_secret):
        logger.warning("Webhook signature validation failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # ── Step 2: Parse payload ─────────────────────────────────────────────────
    try:
        payload = WebhookPayload.model_validate_json(body)
    except Exception as exc:
        logger.error("Failed to parse webhook payload: %s", exc)
        return WebhookResponse.error_response(f"Invalid payload: {exc}")

    # ── Repo allowlist — reject requests targeting repos other than ours ──────
    if payload.repo.lower() != state.settings.github_repo.lower():
        logger.warning(
            "Webhook rejected: repo %r not allowed (expected %r)",
            payload.repo,
            state.settings.github_repo,
        )
        return WebhookResponse.error_response("Repository not allowed")

    # ── Resolve pr_sha if not provided by trigger ─────────────────────────────
    if not payload.pr_sha:
        try:
            payload.pr_sha = await state.git.get_pr_head_sha(
                repo=payload.repo, pr_number=payload.pr_number_int
            )
            logger.info("Resolved pr_sha from GitHub: %s", payload.pr_sha)
        except Exception as exc:
            logger.warning("Could not resolve pr_sha: %s — continuing without it", exc)

    log_ctx = {"repo": payload.repo, "pr": payload.pr_number, "sha": payload.pr_sha}
    logger.info("Webhook received", extra=log_ctx)

    # ── Step 3: Fetch PR diff ─────────────────────────────────────────────────
    try:
        diff = await state.git.get_pr_diff(
            repo=payload.repo,
            pr_number=payload.pr_number_int,
        )
    except Exception as exc:
        logger.error("Failed to fetch PR diff: %s", exc, extra=log_ctx)
        return WebhookResponse.error_response(f"Could not fetch diff: {exc}")

    if not diff.strip():
        logger.info("PR has no diff — approving automatically", extra=log_ctx)
        await _post_comment(payload, "No code changes detected. Approved automatically.", fix_sha=None, issues=[])
        empty_review = ReviewResult(verdict=Verdict.APPROVE, summary="No changes.", issues=[])
        return WebhookResponse.from_review(empty_review)

    # ── Step 4: Code review ───────────────────────────────────────────────────
    review_ctx = ReviewContext(
        repo=payload.repo,
        pr_number=payload.pr_number,
        pr_branch=payload.pr_branch,
        pr_sha=payload.pr_sha,
    )
    try:
        review = await review_diff(diff, review_ctx)
    except ReviewerError as exc:
        logger.error("Reviewer failed: %s", exc, extra=log_ctx)
        await _post_comment(
            payload,
            f"Code review failed due to an internal error: {exc}\n\nPlease review manually.",
            fix_sha=None,
            issues=[],
        )
        return WebhookResponse.error_response(f"Reviewer error: {exc}")

    logger.info(
        "Review complete: verdict=%s errors=%d warnings=%d",
        review.verdict.value,
        review.error_count,
        review.warning_count,
        extra=log_ctx,
    )

    strategy = review.fix_strategy()

    # ── Step 5a: Approve — no issues, skip supervision ────────────────────────
    if strategy == FixStrategy.SKIP:
        await _post_comment(payload, review.summary, fix_sha=None, issues=review.issues, verdict=review.verdict)
        return WebhookResponse.from_review(review)

    # ── Step 5b: Comment only — no fixable issues, skip supervision ───────────
    if strategy == FixStrategy.COMMENT_ONLY:
        await _post_comment(payload, review.summary, fix_sha=None, issues=review.issues, verdict=review.verdict)
        return WebhookResponse.from_review(review)

    # ── Step 5c: Opus supervises only when Reviewer requests it ──────────────
    # Cost control: Opus is called only when needs_supervision is True.
    if review.needs_supervision:
        logger.info(
            "Reviewer requested supervision: %s",
            review.supervision_reason,
            extra=log_ctx,
        )
        try:
            supervision = await supervise(diff, review)
            logger.info(
                "Opus supervision verdict: %s — %s",
                supervision.verdict.value,
                supervision.reasoning,
                extra=log_ctx,
            )
        except SupervisorError as exc:
            # Supervision failed — fall back to comment-only to be safe
            logger.error("Supervisor failed: %s — falling back to comment-only", exc, extra=log_ctx)
            await _post_comment(
                payload,
                f"{review.summary}\n\n⚠️ Supervision unavailable — auto-fix skipped.",
                fix_sha=None,
                issues=review.issues,
                verdict=review.verdict,
            )
            return WebhookResponse.from_review(review)

        # ── Step 5c-i: Opus disagrees — escalate to human ────────────────────
        if supervision.needs_human:
            concerns_text = "\n".join(f"- {c}" for c in supervision.concerns)
            escalation_msg = (
                f"{review.summary}\n\n"
                f"⚠️ **Supervisor escalation** — Opus flagged a serious disagreement. "
                f"A human should review before any fix is applied.\n\n"
                f"**Opus reasoning:** {supervision.reasoning}\n"
                + (f"\n**Concerns:**\n{concerns_text}" if concerns_text else "")
            )
            await _post_comment(payload, escalation_msg, fix_sha=None, issues=review.issues, verdict=review.verdict)
            logger.warning("Opus escalated PR %s — human review required", payload.pr_number, extra=log_ctx)
            return WebhookResponse.from_review(review)

        # ── Step 5c-ii: Opus overrides to comment-only ───────────────────────
        if supervision.verdict == SupervisorVerdict.OVERRIDE_TO_COMMENT:
            override_msg = (
                f"{review.summary}\n\n"
                f"ℹ️ **Auto-fix skipped** — Opus found the proposed fixes risky. "
                f"Please apply fixes manually.\n\n"
                f"**Opus reasoning:** {supervision.reasoning}"
            )
            await _post_comment(payload, override_msg, fix_sha=None, issues=review.issues, verdict=review.verdict)
            return WebhookResponse.from_review(review)

    # ── Step 5d: Author trust gate ────────────────────────────────────────────
    # Auto-fix writes code and pushes a commit; restrict to trusted contributors.
    try:
        association = await state.git.get_pr_author_association(
            repo=payload.repo, pr_number=payload.pr_number_int
        )
    except Exception as exc:
        logger.warning(
            "Could not fetch author association for %s #%s: %s — skipping auto-fix",
            payload.repo, payload.pr_number, exc,
            extra=log_ctx,
        )
        association = "NONE"

    if association not in _TRUSTED_ASSOCIATIONS:
        logger.info(
            "PR author association %r is not trusted — downgrading auto-fix to comment-only",
            association,
            extra=log_ctx,
        )
        await _post_comment(
            payload,
            f"{review.summary}\n\n"
            f"> Auto-fix skipped — contributor level `{association}` is not trusted "
            f"for automated commits. Please apply the suggested fixes manually.",
            fix_sha=None,
            issues=review.issues,
            verdict=review.verdict,
        )
        return WebhookResponse.from_review(review)

    # ── Step 5e: Proceed with auto-fix ───────────────────────────────────────
    # Either supervision approved or it was not requested; author is trusted.
    patch_ctx = PatchContext.from_payload(payload)
    patch = await apply_fixes(review, patch_ctx, git=state.git)

    if patch.success and patch.commit_sha:
        await _retrigger_pipeline(payload, patch.commit_sha)

    await _post_comment(
        payload,
        review.summary,
        fix_sha=patch.commit_sha if patch.success else None,
        issues=review.issues,
        verdict=review.verdict,
    )

    return WebhookResponse.from_review(review, patch)


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _post_comment(
    payload: WebhookPayload,
    summary: str,
    fix_sha: str | None,
    issues: list,
    verdict: object | None = None,
) -> None:
    """Post the review comment on the PR. Logs and swallows errors — never raises."""
    if verdict == Verdict.APPROVE:
        verdict_emoji = "\u2705"   # ✅
    elif verdict == Verdict.REQUEST_CHANGES:
        verdict_emoji = "\u274c"   # ❌
    else:
        verdict_emoji = "\u26a0\ufe0f"  # ⚠️  (error / unknown)

    try:
        body = build_review_comment(
            review_summary=summary,
            issues=issues,
            fix_commit_sha=fix_sha,
            verdict_emoji=verdict_emoji,
        )
        await state.git.post_pr_comment(
            repo=payload.repo,
            pr_number=payload.pr_number_int,
            body=body,
        )
    except Exception as exc:
        logger.error(
            "Failed to post PR comment on %s #%s: %s",
            payload.repo,
            payload.pr_number,
            exc,
        )


async def _retrigger_pipeline(
    payload: WebhookPayload,
    fix_commit_sha: str,
) -> None:
    """Re-trigger the Harness pipeline after a fix commit. Logs and swallows errors."""
    try:
        execution_id = await state.harness.trigger_pipeline(
            pr_number=payload.pr_number,
            pr_branch=payload.pr_branch,
            pr_sha=fix_commit_sha,
        )
        execution_url = state.harness.get_execution_url(execution_id)
        logger.info(
            "Pipeline re-triggered: %s — %s", execution_id, execution_url
        )

        # Append execution URL to the PR comment (best-effort, separate call)
        await state.git.post_pr_comment(
            repo=payload.repo,
            pr_number=payload.pr_number_int,
            body=(
                f"Pipeline re-triggered after fix commit `{fix_commit_sha[:8]}`.\n"
                f"[Watch execution]({execution_url})"
            ),
        )
    except Exception as exc:
        logger.error(
            "Failed to re-trigger pipeline for %s #%s: %s",
            payload.repo,
            payload.pr_number,
            exc,
        )
