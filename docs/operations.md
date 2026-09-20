# Operations runbook

Written for the operators and on-call engineers who run this system in a
cluster. It assumes `kubectl` access to `ai-test-system` and `ai-test-runners`,
and the API token from the `ai-test-gateway` Secret. Deployment itself is in
[../k8s/README.md](../k8s/README.md); the design and its security boundary are
in [architecture.md](architecture.md).

Conventions used below:

```console
export NS=ai-test-system
export RUNNERS=ai-test-runners
export GATEWAY=https://ai-test.internal              # or: kubectl -n $NS port-forward svc/gateway 8080:8080
export TOKEN=$(kubectl -n $NS get secret ai-test-gateway -o jsonpath='{.data.api-token}' | base64 -d)
alias api='curl -sS -H "Authorization: Bearer $TOKEN"'
```

## Contents

- [After every deploy](#after-every-deploy)
- [Inspecting a job](#inspecting-a-job)
- [Retry](#retry)
- [Cancel](#cancel)
- [Timeouts and stuck jobs](#timeouts-and-stuck-jobs)
- [Cleanup and reconciliation](#cleanup-and-reconciliation)
- [Secret rotation](#secret-rotation)
- [Incidents](#incidents)
- [Staging end-to-end verification](#staging-end-to-end-verification)
- [What to watch](#what-to-watch)

## After every deploy

These four checks correspond to the security claims the design makes. Run them
after any change to RBAC, NetworkPolicy, the launcher or the images.

**1. The gateway can create Jobs only in the runner namespace.**

```console
kubectl auth can-i create jobs -n $RUNNERS --as=system:serviceaccount:ai-test-system:gateway   # yes
kubectl auth can-i create jobs -n default  --as=system:serviceaccount:ai-test-system:gateway   # no
kubectl auth can-i create pods -n $RUNNERS --as=system:serviceaccount:ai-test-system:gateway   # no
kubectl auth can-i get secrets -n $RUNNERS --as=system:serviceaccount:ai-test-system:gateway   # no
```

**2. Runners cannot reach the Kubernetes API.**

```console
kubectl auth can-i --list -n $RUNNERS --as=system:serviceaccount:ai-test-runners:test-runner
kubectl -n $RUNNERS get jobs -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.automountServiceAccountToken}{"\n"}{end}'
```

The first prints only the self-review verbs every identity has; the second must
print `false` for every Job. No API token is mounted, and the egress policy
denies RFC1918 destinations, so there is no socket to try either.

**3. Test Jobs hold no model credential.**

```console
kubectl -n $RUNNERS get jobs -l app.kubernetes.io/component=test -o json \
  | grep -c OPENAI_API_KEY        # must print 0
```

**4. The reconciler runs.**

```console
kubectl -n $NS get cronjob reconciler
kubectl -n $NS logs job/$(kubectl -n $NS get jobs -l app.kubernetes.io/component=reconciler \
  --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')
```

A healthy sweep prints one JSON line, for example
`{"notifications_retried": 0, "purged_artifacts": 0, "purged_bytes": 0, "released": [], "timed_out": []}`.

## Inspecting a job

```console
api $GATEWAY/api/v1/test-jobs/$JOB_ID | python -m json.tool
```

The response carries `status`, `base_sha`, `code_sha`, `tested_sha`,
`failure_class`, `failure_message` and every artifact with its SHA-256 and a
short-lived signed `url`. An artifact with a `purged_at` timestamp and a null
`url` has passed retention: the digest is kept, the bytes are gone.

Download one (the URL is already signed, so no token is needed, and it expires
in minutes):

```console
curl -sS "$GATEWAY$(api $GATEWAY/api/v1/test-jobs/$JOB_ID \
  | python -c 'import json,sys;print(next(a["url"] for a in json.load(sys.stdin)["artifacts"] if a["type"]=="RAW_LOG"))')" -o runner.log
```

Runner Pod logs, while the Job still exists (`ttlSecondsAfterFinished` is one
hour by default):

```console
kubectl -n $RUNNERS logs -l ai-test.openai.com/job-id=$JOB_ID --tail=200
```

Never fix state by editing the database. Every status change must go through the
API or the reconciler, because the event table, the outbox and the optimistic
version are updated together.

## Retry

There is no generic "retry" verb, because what to redo depends on where the job
stopped.

| Situation | Action |
| --- | --- |
| Test result is wrong or flaky for a commit | Push a new commit and let the push webhook verify it. The older attempt becomes `SUPERSEDED` |
| Same commit, infrastructure failure (`ERROR`, `INFRASTRUCTURE`) | Re-deliver the push event: `api -X POST $GATEWAY/api/v1/test-jobs/$JOB_ID/verify -d '{"repository":"...","branch":"...","code_sha":"<sha>","push_event_id":"<new-delivery-id>"}'`. Use a delivery ID that has not been seen; reusing one is answered as a duplicate by design |
| Notification never reached n8n | Nothing to do by hand: the reconciler retries `PENDING` notifications every five minutes. Confirm with the sweep output. A notification that exhausted its four attempts is `DEAD` and needs the incident below |
| Plan itself was wrong | Submit a corrected plan. It is a new job: the idempotency key includes the plan digest |

The prepare phase is not retried automatically. If prepare failed for a plan you
still want, fix the cause and submit the plan again.

## Cancel

```console
api -X POST $GATEWAY/api/v1/test-jobs/$JOB_ID/cancel
```

Cancel deletes only the Kubernetes Job recorded for that `job_id` - the gateway
re-reads the Job and refuses if its `ai-test.openai.com/job-id` label does not
match - records `CANCELLED`, and enqueues one completion notification. Calling
it again on a job that is already finished returns `duplicate: true` and changes
nothing.

## Timeouts and stuck jobs

Three independent mechanisms end a job that stops making progress:

1. `activeDeadlineSeconds` (30 minutes) makes Kubernetes kill the Pod.
2. The runner classifies its own per-command timeout and reports `TIMEOUT` with
   partial logs.
3. The reconciler sweeps jobs whose row has not been updated for longer than the
   runner deadline plus `RECONCILE_GRACE_SECONDS`, and marks them `TIMEOUT` -
   this is the one that covers an evicted or deleted Pod that never reported.

`WAITING_FOR_CODE` is deliberately exempt from (3): it is waiting for a person or
agent to push, and expires only after `WAITING_FOR_CODE_TTL_SECONDS` (24 hours
by default), with `summary.reason: waiting_for_code_expired`.

Triage a job that looks stuck:

```console
api $GATEWAY/api/v1/test-jobs/$JOB_ID | python -m json.tool | head -30   # status and updated_at
kubectl -n $RUNNERS get jobs -l ai-test.openai.com/job-id=$JOB_ID
kubectl -n $RUNNERS describe job <name>          # Pending usually means quota or image pull
kubectl -n $RUNNERS get resourcequota runner-quota
```

If the Job is Pending against the quota, either raise the quota or cancel older
jobs. If the Pod is gone and the row is still non-terminal, let the next sweep
close it, or run the sweep immediately (below).

## Cleanup and reconciliation

The `reconciler` CronJob runs `python -m app.reconcile` every five minutes. One
sweep performs four idempotent duties:

| Duty | What it does |
| --- | --- |
| Timeout | Non-terminal jobs past their deadline become `TIMEOUT`, with their Kubernetes Job deleted and one notification enqueued |
| Release | Terminal jobs that still name a Kubernetes Job have it deleted and the name cleared |
| Retention | Artifacts of jobs completed more than `ARTIFACT_RETENTION_DAYS` ago lose their bytes; the row keeps digest, size and type |
| Notifications | `PENDING` notifications are re-delivered |

Run one immediately, out of schedule:

```console
kubectl -n $NS create job reconcile-now --from=cronjob/reconciler
kubectl -n $NS logs job/reconcile-now -f
```

Delivery is at-least-once by design: the gateway runs more than one replica and
the sweep can overlap with a replica's own retry, so the same completion can be
sent twice. The completion workflow forwards a stable `Idempotency-Key`, and the
sink must persist it before creating a comment - see
[../n8n/README.md](../n8n/README.md). Duplicate comments in a chat channel are
the symptom of a sink that ignores that header.

Tuning knobs are environment variables on both the Deployment and the CronJob:
`RUNNER_ACTIVE_DEADLINE_SECONDS`, `RUNNER_TTL_SECONDS`,
`RECONCILE_GRACE_SECONDS`, `WAITING_FOR_CODE_TTL_SECONDS`,
`ARTIFACT_RETENTION_DAYS`. Keep the CronJob and the Deployment in agreement:
the launcher writes the deadline into every Job it creates, and the reconciler
uses the same value to decide what is overdue.

## Secret rotation

Rotate on a schedule and immediately after any suspected exposure. Callback
tokens are split per workload type precisely so each can be rotated on its own.

**Runner callback tokens (`codex-runner-token`, `test-runner-token`).** The
gateway accepts every configured runner token at once, so rotation has no
window where in-flight runners are rejected:

1. Set the new value in `ai-test-gateway` under a *different* key and add it to
   the Deployment as `GATEWAY_RUNNER_TOKEN` (the shared slot, accepted
   alongside the per-workload ones). Restart the gateway.
2. Update `ai-test-codex-callback` or `ai-test-test-callback` in
   `ai-test-runners` with the new value. New Jobs pick it up; running Jobs keep
   working on the old token.
3. When no Job older than the deadline remains, move the new value into
   `codex-runner-token` / `test-runner-token`, clear `GATEWAY_RUNNER_TOKEN` and
   restart the gateway.

**API token (`api-token`).** Shared with n8n. Update the gateway Secret and the
n8n credential together, then restart the gateway; plan intake is idempotent, so
a request rejected mid-rotation can simply be resent.

**Model credential (`ai-test-codex-auth`).** Update the Secret, then let running
Codex Jobs finish or cancel them. Nothing else references it - and if it ever
appears in a test Job's environment, that is an incident, not a rotation.

**Artifact signing key (`artifact-signing-key`).** Rotating invalidates
outstanding artifact links, which live for five minutes. Rotate, restart, done.

**Completion webhook secret (`completion-webhook-secret`).** Must change on the
gateway and in the n8n workflow together; deliveries signed with the old secret
are rejected, but they stay `PENDING` and are retried, so a short mismatch is
survivable.

**Git read credential.** Replace the Secret in `ai-test-runners` only. It must
never be the same identity as the bot that opens pull requests, which lives with
n8n.

After any rotation, re-run the [post-deploy checks](#after-every-deploy).

## Incidents

**A notification is `DEAD`.** Four delivery attempts failed. Fix the sink
first, then post the comment or message from the job's status endpoint, which is
the authoritative record of the run. The outbox does not resurrect dead rows on
its own, deliberately: a sink that was broken for an hour would otherwise come
back to a storm of duplicate comments.

```console
kubectl -n $NS exec deploy/gateway -- python -c "
from app.config import Settings
from app.db import create_database_engine, create_session_factory
from app.repositories.models import NotificationRow
from sqlalchemy import select
engine = create_database_engine(Settings.from_env().database_url)
with create_session_factory(engine)() as session:
    for row in session.scalars(select(NotificationRow).where(NotificationRow.status == 'DEAD')):
        print(row.job_id, row.event_type, row.last_error)
"
```

**Artifact volume is full.** Lower `ARTIFACT_RETENTION_DAYS`, run the sweep out
of schedule, and confirm reclaimed bytes in the sweep output
(`purged_bytes`). Artifacts are immutable, so nothing is rewritten in place.

**Kubernetes API is unavailable.** The gateway answers `202` and records the
job, then dispatch fails and the job goes to `ERROR` with
`INFRASTRUCTURE`. Nothing is lost; resubmit the plan after recovery. Job
deletions that failed are retried by the next sweep because the Job name stays
recorded.

**Suspected credential exposure in a test Job.** Test Jobs execute
repository-controlled code, which is why they never receive the model
credential. If one ever does, treat it as compromised: rotate
`ai-test-codex-auth` first, then find how it got there:

```console
kubectl -n $RUNNERS get job <name> -o json | python -m json.tool | grep -A3 secretKeyRef
```

**A patch tried to touch production code.** That is the `BLOCKED` path working:
`failure_class` is `POLICY` and no test Job is created. Read
`generation-result.json` for the denied paths. No action is needed beyond
telling the plan author.

## Staging end-to-end verification

Run this after any change to the pipeline, before promoting a build.

```console
# 1. Plan intake. Must answer 202 with a job_id in well under five seconds.
python scripts/submit-test-plan.py \
  --gateway $GATEWAY --plan docs/test-plans/example.yaml \
  --callback-url https://n8n.example/webhook/test-job-completed

# 2. Prepare runs while nothing waits on it.
api $GATEWAY/api/v1/test-jobs/$JOB_ID | python -m json.tool | grep status
kubectl -n $RUNNERS get jobs -l app.kubernetes.io/component=prepare

# 3. Push a commit whose message carries the job footer, then watch the
#    generate and test Jobs appear for that exact SHA.
kubectl -n $RUNNERS get jobs -l ai-test.openai.com/job-id=$JOB_ID \
  -o custom-columns=NAME:.metadata.name,SHA:.metadata.annotations.ai-test\\.openai\\.com/code-sha

# 4. Happy path: status PASSED, tested_sha equal to the pushed SHA, artifacts
#    present, one notification in n8n.
# 5. Failure path: push a commit that breaks a test and confirm FAILED with
#    failure_class TEST_FAILURE and a RAW_LOG artifact naming the test.
```

The same two paths, plus the blocked-patch and superseded-commit paths, run
without a cluster in `gateway/tests/test_end_to_end.py`. That suite starts a
real gateway, a real Git repository and the real test-runner process, so a
staging run is confirming the cluster wiring rather than the pipeline logic:

```console
cd gateway && python -m pytest tests/test_end_to_end.py -v
```

## What to watch

Log and alert on these; the gateway already emits `job_id`, `task_id`,
`repository`, `phase`, `attempt`, `base_sha` and `code_sha` as structured
fields.

| Signal | Why it matters |
| --- | --- |
| Jobs stuck in `PREPARE_QUEUED` or `TEST_QUEUED` | Dispatch or quota problem, not a test problem |
| `timed_out` non-empty in consecutive sweeps | Runners are dying without reporting |
| `DEAD` notifications | Results are finishing but nobody is being told |
| Kubernetes Job creation failures | RBAC, quota or image pull |
| Patch policy rejections | Expected occasionally; a spike means a prompt or policy regression |
| `purged_bytes` and PVC usage | Retention is keeping up with volume |

Never log authorization headers, model or API keys, Git credentials, full
environment dumps, or whole plans and source files.
