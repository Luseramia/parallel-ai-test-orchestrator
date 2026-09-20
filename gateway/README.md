# Gateway

FastAPI service that owns job state, authorization, idempotency, repository
policy, Kubernetes Job launch and artifact metadata. It is the only component
that talks to PostgreSQL and the only identity that talks to the Kubernetes API.

## Running the tests

```console
python -m pytest tests -v
```

The suite uses temporary SQLite databases, a fake Kubernetes launcher and fake
Git and notification senders, so it needs no cluster. Two groups are worth
knowing about:

- `tests/test_k8s_manifests.py` parses everything under `k8s/` and asserts the
  security properties the design depends on, including byte-equality between
  the Job templates and what the launcher actually POSTs.
- `tests/test_end_to_end.py` starts a real HTTP server, a real Git repository
  and the real test-runner process, and drives the pipeline from plan intake to
  the completion notification. It needs `git` on PATH.

The four PostgreSQL integration tests skip unless `TEST_DATABASE_URL` points at
a live database. The Alembic migrations are PostgreSQL-targeted and add a
trigger that makes job events append-only.

## Processes

| Entry point | Purpose |
| --- | --- |
| `uvicorn app.main:create_app --factory` | The API |
| `python -m app.reconcile` | One cleanup and reconciliation sweep, run by the reconciler CronJob |

Operational procedures are in [../docs/operations.md](../docs/operations.md).
