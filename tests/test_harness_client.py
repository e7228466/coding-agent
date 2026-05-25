"""
tests/test_harness_client.py

Unit tests for agent/harness_client.py.
httpx.AsyncClient is always mocked — no real Harness API calls are made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx

from agent.harness_client import (
    HarnessClient,
    HarnessClientError,
    HarnessClientSettings,
    _build_input_yaml,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

SETTINGS = HarnessClientSettings(
    api_key="harness-key",
    account_id="acc-001",
    org_id="org-001",
    project_id="proj-001",
    pipeline_id="coding-agent-pipeline",
)

EXECUTION_ID = "exec-uuid-1234"

VALID_RESPONSE = {
    "data": {
        "planExecution": {
            "uuid": EXECUTION_ID
        }
    }
}


def _make_http(status: int = 200, json: dict | None = None) -> MagicMock:
    """Build a mock httpx.AsyncClient."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json.return_value = json or VALID_RESPONSE
    response.text = str(json or VALID_RESPONSE)

    http = MagicMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(return_value=response)
    http.aclose = AsyncMock()
    return http


def _make_client(http: MagicMock) -> HarnessClient:
    return HarnessClient(SETTINGS, http_client=http)


# ── trigger_pipeline ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_pipeline_returns_execution_id():
    http = _make_http()
    client = _make_client(http)

    execution_id = await client.trigger_pipeline(
        pr_number="42", pr_branch="feat/login", pr_sha="abc1234"
    )

    assert execution_id == EXECUTION_ID


@pytest.mark.asyncio
async def test_trigger_pipeline_posts_to_correct_url():
    http = _make_http()
    client = _make_client(http)

    await client.trigger_pipeline(pr_number="42", pr_branch="feat/x", pr_sha="abc")

    call_args = http.post.call_args
    url: str = call_args.args[0]
    assert "coding-agent-pipeline" in url
    assert "acc-001" in url
    assert "org-001" in url
    assert "proj-001" in url


@pytest.mark.asyncio
async def test_trigger_pipeline_payload_contains_pr_vars():
    http = _make_http()
    client = _make_client(http)

    await client.trigger_pipeline(pr_number="99", pr_branch="feat/y", pr_sha="deadbeef")

    call_kwargs = http.post.call_args.kwargs
    yaml_str: str = call_kwargs["json"]["inputSetTemplateYaml"]
    assert "pr_number" in yaml_str
    assert "99" in yaml_str  # PyYAML single-quotes numeric-looking strings: '99'
    assert "feat/y" in yaml_str
    assert "deadbeef" in yaml_str


@pytest.mark.asyncio
async def test_trigger_pipeline_non_200_raises():
    http = _make_http(status=403, json={"message": "Forbidden"})
    client = _make_client(http)

    with pytest.raises(HarnessClientError, match="403"):
        await client.trigger_pipeline(pr_number="1", pr_branch="main", pr_sha="abc")


@pytest.mark.asyncio
async def test_trigger_pipeline_201_accepted():
    """Harness may return 201 on some versions — treat as success."""
    http = _make_http(status=201)
    client = _make_client(http)

    execution_id = await client.trigger_pipeline(
        pr_number="1", pr_branch="main", pr_sha="abc"
    )
    assert execution_id == EXECUTION_ID


@pytest.mark.asyncio
async def test_trigger_pipeline_network_error_raises():
    http = MagicMock(spec=httpx.AsyncClient)
    http.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    client = _make_client(http)
    with pytest.raises(HarnessClientError, match="Network error"):
        await client.trigger_pipeline(pr_number="1", pr_branch="main", pr_sha="abc")


@pytest.mark.asyncio
async def test_trigger_pipeline_unexpected_response_shape_raises():
    http = _make_http(json={"data": {}})   # missing planExecution key
    client = _make_client(http)

    with pytest.raises(HarnessClientError, match="Unexpected Harness response"):
        await client.trigger_pipeline(pr_number="1", pr_branch="main", pr_sha="abc")


# ── get_execution_url ─────────────────────────────────────────────────────────

def test_get_execution_url_contains_all_ids():
    client = HarnessClient(SETTINGS, http_client=MagicMock())
    url = client.get_execution_url("exec-abc-123")

    assert "acc-001" in url
    assert "org-001" in url
    assert "proj-001" in url
    assert "coding-agent-pipeline" in url
    assert "exec-abc-123" in url
    assert url.startswith("https://")


def test_get_execution_url_ends_with_pipeline():
    client = HarnessClient(SETTINGS, http_client=MagicMock())
    url = client.get_execution_url("exec-xyz")
    assert url.endswith("/pipeline")


# ── aclose ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aclose_calls_http_aclose():
    http = _make_http()
    client = _make_client(http)
    await client.aclose()
    http.aclose.assert_called_once()


# ── _build_input_yaml ─────────────────────────────────────────────────────────

def test_build_input_yaml_is_valid_yaml():
    import yaml
    result = _build_input_yaml(
        pipeline_id="my-pipeline",
        pr_number="5",
        pr_branch="feat/test",
        pr_sha="cafebabe",
    )
    parsed = yaml.safe_load(result)
    assert parsed["pipeline"]["identifier"] == "my-pipeline"
    variables = {v["name"]: v["value"] for v in parsed["pipeline"]["variables"]}
    assert variables["pr_number"] == "5"
    assert variables["pr_branch"] == "feat/test"
    assert variables["pr_sha"] == "cafebabe"


def test_build_input_yaml_branch_with_double_quotes():
    """Branch names containing double quotes must not break YAML structure."""
    import yaml
    result = _build_input_yaml(
        pipeline_id="p",
        pr_number="1",
        pr_branch='feat/fix-"nasty"-branch',
        pr_sha="abc",
    )
    parsed = yaml.safe_load(result)
    variables = {v["name"]: v["value"] for v in parsed["pipeline"]["variables"]}
    assert variables["pr_branch"] == 'feat/fix-"nasty"-branch'


def test_build_input_yaml_branch_with_colon():
    """Branch names containing colons must not break YAML structure."""
    import yaml
    result = _build_input_yaml(
        pipeline_id="p",
        pr_number="1",
        pr_branch="feat/scope:description",
        pr_sha="abc",
    )
    parsed = yaml.safe_load(result)
    variables = {v["name"]: v["value"] for v in parsed["pipeline"]["variables"]}
    assert variables["pr_branch"] == "feat/scope:description"


def test_build_input_yaml_branch_with_newline():
    """Embedded newlines (adversarial input) must not inject extra YAML keys."""
    import yaml
    malicious = "feat/branch\ninjected_key: injected_value"
    result = _build_input_yaml(
        pipeline_id="p",
        pr_number="1",
        pr_branch=malicious,
        pr_sha="abc",
    )
    parsed = yaml.safe_load(result)
    # The injected key must not appear at top level
    assert "injected_key" not in parsed
    variables = {v["name"]: v["value"] for v in parsed["pipeline"]["variables"]}
    assert variables["pr_branch"] == malicious
