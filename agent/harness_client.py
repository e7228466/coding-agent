"""
agent/harness_client.py

Async HTTP client for the Harness NextGen REST API.
Used by the agent to re-trigger a pipeline after pushing a fix commit.

Public API:
    HarnessClient
      .trigger_pipeline()   — start a new pipeline execution for a given PR
      .get_execution_url()  — build a browser URL to the execution (for PR comments)

Docs: https://apidocs.harness.io/tag/Pipeline-Execution
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

HARNESS_BASE_URL = "https://app.harness.io"
REQUEST_TIMEOUT = 30  # seconds


# ── Custom exception ──────────────────────────────────────────────────────────


class HarnessClientError(Exception):
    """Raised when any Harness API operation fails."""


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HarnessClientSettings:
    api_key: str
    account_id: str
    org_id: str
    project_id: str
    pipeline_id: str


# ── Main client ───────────────────────────────────────────────────────────────


class HarnessClient:
    """
    Async Harness API client.

    Uses httpx.AsyncClient for all requests — fully non-blocking.

    Usage:
        settings = HarnessClientSettings(
            api_key=..., account_id=..., org_id=...,
            project_id=..., pipeline_id=...,
        )
        harness = HarnessClient(settings)
        execution_id = await harness.trigger_pipeline(
            pr_number="42", pr_branch="feat/login", pr_sha="abc1234"
        )
    """

    def __init__(
        self,
        settings: HarnessClientSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._s = settings
        # Allow injection of a mock client in tests
        self._http = http_client or httpx.AsyncClient(
            base_url=HARNESS_BASE_URL,
            headers={
                "x-api-key": settings.api_key,
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )

    # ── Public methods ────────────────────────────────────────────────────────

    async def trigger_pipeline(
        self,
        *,
        pr_number: str,
        pr_branch: str,
        pr_sha: str,
    ) -> str:
        """
        Trigger a new execution of the configured pipeline.

        Passes PR metadata as pipeline input variables so the triggered run
        has full context — matching how the original webhook trigger works.

        Args:
            pr_number:  PR number string, e.g. "42".
            pr_branch:  Head branch name.
            pr_sha:     Head commit SHA (after agent fix commit was pushed).

        Returns:
            The Harness execution ID string (used to build the execution URL).

        Raises:
            HarnessClientError: On non-2xx response or network failure.
        """
        url = (
            f"/pipeline/api/pipeline/execute/{self._s.pipeline_id}"
            f"?accountIdentifier={self._s.account_id}"
            f"&orgIdentifier={self._s.org_id}"
            f"&projectIdentifier={self._s.project_id}"
            f"&moduleType=CI"
        )

        payload = {
            "inputSetTemplateYaml": _build_input_yaml(
                pipeline_id=self._s.pipeline_id,
                pr_number=pr_number,
                pr_branch=pr_branch,
                pr_sha=pr_sha,
            )
        }

        logger.info(
            "Triggering Harness pipeline %s for PR #%s (sha=%s)",
            self._s.pipeline_id,
            pr_number,
            pr_sha,
        )

        try:
            response = await self._http.post(url, json=payload)
        except httpx.RequestError as exc:
            raise HarnessClientError(
                f"Network error triggering pipeline: {exc}"
            ) from exc

        if response.status_code not in (200, 201):
            raise HarnessClientError(
                f"Harness API returned {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        try:
            execution_id: str = data["data"]["planExecution"]["uuid"]
        except (KeyError, TypeError) as exc:
            raise HarnessClientError(
                f"Unexpected Harness response shape: {data}"
            ) from exc

        logger.info("Pipeline execution started: %s", execution_id)
        return execution_id

    def get_execution_url(self, execution_id: str) -> str:
        """
        Return a browser URL for a given Harness pipeline execution.

        Used to embed a direct link in the PR comment so developers can
        watch the re-triggered run without navigating the Harness UI.

        Args:
            execution_id: The UUID returned by trigger_pipeline().

        Returns:
            Full HTTPS URL string.
        """
        return (
            f"{HARNESS_BASE_URL}/ng/#/account/{self._s.account_id}"
            f"/ci/orgs/{self._s.org_id}"
            f"/projects/{self._s.project_id}"
            f"/pipelines/{self._s.pipeline_id}"
            f"/executions/{execution_id}/pipeline"
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Call on app shutdown."""
        await self._http.aclose()


# ── Internal helpers ──────────────────────────────────────────────────────────


def _build_input_yaml(
    *,
    pipeline_id: str,
    pr_number: str,
    pr_branch: str,
    pr_sha: str,
) -> str:
    """
    Build the inputSetTemplateYaml string required by the Harness execute API.

    This mirrors the `inputYaml` block in harness/trigger.yaml, but with
    concrete values instead of trigger expressions.

    Uses yaml.safe_dump() for all variable values so branch names containing
    quotes, colons, newlines, or other special characters are safely escaped
    rather than breaking the YAML structure.
    """
    import yaml

    def _quote(value: str) -> str:
        # Dump as a single-key mapping so PyYAML picks the safest scalar style,
        # then extract just the value portion (after "v: ").
        # This correctly handles quotes, colons, newlines, and all special chars.
        dumped = yaml.safe_dump({"v": value}, default_flow_style=False, allow_unicode=True)
        return dumped.split("v: ", 1)[1].rstrip("\n")

    return (
        f"pipeline:\n"
        f"  identifier: {_quote(pipeline_id)}\n"
        f"  variables:\n"
        f"    - name: pr_number\n"
        f"      type: String\n"
        f"      value: {_quote(pr_number)}\n"
        f"    - name: pr_branch\n"
        f"      type: String\n"
        f"      value: {_quote(pr_branch)}\n"
        f"    - name: pr_sha\n"
        f"      type: String\n"
        f"      value: {_quote(pr_sha)}\n"
    )
