# Coding Agent

AI-powered code review and auto-fix agent for Harness CI/CD.

---

## Prerequisites

Before running the agent you need four things pre-created in Harness:

1. Three **Connectors** (see setup below)
2. **Secrets** declared in `harness/secrets.yaml`
3. A Kubernetes namespace for the runner pods
4. A Docker Hub account (or any container registry) to push the agent image

---

## Harness connector setup

The pipeline references three connectors by identifier. Create them once in the
Harness UI under **Project → Project Setup → Connectors**.

### 1. `github-connector` — GitHub App / PAT

Used by the Trigger to receive webhook events from GitHub.

| Field | Value |
|---|---|
| Name | `github-connector` (must match exactly) |
| Type | GitHub |
| URL | `https://github.com` |
| Connection type | Account |
| Auth | Personal Access Token → use secret `github_token` |
| API access | Enable → same token |

Required token scopes: `repo`, `pull_requests`, `write:discussion`

### 2. `docker-hub-connector` — Docker Hub

Used by CI steps to pull the `curlimages/curl` and `alpine` images.

| Field | Value |
|---|---|
| Name | `docker-hub-connector` (must match exactly) |
| Type | Docker Registry |
| URL | `https://index.docker.io/v2/` |
| Auth | Username + Password (your Docker Hub credentials) |

If your organisation uses a private registry, change the image paths in
`harness/pipeline.yaml` and point this connector at your registry instead.

### 3. `k8s-connector` — Kubernetes cluster

Used by the CI stage to schedule runner pods.

| Field | Value |
|---|---|
| Name | `k8s-connector` (must match exactly) |
| Type | Kubernetes Cluster |
| Auth | Inherit from Delegate (recommended) or Service Account |
| Namespace | `harness-agents` (create it first: `kubectl create namespace harness-agents`) |

The service account needs these RBAC permissions in the `harness-agents` namespace:
`pods`, `pods/log`, `pods/exec` — create, get, list, watch, delete.

---

## Secrets setup

After creating the connectors, create all secrets listed in `harness/secrets.yaml`
under **Project → Project Setup → Secrets → + New Secret → Text**.

| Secret identifier | Where to get the value |
|---|---|
| `anthropic_api_key` | console.anthropic.com → API Keys |
| `github_token` | GitHub → Settings → Developer settings → PAT |
| `github_repo` | Plain string, e.g. `acme/backend` |
| `agent_webhook_url` | Base URL of your deployed agent, e.g. `https://agent.internal` |
| `webhook_secret` | Generate with `openssl rand -hex 32` |
| `harness_api_key` | Harness → Account Settings → API Keys → + Token |
| `harness_account_id` | Harness → Account Settings → Overview |
| `harness_org_id` | Harness → Organisation Settings → Overview |
| `harness_project_id` | Harness → Project Settings → Overview |
| `harness_pipeline_id` | `coding-agent-pipeline` (matches `pipeline.yaml`) |

---

## Local development

```bash
# 1. Clone and set up
git clone <repo>
cd coding-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # fill in your keys

# 2. Run the agent
uvicorn agent.main:app --reload --port 8000

# 3. Smoke test with a sample webhook
# First compute the HMAC signature:
BODY=$(cat tests/fixtures/sample_webhook_payload.json)
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print "sha256="$2}')

curl -s -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Harness-Signature: $SIG" \
  -d @tests/fixtures/sample_webhook_payload.json | python3 -m json.tool

# 4. Run tests
pytest tests/ -v --cov=agent --cov-report=term-missing
```

---

## Deploy the agent

```bash
# Build and push the Docker image
docker build -t <your-registry>/coding-agent:latest .
docker push <your-registry>/coding-agent:latest

# Deploy to Kubernetes (example — adapt to your setup)
kubectl create namespace coding-agent
kubectl create secret generic coding-agent-env \
  --from-env-file=.env \
  --namespace coding-agent
kubectl apply -f k8s/deployment.yaml   # create this from your infra templates
```

The agent must be reachable from Harness CI runner pods.
Set `agent_webhook_url` in Harness Secrets to the service's internal URL.

---

## Known limitations

- `git_client.commit_files()` calls the GitHub Contents API once per file,
  so a 3-file fix produces 3 separate `[agent-fix]` commits in the PR history.
  Harness reads only `git log -1`, so the guard step works correctly.

- `patcher._apply_single_fix()` uses a line-window heuristic. Very short or
  common lines (e.g. `return x`, `pass`) may be replaced at the wrong location
  if they appear more than once near the target line. The fix is skipped rather
  than applied incorrectly when the heuristic is uncertain.
