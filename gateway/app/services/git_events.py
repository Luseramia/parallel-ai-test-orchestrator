from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

JOB_PATTERN = re.compile(r"^atj_[0-9a-f]{32}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TRAILER_PATTERN = re.compile(r"^(?P<key>[A-Za-z0-9-]+):[ \t]*(?P<value>.+)$")


class CommitFooterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VerificationFooter:
    job_id: str
    phase: str


def parse_verification_footer(message: str) -> VerificationFooter:
    stripped = message.rstrip()
    if not stripped:
        raise CommitFooterError("commit message is empty")
    trailer_block = stripped.split("\n\n")[-1]
    trailers: dict[str, list[str]] = {}
    for line in trailer_block.splitlines():
        match = TRAILER_PATTERN.fullmatch(line)
        if not match:
            raise CommitFooterError("commit footer block contains an invalid trailer")
        trailers.setdefault(match.group("key"), []).append(match.group("value").strip())
    jobs = trailers.get("AI-Test-Job", [])
    phases = trailers.get("AI-Test-Phase", [])
    if len(jobs) != 1 or len(phases) != 1:
        raise CommitFooterError("commit must contain exactly one AI test job and phase")
    if not JOB_PATTERN.fullmatch(jobs[0]):
        raise CommitFooterError("AI-Test-Job is invalid")
    if phases[0] != "verify":
        raise CommitFooterError("AI-Test-Phase must be verify")
    return VerificationFooter(job_id=jobs[0], phase=phases[0])


def select_head_verification_footer(
    commits: list[dict[str, Any]], after_sha: str
) -> VerificationFooter:
    if not SHA_PATTERN.fullmatch(after_sha):
        raise CommitFooterError("push after SHA must be a full lowercase SHA")
    matches = [commit for commit in commits if commit.get("id") == after_sha]
    if len(matches) != 1:
        raise CommitFooterError("push payload does not contain exactly one head commit")
    message = matches[0].get("message")
    if not isinstance(message, str):
        raise CommitFooterError("head commit message is missing")
    return parse_verification_footer(message)
