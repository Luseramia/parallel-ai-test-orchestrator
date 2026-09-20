# Database migrations

Set `DATABASE_URL` to a PostgreSQL DSN and run:

```console
alembic upgrade head
```

The first migration creates the durable jobs, verify-attempt identities,
append-only events, artifact metadata, and general idempotency-key records.
On PostgreSQL, update and delete operations against the event table are
rejected by a trigger.

