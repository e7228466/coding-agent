# CLAUDE.md — Coding Agent for Harness Engineering

This file is the authoritative guide for Claude Code working in this repository.
Read it fully before making any changes.

---

## Project start checklist

Every project must confirm the following documents before any work begins.
**Do not skip any required document — ask the user to provide it if missing.**

| Document | Required | Location |
|---|---|---|
| `project.md` | **必須** | `docs/project.md` |
| PRD | **必須** | `docs/PRD.md` |
| Implementation plan | 任意（不需要時可跳過） | `docs/implementation.md` |

If `project.md` or PRD is missing, stop and request it from the user before proceeding.

---

## Project overview

This project is a **Coding Agent** that integrates with Harness CI/CD.
When a developer opens a Pull Request, Harness triggers this agent via webhook.
The agent reviews the code diff using Claude, posts a review comment, and—if issues
are found—automatically generates a fix commit and re-triggers the Harness pipeline.

### Core flow

```
PR opened → Harness Trigger → CI Step calls agent webhook
  → agent/main.py receives diff
  → reviewer.py calls Claude API (code review)
  → if issues found → patcher.py generates fix → git_client.py commits
  → harness_client.py re-triggers pipeline
  → if clean → CD Step deploys
```

---

## Repository layout

```
coding-agent/
├── harness/
│   ├── pipeline.yaml       # Main Harness CI/CD pipeline definition
│   ├── trigger.yaml        # PR webhook trigger configuration
│   └── secrets.yaml        # Secret references (no actual values here)
├── agent/
│   ├── main.py             # FastAPI app — webhook entry point
│   ├── reviewer.py         # Claude API calls for code review
│   ├── patcher.py          # Generates fix patches from review output
│   ├── git_client.py       # GitHub API integration (PyGithub)
│   ├── harness_client.py   # Harness API integration (pipeline trigger)
│   └── models.py           # Pydantic models for request/response
├── tests/
│   ├── test_reviewer.py
│   ├── test_patcher.py
│   └── fixtures/           # Sample diffs and review outputs for tests
├── Dockerfile
├── requirements.txt
├── .env.example
└── CLAUDE.md               # ← you are here
```

---

## Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | Python 3.11+ | Primary language |
| Web framework | FastAPI | Async webhook receiver |
| Claude integration | `anthropic` SDK | Code review via Claude |
| GitHub integration | `PyGithub` | Fetch diffs, post comments, push commits |
| Harness integration | `httpx` + REST | Trigger pipelines, report status |
| Config | `pydantic-settings` | Typed env var management |
| Containerisation | Docker | Runs inside Harness CI steps |

---

## Environment variables

All secrets come from environment variables. Never hardcode credentials.

```bash
# .env.example — copy to .env for local development
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
GITHUB_REPO=owner/repo-name
HARNESS_API_KEY=...
HARNESS_ACCOUNT_ID=...
HARNESS_ORG_ID=...
HARNESS_PROJECT_ID=...
HARNESS_PIPELINE_ID=...
WEBHOOK_SECRET=...          # HMAC secret for validating Harness webhook calls
LOG_LEVEL=INFO
```

In production, these are injected via Harness Secrets Manager and referenced in
`harness/secrets.yaml`. Never commit `.env`.

---

## Key design decisions

### 1. Stateless agent

The agent is fully stateless. All context (PR number, repo, diff) comes in the
webhook payload. No database is used. This makes horizontal scaling trivial inside
Harness.

### 2. Claude as reviewer, not decision-maker

Claude outputs structured JSON (`ReviewResult`) with a list of issues and optional
fix suggestions. The agent code (not Claude) decides whether to auto-fix, comment
only, or approve. This keeps business logic auditable and testable without calling
the API.

### 3. Fix commits go on the PR branch

When the agent generates a fix, it pushes directly to the PR's head branch via
the GitHub API. It does not open a new PR. The commit message always includes
`[agent-fix]` so humans can identify automated commits.

### 4. Idempotent re-triggering

`harness_client.py` uses the Harness API to trigger the pipeline with the same
PR SHA. The pipeline YAML has a guard step that exits early if no agent-fix commit
is newer than the last run, preventing infinite loops.

### 5. Webhook validation

Every incoming request to `POST /webhook` is validated against `WEBHOOK_SECRET`
using HMAC-SHA256. Reject without processing if the signature does not match.

---

## Claude API usage

### Model 分工

このプロジェクトは **2つのモデル** を役割分担して使用する。
モデル名を変更する場合は必ずこのファイルと対応するテストフィクスチャも更新すること。

| 役割 | モデル | ファイル | 呼び出し条件 |
|---|---|---|---|
| **Reviewer（一次審査）** | `ollama/llama3.1`（ローカル）or `claude-sonnet-4-6` | `reviewer.py` / `scripts/review.py` | 毎回必ず呼ぶ |
| **Supervisor（監督）** | `claude-opus-4-5` | `supervisor.py` / `scripts/review.py` | Reviewer が必要と判断した場合のみ |

### Sonnet の役割（Reviewer）

- PR diff を高速にレビューして問題を洗い出す
- `needs_supervision` フラグで Opus が必要かどうか自己判断する
- 以下の条件に該当する場合は `needs_supervision: true` にする：
  - `error` severity の issue が 1 件以上ある
  - セキュリティ上の脆弱性がある
  - auto-fix の正しさに自信が持てない
  - 変更の影響範囲が大きい

### Opus の役割（Supervisor）

- Sonnet の review 結果と元の diff を受け取り、品質チェックを行う
- **コストを抑えるため、Sonnet が `needs_supervision: true` にした場合のみ呼ぶ**
- 3つの verdict を返す：

| verdict | 意味 | 次のアクション |
|---|---|---|
| `approve_fix` | Sonnet の結論は妥当 | auto-fix を実行 |
| `override_to_comment` | 問題は本物だが fix は危険 | コメントのみ投稿 |
| `escalate` | 重大な見落としあり | 人間によるレビューを要求 |

### System prompt の場所

- Sonnet 用：`reviewer.py` の `SYSTEM_PROMPT`（module-level 定数）
- Opus 用：`supervisor.py` の `SUPERVISION_SYSTEM_PROMPT`（module-level 定数）
- ローカル版：`.claude/review-prompt.md`（Claude Code CLI に渡す markdown）

いずれも動的に構築してはいけない。

### Structured output

Sonnet の出力スキーマ：
```
{
  "verdict": "approve" | "request_changes",
  "needs_supervision": true | false,
  "supervision_reason": "理由（needs_supervision が true の場合）",
  "summary": "...",
  "issues": [...]
}
```

Opus の出力スキーマ：
```
{
  "verdict": "approve_fix" | "override_to_comment" | "escalate",
  "reasoning": "判断理由",
  "concerns": ["懸念点"]
}
```

いずれも `json.loads()` で parse し、失敗した場合は comment-only にフォールバックする。

### Ollama（ローカルモデル）

Reviewer は Ollama 経由でローカルモデルに切り替え可能。Supervisor は常に Opus を使用する。

```bash
# .env に追加
REVIEWER_MODEL=ollama/llama3.1      # または ollama/deepseek-r1 など
OLLAMA_BASE_URL=http://localhost:11434/v1   # Ollama のデフォルト
```

`reviewer.py` の `_call_llm()` がモデル名の先頭 `ollama/` を見て自動的に
OpenAI 互換エンドポイント（Ollama）に切り替える。
`REVIEWER_MODEL` を設定しない場合は `claude-sonnet-4-6` にフォールバックする。

### Token budget

diff は 8,000 tokens 以内に収める。超える場合は最初の 8,000 tokens に切り詰めて
`[diff truncated]` を付記する。切り詰めロジックは `reviewer.py::truncate_diff()`。

---

## Coding conventions

### General

- Type-annotate all function signatures.
- Use `async def` for all I/O-bound functions (API calls, file reads).
- Use `pydantic` models for all data crossing module boundaries.
- No bare `except:` — always catch a specific exception or `Exception` with logging.

### Naming

| Thing | Convention | Example |
|---|---|---|
| Files | `snake_case.py` | `git_client.py` |
| Classes | `PascalCase` | `ReviewResult` |
| Functions / variables | `snake_case` | `fetch_pr_diff()` |
| Constants | `UPPER_SNAKE` | `SYSTEM_PROMPT` |
| Harness YAML IDs | `kebab-case` | `run-code-review` |

### Logging

Use the standard `logging` module. Get loggers with `logging.getLogger(__name__)`.
Log at `INFO` for normal operations, `WARNING` for recoverable issues (e.g. diff
truncation), `ERROR` for failures that abort the request.
Never log the full diff or any file contents at `ERROR` level — they may contain
secrets.

### Error handling in the webhook handler

`main.py` must always return HTTP 200 to Harness, even on internal errors.
Harness retries on non-200, which can cause duplicate reviews.
Internal errors should be logged and returned as:
```json
{ "status": "error", "message": "..." }
```

---

## Harness pipeline conventions

### Step naming

Use descriptive `name:` fields in kebab-case IDs and sentence-case display names.

```yaml
- step:
    identifier: run-code-review
    name: Run code review agent
    type: Run
```

### Failure strategy

Every step that calls the agent must have:
```yaml
failureStrategies:
  - onFailure:
      errors: [AllErrors]
      action:
        type: Ignore   # agent errors must not block the pipeline
```

The agent itself reports status back via PR comments. Pipeline failure handling
is separate from review outcome.

### Secrets references

Always use Harness secret syntax, never inline values:
```yaml
envVariables:
  ANTHROPIC_API_KEY: <+secrets.getValue("anthropic_api_key")>
```

---

## Testing

### Run tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

### Fixtures

Sample diffs live in `tests/fixtures/diffs/`. Each fixture is a `.diff` file
paired with an `_expected.json` file containing the expected `ReviewResult`.
Use these in unit tests to avoid calling the real Claude API.

### Mocking

- Mock `anthropic.Anthropic` with `unittest.mock.patch` in reviewer tests.
- Mock `github.Github` in patcher and git_client tests.
- Never make real API calls in unit tests.

### Test coverage target

Aim for ≥ 80% coverage on `reviewer.py` and `patcher.py`. These are the
highest-risk files.

---

## Local development workflow

```bash
# 1. Clone and set up
git clone <repo>
cd coding-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys

# 2. Run the agent locally
uvicorn agent.main:app --reload --port 8000

# 3. Simulate a webhook (using curl)
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Harness-Signature: <computed-hmac>" \
  -d @tests/fixtures/sample_webhook_payload.json

# 4. Run tests
pytest tests/ -v --cov=agent --cov-report=term-missing
```

---

## What Claude Code should NOT do

- Do not modify `harness/secrets.yaml` — secret values are managed outside this repo.
- Do not change the model names in `reviewer.py` or `supervisor.py` without updating this file.
- Do not call Opus (`claude-opus-4-5`) unconditionally — it must only be called when Sonnet sets `needs_supervision: true`.
- Current Reviewer model: `claude-sonnet-4-6`. Current Supervisor model: `claude-opus-4-5`.
- Do not add synchronous blocking I/O inside `async def` functions — use `asyncio.to_thread()` if needed.
- Do not add new environment variables without updating `.env.example` and this file.
- Do not push to `main` directly — all changes go through PRs (the agent reviews its own fixes too).

---

## Known limitations

### `git_client.commit_files()` — one commit per file

The GitHub Contents API does not support multi-file atomic commits. Each file in a
fix is pushed as a separate API call, so a 3-file fix produces 3 separate
`[agent-fix]` commits in the PR history. The Harness guard step reads only
`git log -1 --pretty=%s`, so it still detects the fix correctly. This is a known
trade-off, not a bug.

### `patcher._apply_single_fix()` — line-window heuristic

Fixes are located by a line-number window heuristic. If the target line is very
short or appears multiple times near the target line (e.g. `return x`, `pass`),
the patch may match the wrong location. In such cases the fix is skipped entirely
rather than applied incorrectly. Claude Code should not attempt to "improve" this
heuristic without understanding the full test suite in `tests/test_patcher.py`.

---

## Glossary

| Term | Meaning |
|---|---|
| **Verdict** | Claude's top-level decision: `approve` or `request_changes` |
| **Issue** | A single code problem identified by Claude, with file, line, severity, message, and optional fix |
| **Patch** | A git diff string generated by `patcher.py` from Claude's fix suggestions |
| **Agent-fix commit** | A commit pushed by the agent, identifiable by `[agent-fix]` in the message |
| **Harness step** | A single unit of work inside a Harness pipeline stage |
| **Trigger** | A Harness entity that listens for events (PR open/update) and starts a pipeline |
