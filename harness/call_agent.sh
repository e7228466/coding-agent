#!/bin/sh
set -e

apk add -q --no-progress curl python3

REQUEST_BODY=$(printf '{"pr_number":"%s","pr_branch":"%s","pr_sha":"%s","repo":"%s"}' \
  "$PR_NUMBER" "$PR_BRANCH" "$PR_SHA" "$GITHUB_REPO")

SIG="sha256=$(printf '%s' "$REQUEST_BODY" | python3 -c \
  'import sys,hmac,hashlib,os;body=sys.stdin.buffer.read();key=os.environ["WEBHOOK_SECRET"].encode();print(hmac.new(key,body,hashlib.sha256).hexdigest())')"

CT="Content-Type: application/json"
SH="X-Harness-Signature: $SIG"

RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST "${AGENT_WEBHOOK_URL}/webhook" \
  -H "$CT" \
  -H "$SH" \
  -d "$REQUEST_BODY")

HTTP_BODY=$(echo "$RESPONSE" | head -n -1)
HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)

echo "Agent response: $HTTP_BODY"
echo "HTTP status: $HTTP_CODE"

if echo "$HTTP_BODY" | grep -q "fix_applied"; then
  echo "##[set-output name=agent_fix_applied;value=true]"
else
  echo "##[set-output name=agent_fix_applied;value=false]"
fi

exit 0
