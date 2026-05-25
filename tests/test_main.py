"""
tests/test_main.py

Integration-level tests for agent/main.py.
Lifespan is exercised with all external clients mocked — no real API keys needed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agent.main import app
from agent.models import Issue, PatchResult, ReviewResult, Severity, Verdict

WEBHOOK_SECRET = "test-secret"

VALID_PAYLOAD: dict = {
    "repo": "acme/backend",
    "pr_number": "42",
    "pr_branch": "feat/login",
    "pr_sha": "abc1234deadbeef1",
}


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload_bytes(**overrides: object) -> bytes:
    return json.dumps({**VALID_PAYLOAD, **overrides}).encode()


@pytest.fixture
def client():
    """TestClient with all external dependencies mocked so no real env vars are needed."""
    mock_settings = MagicMock()
    mock_settings.log_level = "INFO"
    mock_settings.webhook_secret = WEBHOOK_SECRET
    mock_settings.github_token = "fake-token"
    mock_settings.github_repo = "acme/backend"
    mock_settings.anthropic_api_key = "fake-key"
    mock_settings.harness_api_key = "fake-key"
    mock_settings.harness_account_id = "acc"
    mock_settings.harness_org_id = "org"
    mock_settings.harness_project_id = "proj"
    mock_settings.harness_pipeline_id = "pipe"

    mock_git = MagicMock()
    mock_git.get_pr_diff = AsyncMock(return_value="some diff content")
    mock_git.post_pr_comment = AsyncMock()
    mock_git.get_pr_author_association = AsyncMock(return_value="OWNER")

    mock_harness = MagicMock()
    mock_harness.aclose = AsyncMock()
    mock_harness.trigger_pipeline = AsyncMock(return_value="exec-123")
    mock_harness.get_execution_url = MagicMock(return_value="https://app.harness.io/exec/123")

    with patch("agent.main.Settings", return_value=mock_settings), \
         patch("agent.main.GitClient", return_value=mock_git), \
         patch("agent.main.HarnessClient", return_value=mock_harness):
        with TestClient(app) as c:
            yield c


# ── /health ───────────────────────────────────────────────────────────────────

def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── Signature validation ──────────────────────────────────────────────────────

def test_webhook_bad_signature_returns_401(client: TestClient) -> None:
    body = _payload_bytes()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"x-harness-signature": "sha256=badhash"},
    )
    assert resp.status_code == 401


def test_webhook_missing_signature_returns_401(client: TestClient) -> None:
    body = _payload_bytes()
    resp = client.post("/webhook", content=body)
    assert resp.status_code == 401


def test_webhook_wrong_prefix_returns_401(client: TestClient) -> None:
    body = _payload_bytes()
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"x-harness-signature": f"md5={digest}"},  # wrong prefix
    )
    assert resp.status_code == 401


# ── WebhookPayload validators ─────────────────────────────────────────────────

def test_webhook_invalid_sha_returns_200_error(client: TestClient) -> None:
    body = _payload_bytes(pr_sha="NOTAHEX!")   # triggers sha_looks_valid
    resp = client.post(
        "/webhook",
        content=body,
        headers={"x-harness-signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_webhook_repo_without_slash_returns_200_error(client: TestClient) -> None:
    body = _payload_bytes(repo="nodash")   # triggers repo_has_slash
    resp = client.post(
        "/webhook",
        content=body,
        headers={"x-harness-signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


# ── _post_comment / _retrigger_pipeline error swallowing ─────────────────────

def test_post_comment_failure_is_swallowed(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    state.git.post_pr_comment = AsyncMock(side_effect=Exception("network down"))
    review = ReviewResult(verdict=Verdict.APPROVE, summary="All good.", issues=[])
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    # Error in _post_comment must not surface as HTTP 500
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_retrigger_failure_is_swallowed(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    state.harness.trigger_pipeline = AsyncMock(side_effect=Exception("harness down"))
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Bug found.",
        needs_supervision=False,
        issues=[Issue(file="a.py", line=1, severity=Severity.ERROR,
                      message="crash", fix="safe()")],
    )
    patch_result = PatchResult(success=True, commit_sha="abc123", patched_files=["a.py"])
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.apply_fixes", AsyncMock(return_value=patch_result)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    # Error in _retrigger_pipeline must not surface — fix_applied should still be True
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["fix_applied"] is True


# ── /webhook — repo allowlist ─────────────────────────────────────────────────

def test_webhook_rejects_wrong_repo(client: TestClient) -> None:
    body = _payload_bytes(repo="attacker/other-repo")
    resp = client.post(
        "/webhook",
        content=body,
        headers={"x-harness-signature": _sign(body)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert "not allowed" in data["error"]


def test_webhook_accepts_repo_different_case(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    body = _payload_bytes(repo="Acme/Backend")   # same as "acme/backend" in settings — case-insensitive
    review = ReviewResult(verdict=Verdict.APPROVE, summary="All good.", issues=[])
    with patch("agent.main.review_diff", AsyncMock(return_value=review)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    data = resp.json()
    assert data["status"] == "ok"
    assert data.get("error") != "Repository not allowed"


# ── /webhook — payload errors (always 200) ────────────────────────────────────

def test_webhook_invalid_json_returns_200_error(client: TestClient) -> None:
    body = b"this is not json {"
    resp = client.post(
        "/webhook",
        content=body,
        headers={"x-harness-signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_webhook_diff_fetch_error_returns_200(client: TestClient) -> None:
    from agent.git_client import GitClientError
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(side_effect=GitClientError("repo not found"))
    body = _payload_bytes()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"x-harness-signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_webhook_reviewer_error_returns_200(client: TestClient) -> None:
    from agent.reviewer import ReviewerError
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(side_effect=ReviewerError("API down"))):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


# ── /webhook — empty diff ─────────────────────────────────────────────────────

def test_webhook_empty_diff_auto_approves(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="   \n  ")
    body = _payload_bytes()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"x-harness-signature": _sign(body)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["verdict"] == "approve"
    assert data["fix_applied"] is False


# ── /webhook — review outcomes ────────────────────────────────────────────────

def test_webhook_approve_verdict(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    review = ReviewResult(verdict=Verdict.APPROVE, summary="All good.", issues=[])
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "approve"
    assert data["fix_applied"] is False


def test_webhook_comment_only_when_no_fixable_issues(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Unfixable issues found.",
        issues=[Issue(file="a.py", line=1, severity=Severity.WARNING,
                      message="bad practice", fix=None)],
    )
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["fix_applied"] is False


def test_webhook_auto_fix_without_supervision(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="SQL injection found.",
        needs_supervision=False,
        issues=[Issue(file="a.py", line=1, severity=Severity.ERROR,
                      message="SQL injection", fix="safe_query(param)")],
    )
    patch_result = PatchResult(success=True, commit_sha="fix123abc", patched_files=["a.py"])
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.apply_fixes", AsyncMock(return_value=patch_result)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["fix_applied"] is True
    assert data["fix_commit_sha"] == "fix123abc"


def test_webhook_supervision_escalates_to_human(client: TestClient) -> None:
    from agent.main import state
    from agent.supervisor import SupervisionResult, SupervisorVerdict

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Serious security issue.",
        needs_supervision=True,
        supervision_reason="error severity",
        issues=[Issue(file="a.py", line=1, severity=Severity.ERROR,
                      message="remote code execution", fix="safe()")],
    )
    supervision = SupervisionResult(
        verdict=SupervisorVerdict.ESCALATE,
        reasoning="Needs human review",
        concerns=["critical security impact"],
    )
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.supervise", AsyncMock(return_value=supervision)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["fix_applied"] is False


def test_webhook_supervision_override_to_comment(client: TestClient) -> None:
    from agent.main import state
    from agent.supervisor import SupervisionResult, SupervisorVerdict

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Issue found.",
        needs_supervision=True,
        supervision_reason="error severity",
        issues=[Issue(file="a.py", line=1, severity=Severity.ERROR,
                      message="potential bug", fix="maybe_fix()")],
    )
    supervision = SupervisionResult(
        verdict=SupervisorVerdict.OVERRIDE_TO_COMMENT,
        reasoning="Fix is risky",
        concerns=["might break API contract"],
    )
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.supervise", AsyncMock(return_value=supervision)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["fix_applied"] is False


def test_webhook_supervision_failure_falls_back_to_comment(client: TestClient) -> None:
    from agent.main import state
    from agent.supervisor import SupervisorError

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Issue found.",
        needs_supervision=True,
        supervision_reason="error severity",
        issues=[Issue(file="a.py", line=1, severity=Severity.ERROR,
                      message="bug", fix="fix()")],
    )
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.supervise", AsyncMock(side_effect=SupervisorError("Opus unavailable"))):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["fix_applied"] is False


def test_webhook_supervision_approved_proceeds_to_fix(client: TestClient) -> None:
    from agent.main import state
    from agent.supervisor import SupervisionResult, SupervisorVerdict

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Issue found.",
        needs_supervision=True,
        supervision_reason="error severity",
        issues=[Issue(file="a.py", line=1, severity=Severity.ERROR,
                      message="sql injection", fix="safe_query()")],
    )
    supervision = SupervisionResult(
        verdict=SupervisorVerdict.APPROVE_FIX,
        reasoning="Fix is safe",
        concerns=[],
    )
    patch_result = PatchResult(success=True, commit_sha="aabbcc", patched_files=["a.py"])
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.supervise", AsyncMock(return_value=supervision)), \
         patch("agent.main.apply_fixes", AsyncMock(return_value=patch_result)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["fix_applied"] is True
    assert data["fix_commit_sha"] == "aabbcc"


# ── /webhook — author trust gate ─────────────────────────────────────────────

def test_webhook_untrusted_author_skips_auto_fix(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    state.git.get_pr_author_association = AsyncMock(return_value="CONTRIBUTOR")
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Bug found.",
        needs_supervision=False,
        issues=[Issue(file="a.py", line=1, severity=Severity.ERROR,
                      message="null deref", fix="if x is not None:")],
    )
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.apply_fixes") as mock_apply:
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["fix_applied"] is False
    mock_apply.assert_not_called()


def test_webhook_trusted_author_proceeds_to_auto_fix(client: TestClient) -> None:
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    state.git.get_pr_author_association = AsyncMock(return_value="MEMBER")
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Bug found.",
        needs_supervision=False,
        issues=[Issue(file="a.py", line=1, severity=Severity.WARNING,
                      message="bad practice", fix="good_practice()")],
    )
    patch_result = PatchResult(success=True, commit_sha="cc1234", patched_files=["a.py"])
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.apply_fixes", AsyncMock(return_value=patch_result)):
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["fix_applied"] is True


def test_webhook_association_fetch_failure_skips_auto_fix(client: TestClient) -> None:
    from agent.git_client import GitClientError
    from agent.main import state

    state.git.get_pr_diff = AsyncMock(return_value="some diff")
    state.git.get_pr_author_association = AsyncMock(
        side_effect=GitClientError("API timeout")
    )
    review = ReviewResult(
        verdict=Verdict.REQUEST_CHANGES,
        summary="Bug found.",
        needs_supervision=False,
        issues=[Issue(file="a.py", line=1, severity=Severity.ERROR,
                      message="crash", fix="safe()")],
    )
    body = _payload_bytes()
    with patch("agent.main.review_diff", AsyncMock(return_value=review)), \
         patch("agent.main.apply_fixes") as mock_apply:
        resp = client.post(
            "/webhook",
            content=body,
            headers={"x-harness-signature": _sign(body)},
        )
    assert resp.status_code == 200
    assert resp.json()["fix_applied"] is False
    mock_apply.assert_not_called()
