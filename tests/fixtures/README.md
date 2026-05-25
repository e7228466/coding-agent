# tests/fixtures/

Static test data for unit tests. No real API calls needed.

## Files

### `sample_webhook_payload.json`
Used by the local `curl` smoke test in CLAUDE.md:
```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Harness-Signature: <computed-hmac>" \
  -d @tests/fixtures/sample_webhook_payload.json
```

### `diffs/`

Each scenario has two files:
- `<name>.diff`          — raw `git diff` output to feed into `reviewer.py`
- `<name>_expected.json` — the `ReviewResult` JSON Claude should return

| Scenario | Verdict | Issues |
|---|---|---|
| `sql_injection` | `request_changes` | SQL injection (error) + unused import (warning) |
| `clean` | `approve` | none |

## Adding a new fixture

1. Save the diff as `diffs/<name>.diff`
2. Save the expected `ReviewResult` as `diffs/<name>_expected.json`
3. In your test, load both and mock the Anthropic client to return the expected JSON:

```python
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "diffs"

def load_fixture(name: str) -> tuple[str, str]:
    diff = (FIXTURES / f"{name}.diff").read_text()
    expected = (FIXTURES / f"{name}_expected.json").read_text()
    return diff, expected
```
