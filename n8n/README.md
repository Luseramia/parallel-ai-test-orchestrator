# n8n workflow exports

Import the three JSON files in `workflows/`, inspect them in the target n8n
version, then activate them. They intentionally contain no credential records.

Required n8n environment variables:

- `AI_TEST_GATEWAY_URL` and `AI_TEST_GATEWAY_TOKEN`
- `AI_TEST_PLAN_WEBHOOK_SECRET` and `AI_TEST_COMPLETION_WEBHOOK_SECRET`
- `GITHUB_WEBHOOK_SECRET`
- `AI_TEST_ALLOWED_REPOSITORIES`, a comma-separated GitHub `owner/repo` list
- `AI_TEST_NOTIFICATION_URL` and `AI_TEST_NOTIFICATION_TOKEN`

The Code nodes use Node's `crypto` module. Self-hosted n8n must explicitly
allow it with `NODE_FUNCTION_ALLOW_BUILTIN=crypto`. Keep environment access
enabled for these workflows or replace the expressions with n8n credential
objects before activation.

Plan and completion signatures cover `timestamp + "." + raw_request_body`
using HMAC-SHA256. Send `X-AI-Test-Timestamp` and
`X-AI-Test-Signature: sha256=<hex>`. Requests older than five minutes are
rejected. GitHub pushes use `X-Hub-Signature-256` and
`X-GitHub-Delivery`.

The completion workflow forwards a stable `Idempotency-Key` to the configured
notification sink. The sink must persist this key before creating a comment or
sending a message. This closes the ambiguous-acknowledgement case where n8n
finishes the side effect but the gateway retries because the HTTP response was
lost.

Webhook executions contain only short authentication, validation, and gateway
requests. Codex and test execution are never run inside n8n.
