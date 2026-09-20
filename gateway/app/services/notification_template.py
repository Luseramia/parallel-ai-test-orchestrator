from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.test_jobs import JobStatus, TestJob


def render_completion_message(payload: dict[str, Any]) -> str:
    """Render a deterministic, idempotency-friendly Markdown result summary."""

    summary = payload.get("summary")
    counts = summary if isinstance(summary, dict) else {}
    tested_sha = payload.get("tested_sha") or "not-executed"
    lines = [
        f"## AI test job {payload['status']}",
        "",
        f"- Job: `{payload['job_id']}`",
        f"- Tested SHA: `{tested_sha}`",
        f"- Attempt: `{payload['attempt']}`",
    ]
    if any(name in counts for name in ("passed", "failed", "skipped")):
        lines.append(
            "- Tests: "
            f"{int(counts.get('passed', 0))} passed, "
            f"{int(counts.get('failed', 0))} failed, "
            f"{int(counts.get('skipped', 0))} skipped"
        )
    duration = counts.get("duration_seconds")
    if isinstance(duration, int | float):
        lines.append(f"- Duration: `{duration:.3f}s`")
    lines.extend(("", f"Result: {payload['status_url']}"))
    return "\n".join(lines)


def build_terminal_payload(
    *,
    job: TestJob,
    status: JobStatus,
    attempt: int,
    summary: dict[str, Any],
    occurred_at: datetime,
) -> dict[str, Any]:
    """Build the completion payload for a terminal status decided by the gateway.

    Runner callbacks carry their own SHAs and summary, so they build the
    payload from the event. Gateway-side terminals - cancel and the reconciler
    timeout sweep - have only the stored job, and share this shape so n8n sees
    one message format.
    """

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "job_id": job.id,
        "task_id": job.task_id,
        "status": status.value,
        "base_sha": job.base_sha,
        "code_sha": job.code_sha,
        "tested_sha": job.tested_sha,
        "attempt": attempt,
        "summary": summary,
        "status_url": f"/api/v1/test-jobs/{job.id}",
        "occurred_at": occurred_at.isoformat(),
    }
    payload["message"] = render_completion_message(payload)
    return payload
