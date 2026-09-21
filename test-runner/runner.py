from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

FULL_SHA = __import__("re").compile(r"^[0-9a-f]{40}$")
SAFE_ENVIRONMENT_KEYS = {
    "CI",
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONPATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
}


class RunnerError(RuntimeError):
    def __init__(self, failure_class: str, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class


@dataclass(frozen=True, slots=True)
class CommandResult:
    name: str
    command_id: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    output: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    type: str
    object_key: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class RunnerConfiguration:
    job_id: str
    task_id: str
    clone_url: str
    base_sha: str
    code_sha: str
    patch_path: Path
    patch_sha256: str
    artifact_root: Path
    gateway_url: str
    runner_token: str
    attempt: int
    command_slots: dict[str, str]
    command_argv: dict[str, list[str]]
    command_timeouts: dict[str, int]
    allowed_paths: list[str]
    denied_paths: list[str]
    junit_globs: list[str]
    coverage_globs: list[str]
    allow_skipped_required_tests: bool
    max_log_bytes: int
    precloned_repository: Path | None = None
    artifact_gateway_url: str | None = None
    patch_object_key: str | None = None


def sanitized_test_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    environment = source or os.environ
    clean = {
        key: value
        for key, value in environment.items()
        if key.upper() in SAFE_ENVIRONMENT_KEYS
    }
    clean["CI"] = "true"
    return clean


def checkout_exact_sha(
    clone_url: str,
    code_sha: str,
    parent: Path,
    precloned_repository: Path | None = None,
) -> Path:
    if not FULL_SHA.fullmatch(code_sha):
        raise RunnerError("POLICY_VIOLATION", "checkout requires a full Git SHA")
    git_environment = sanitized_test_environment()
    git_environment["GIT_TERMINAL_PROMPT"] = "0"
    if precloned_repository is not None:
        workspace = precloned_repository.resolve()
        if not workspace.is_dir():
            raise RunnerError(
                "ENVIRONMENT_FAILED", "pre-cloned repository is missing"
            )
        actual = run_checked(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            cwd=parent,
            timeout_seconds=10,
            environment=git_environment,
            failure_class="ENVIRONMENT_FAILED",
        ).strip()
        if actual != code_sha:
            raise RunnerError(
                "ENVIRONMENT_FAILED",
                "pre-cloned repository SHA does not match code SHA",
            )
        return workspace

    workspace = parent / "repository"
    run_checked(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            clone_url,
            str(workspace),
        ],
        cwd=parent,
        timeout_seconds=120,
        environment=git_environment,
        failure_class="ENVIRONMENT_FAILED",
    )
    run_checked(
        ["git", "-C", str(workspace), "fetch", "--depth=1", "origin", code_sha],
        cwd=parent,
        timeout_seconds=120,
        environment=git_environment,
        failure_class="ENVIRONMENT_FAILED",
    )
    run_checked(
        ["git", "-C", str(workspace), "checkout", "--detach", code_sha],
        cwd=parent,
        timeout_seconds=60,
        environment=git_environment,
        failure_class="ENVIRONMENT_FAILED",
    )
    actual = run_checked(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        cwd=parent,
        timeout_seconds=10,
        environment=git_environment,
        failure_class="ENVIRONMENT_FAILED",
    ).strip()
    if actual != code_sha:
        raise RunnerError(
            "ENVIRONMENT_FAILED", "checked out SHA does not match code SHA"
        )
    return workspace


def verify_and_apply_patch(configuration: RunnerConfiguration, workspace: Path) -> None:
    patch = configuration.patch_path.read_bytes()
    if not hashlib.sha256(patch).hexdigest() == configuration.patch_sha256:
        raise RunnerError(
            "PATCH_APPLY_FAILED", "patch checksum does not match metadata"
        )
    for arguments in (
        ["git", "apply", "--check", "-"],
        ["git", "apply", "--index", "-"],
    ):
        completed = subprocess.run(
            arguments,
            cwd=workspace,
            input=patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            env=sanitized_test_environment(),
            check=False,
        )
        if completed.returncode != 0:
            raise RunnerError(
                "PATCH_APPLY_FAILED", "validated patch could not be applied"
            )
    changed = run_checked(
        ["git", "diff", "--cached", "--name-only", "-z", configuration.code_sha],
        cwd=workspace,
        timeout_seconds=30,
        environment=sanitized_test_environment(),
        failure_class="PATCH_APPLY_FAILED",
    ).split("\0")
    for path in filter(None, changed):
        ensure_safe_path(path)
        if not any(
            glob_matches(path, pattern) for pattern in configuration.allowed_paths
        ):
            raise RunnerError("POLICY_VIOLATION", f"patch path is not allowed: {path}")
        if any(glob_matches(path, pattern) for pattern in configuration.denied_paths):
            raise RunnerError("POLICY_VIOLATION", f"patch path is denied: {path}")


def execute_commands(
    configuration: RunnerConfiguration, workspace: Path
) -> list[CommandResult]:
    results: list[CommandResult] = []
    environment = sanitized_test_environment()
    for name in ("install", "build", "unit", "integration", "e2e"):
        command_id = configuration.command_slots.get(name)
        if not command_id:
            continue
        argv = configuration.command_argv.get(command_id)
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(argument, str) and argument for argument in argv)
        ):
            raise RunnerError(
                "POLICY_VIOLATION", f"command ID {command_id!r} has no argv mapping"
            )
        started = time.monotonic()
        timed_out = False
        exit_code: int | None
        output: str
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        try:
            output, _ = process.communicate(
                timeout=configuration.command_timeouts.get(name, 300)
            )
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(process)
            output, _ = process.communicate()
            exit_code = None
        if len(output.encode("utf-8")) > configuration.max_log_bytes:
            output = output.encode("utf-8")[: configuration.max_log_bytes].decode(
                "utf-8", errors="replace"
            )
        results.append(
            CommandResult(
                name=name,
                command_id=command_id,
                exit_code=exit_code,
                duration_seconds=round(time.monotonic() - started, 3),
                timed_out=timed_out,
                output=output,
            )
        )
        if timed_out or exit_code != 0:
            break
    return results


def parse_junit(workspace: Path, patterns: list[str]) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for pattern in patterns:
        for path in glob.glob(str(workspace / pattern), recursive=True):
            root = ET.parse(path).getroot()
            suites = (
                [root]
                if root.tag == "testsuite"
                else list(root.findall(".//testsuite"))
            )
            for suite in suites:
                tests = int(suite.attrib.get("tests", "0"))
                failures = int(suite.attrib.get("failures", "0")) + int(
                    suite.attrib.get("errors", "0")
                )
                skipped = int(suite.attrib.get("skipped", "0"))
                counts["failed"] += failures
                counts["skipped"] += skipped
                counts["passed"] += max(0, tests - failures - skipped)
    return counts


def parse_coverage(workspace: Path, patterns: list[str]) -> dict[str, float] | None:
    for pattern in patterns:
        for path in glob.glob(str(workspace / pattern), recursive=True):
            candidate = Path(path)
            if not candidate.is_file() or candidate.suffix.lower() != ".json":
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if "total" in payload:
                    total = payload["total"]
                    return {
                        "lines": float(total["lines"]["pct"]),
                        "branches": float(total["branches"]["pct"]),
                    }
                if "totals" in payload:
                    totals = payload["totals"]
                    coverage = {"lines": float(totals["percent_covered"])}
                    if totals.get("num_branches"):
                        coverage["branches"] = round(
                            100
                            * float(totals.get("covered_branches", 0))
                            / float(totals["num_branches"]),
                            2,
                        )
                    return coverage
                if "lines" in payload:
                    coverage = {"lines": float(payload["lines"])}
                    if "branches" in payload:
                        coverage["branches"] = float(payload["branches"])
                    return coverage
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return None


def classify_failure(results: list[CommandResult]) -> dict[str, str] | None:
    failed = next(
        (result for result in results if result.timed_out or result.exit_code != 0),
        None,
    )
    if failed is None:
        return None
    if failed.timed_out:
        failure_class = "COMMAND_TIMEOUT"
    elif failed.name == "install":
        failure_class = "DEPENDENCY_INSTALL_FAILED"
    elif failed.name == "build":
        failure_class = "BUILD_FAILED"
    elif any(
        marker in failed.output for marker in ("SyntaxError", "CompileError", "TS")
    ):
        failure_class = "TEST_COMPILE_FAILED"
    else:
        failure_class = "TEST_ASSERTION_FAILED"
    return {
        "class": failure_class,
        "message": f"command {failed.command_id} did not complete successfully",
        "command_id": failed.command_id,
    }


def run(configuration: RunnerConfiguration) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ai-test-runner-") as directory:
        if configuration.artifact_gateway_url and configuration.patch_object_key:
            download_artifact(
                configuration.artifact_gateway_url,
                configuration.runner_token,
                configuration.patch_object_key,
                configuration.patch_path,
            )
        workspace = checkout_exact_sha(
            configuration.clone_url,
            configuration.code_sha,
            Path(directory),
            configuration.precloned_repository,
        )
        verify_and_apply_patch(configuration, workspace)
        results = execute_commands(configuration, workspace)
        summary = parse_junit(workspace, configuration.junit_globs)
        failure = classify_failure(results)
        status = (
            "PASSED"
            if failure is None
            else ("TIMEOUT" if failure["class"] == "COMMAND_TIMEOUT" else "FAILED")
        )
        if summary["skipped"] and not configuration.allow_skipped_required_tests:
            status = "FAILED"
            failure = {
                "class": "TEST_ASSERTION_FAILED",
                "message": "required test suite contains skipped tests",
            }
        completed_at = datetime.now(UTC)
        result: dict[str, Any] = {
            "schema_version": "1.0",
            "job_id": configuration.job_id,
            "task_id": configuration.task_id,
            "base_sha": configuration.base_sha,
            "tested_sha": configuration.code_sha,
            "patch_sha256": configuration.patch_sha256,
            "status": status,
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
            "duration_seconds": round(time.monotonic() - started, 3),
            "summary": summary,
            "commands": [
                {key: value for key, value in asdict(item).items() if key != "output"}
                for item in results
            ],
            "coverage": parse_coverage(workspace, configuration.coverage_globs),
            "failures": [failure] if failure else [],
            "artifacts": [],
            "uncovered_acceptance_criteria": [],
        }
        artifacts = persist_results(configuration, results, workspace)
        result["artifacts"] = [asdict(artifact) for artifact in artifacts]
        result_path = configuration.artifact_root / (
            f"jobs/{configuration.job_id}/attempts/{configuration.attempt}/test-result.json"
        )
        result_artifact = store_bytes(
            configuration.artifact_root,
            result_path.relative_to(configuration.artifact_root).as_posix(),
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode(),
            "RESULT",
            "application/json",
            gateway_url=configuration.artifact_gateway_url,
            gateway_token=configuration.runner_token,
        )
        result["artifacts"].append(asdict(result_artifact))
        return result


def persist_results(
    configuration: RunnerConfiguration,
    commands: list[CommandResult],
    workspace: Path,
) -> list[StoredArtifact]:
    prefix = f"jobs/{configuration.job_id}/attempts/{configuration.attempt}"
    artifacts = [
        store_bytes(
            configuration.artifact_root,
            f"{prefix}/test-runner.log",
            "\n".join(
                f"[{command.name}:{command.command_id}]\n{command.output}"
                for command in commands
            ).encode(),
            "RAW_LOG",
            "text/plain",
            gateway_url=configuration.artifact_gateway_url,
            gateway_token=configuration.runner_token,
        )
    ]
    for artifact_type, patterns, media_type in (
        ("JUNIT", configuration.junit_globs, "application/xml"),
        ("COVERAGE", configuration.coverage_globs, "application/octet-stream"),
    ):
        for pattern in patterns:
            for source_name in glob.glob(str(workspace / pattern), recursive=True):
                source = Path(source_name)
                if not source.is_file():
                    continue
                artifacts.append(
                    store_bytes(
                        configuration.artifact_root,
                        f"{prefix}/{artifact_type.lower()}/{source.name}",
                        source.read_bytes(),
                        artifact_type,
                        media_type,
                        gateway_url=configuration.artifact_gateway_url,
                        gateway_token=configuration.runner_token,
                    )
                )
    return artifacts


def store_bytes(
    root: Path,
    object_key: str,
    content: bytes,
    artifact_type: str,
    media_type: str,
    *,
    gateway_url: str | None = None,
    gateway_token: str | None = None,
) -> StoredArtifact:
    ensure_safe_path(object_key)
    destination = root.joinpath(*PurePosixPath(object_key).parts).resolve()
    root = root.resolve()
    if not destination.is_relative_to(root):
        raise RunnerError("POLICY_VIOLATION", "artifact key escapes storage root")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise RunnerError("ENVIRONMENT_FAILED", "immutable artifact key conflict")
    else:
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    artifact = StoredArtifact(
        type=artifact_type,
        object_key=object_key,
        sha256=digest,
        size_bytes=len(content),
        media_type=media_type,
    )
    if gateway_url:
        upload_artifact(gateway_url, gateway_token or "", artifact, content)
    return artifact


def download_artifact(
    gateway_url: str, token: str, object_key: str, destination: Path
) -> None:
    ensure_safe_path(object_key)
    request = urllib.request.Request(
        artifact_url(gateway_url, object_key),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
    except (OSError, urllib.error.HTTPError) as error:
        raise RunnerError("ENVIRONMENT_FAILED", "artifact download failed") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def upload_artifact(
    gateway_url: str,
    token: str,
    artifact: StoredArtifact,
    content: bytes,
) -> None:
    request = urllib.request.Request(
        artifact_url(gateway_url, artifact.object_key),
        data=content,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            stored = json.loads(response.read())
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        raise RunnerError("ENVIRONMENT_FAILED", "artifact upload failed") from error
    if (
        stored.get("object_key") != artifact.object_key
        or stored.get("sha256") != artifact.sha256
        or stored.get("size_bytes") != artifact.size_bytes
    ):
        raise RunnerError(
            "ENVIRONMENT_FAILED", "artifact gateway returned mismatched metadata"
        )


def artifact_url(gateway_url: str, object_key: str) -> str:
    from urllib.parse import quote

    return f"{gateway_url.rstrip('/')}/{'/'.join(quote(part, safe='') for part in object_key.split('/'))}"


def post_callback(
    url: str, token: str, body: dict[str, Any], attempts: int = 4
) -> None:
    encoded = json.dumps(body).encode()
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            data=encoded,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if 200 <= response.status < 300:
                    return
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise RunnerError("CALLBACK_FAILED", "callback was rejected") from error
            last_error = error
        except OSError as error:
            last_error = error
        if attempt + 1 < attempts:
            time.sleep(0.25 * (2**attempt))
    raise RunnerError(
        "CALLBACK_FAILED", "callback retry budget exhausted"
    ) from last_error


def run_checked(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    environment: dict[str, str],
    failure_class: str,
) -> str:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunnerError(failure_class, f"command failed: {argv[0]}") from error
    if completed.returncode != 0:
        raise RunnerError(failure_class, f"command failed: {argv[0]}")
    return completed.stdout


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
        time.sleep(0.2)
        process.kill()
    else:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


def glob_matches(path: str, pattern: str) -> bool:
    if "**/" in pattern:
        return fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(
            path, pattern.replace("**/", "")
        )
    return fnmatch.fnmatchcase(path, pattern)


def ensure_safe_path(path: str) -> None:
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts or "\\" in path:
        raise RunnerError("POLICY_VIOLATION", "unsafe relative path")


def load_configuration() -> RunnerConfiguration:
    policy = json.loads(required("RUNNER_POLICY_JSON"))
    artifact_gateway_url = os.getenv("ARTIFACT_GATEWAY_URL")
    return RunnerConfiguration(
        job_id=required("JOB_ID"),
        task_id=required("TASK_ID"),
        clone_url=required("GIT_CLONE_URL"),
        base_sha=required("BASE_SHA"),
        code_sha=required("CODE_SHA"),
        patch_path=Path(required("PATCH_PATH")),
        patch_sha256=required("PATCH_SHA256"),
        artifact_root=Path(required("ARTIFACT_ROOT")),
        gateway_url=required("GATEWAY_URL"),
        runner_token=required("RUNNER_TOKEN"),
        attempt=int(os.getenv("ATTEMPT", "1")),
        command_slots=policy["command_slots"],
        command_argv=policy["command_argv"],
        command_timeouts=policy["command_timeouts"],
        allowed_paths=policy["allowed_paths"],
        denied_paths=policy["denied_paths"],
        junit_globs=policy.get("junit_globs", []),
        coverage_globs=policy.get("coverage_globs", []),
        allow_skipped_required_tests=policy.get("allow_skipped_required_tests", False),
        max_log_bytes=int(policy.get("max_log_bytes", 10_485_760)),
        precloned_repository=(
            Path(value) if (value := os.getenv("PRECLONED_REPOSITORY")) else None
        ),
        artifact_gateway_url=artifact_gateway_url,
        patch_object_key=(
            required("PATCH_OBJECT_KEY") if artifact_gateway_url else None
        ),
    )


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RunnerError("ENVIRONMENT_FAILED", f"required setting {name} is missing")
    return value


def event(
    configuration: RunnerConfiguration,
    status: str,
    artifacts: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": f"evt_{configuration.job_id[4:]}_test_{configuration.attempt}_{status.lower()}",
        "attempt": configuration.attempt,
        "phase": "TEST",
        "status": status,
        "code_sha": configuration.code_sha,
        "artifacts": artifacts,
        "summary": summary,
        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def main() -> int:
    configuration = load_configuration()
    callback_url = (
        f"{configuration.gateway_url.rstrip('/')}/internal/v1/test-jobs/"
        f"{configuration.job_id}/events"
    )
    post_callback(
        callback_url,
        configuration.runner_token,
        event(configuration, "TESTING", [], {}),
    )
    try:
        result = run(configuration)
        post_callback(
            callback_url,
            configuration.runner_token,
            event(
                configuration,
                result["status"],
                result["artifacts"],
                {"result": result},
            ),
        )
        return 0 if result["status"] == "PASSED" else 1
    except RunnerError as error:
        post_callback(
            callback_url,
            configuration.runner_token,
            event(
                configuration,
                "ERROR",
                [],
                {"failure_class": error.failure_class, "error": str(error)[:500]},
            ),
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
