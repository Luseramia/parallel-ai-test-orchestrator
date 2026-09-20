#!/usr/bin/env python3
"""Submit a test plan to the gateway and print the job it created.

The gateway requires ``Idempotency-Key: <task_id>:<base_sha>:<plan digest>``,
where the digest is SHA-256 over the canonical JSON form of the plan. Computing
that by hand is error-prone, which is the only reason this script exists.

    python scripts/submit-test-plan.py \\
        --gateway https://ai-test.internal \\
        --plan docs/test-plans/example.yaml \\
        --callback-url https://n8n.example/webhook/test-job-completed

The API token comes from --token or the GATEWAY_API_TOKEN environment variable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def load_plan(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # imported lazily so JSON plans need no dependency

        return yaml.safe_load(text)
    return json.loads(text)


def canonical(plan: dict[str, Any]) -> bytes:
    return json.dumps(
        plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def submit(
    gateway: str, token: str, plan: dict[str, Any], callback_url: str
) -> dict[str, Any]:
    digest = hashlib.sha256(canonical(plan)).hexdigest()
    body = json.dumps(
        {
            "task_id": plan["task_id"],
            "repository": plan["repository"],
            "branch": plan["branch"],
            "base_sha": plan["base_sha"],
            "plan": plan,
            "callback_url": callback_url,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{gateway.rstrip('/')}/api/v1/test-jobs/prepare",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": f"{plan['task_id']}:{plan['base_sha']}:{digest}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True, help="gateway base URL")
    parser.add_argument("--plan", required=True, type=Path, help="plan YAML or JSON")
    parser.add_argument("--callback-url", required=True, help="completion webhook")
    parser.add_argument("--token", default=os.getenv("GATEWAY_API_TOKEN", ""))
    arguments = parser.parse_args(argv)
    if not arguments.token:
        parser.error("set --token or GATEWAY_API_TOKEN")

    plan = load_plan(arguments.plan)
    try:
        result = submit(
            arguments.gateway, arguments.token, plan, arguments.callback_url
        )
    except urllib.error.HTTPError as error:
        # The gateway explains plan and policy rejections in the body; printing
        # it is the whole point of running this by hand.
        print(f"HTTP {error.code}: {error.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print(
        f"\nStatus: {arguments.gateway.rstrip('/')}{result['status_url']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
