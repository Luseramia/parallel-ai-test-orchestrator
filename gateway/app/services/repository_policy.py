from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


class PolicyValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PatchPolicy:
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    allow_new_files: bool
    allow_deletes: bool
    allow_renames: bool
    allow_binary: bool
    allow_symlinks: bool
    allow_submodules: bool
    allow_mode_changes: bool
    max_files: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    id: str
    clone_url: str
    allowed_branches: tuple[str, ...]
    command_slots: dict[str, str]
    command_argv: dict[str, tuple[str, ...]]
    patch: PatchPolicy
    limits: dict[str, int]
    junit_globs: tuple[str, ...]
    coverage_globs: tuple[str, ...]

    def allows_branch(self, branch: str) -> bool:
        return any(
            fnmatch.fnmatchcase(branch, pattern) for pattern in self.allowed_branches
        )


class RepositoryPolicyRegistry:
    def __init__(self, policies: dict[str, RepositoryPolicy]) -> None:
        self._policies = policies

    @classmethod
    def load(cls, path: Path) -> RepositoryPolicyRegistry:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise PolicyValidationError("repository policy version must be 1")
        raw_repositories = payload.get("repositories")
        if not isinstance(raw_repositories, list) or not raw_repositories:
            raise PolicyValidationError("repository policy must contain repositories")
        policies: dict[str, RepositoryPolicy] = {}
        for raw in raw_repositories:
            policy = _parse_repository(raw)
            if policy.id in policies:
                raise PolicyValidationError(
                    f"duplicate repository policy {policy.id!r}"
                )
            policies[policy.id] = policy
        return cls(policies)

    def get(self, repository: str) -> RepositoryPolicy:
        try:
            return self._policies[repository]
        except KeyError as error:
            raise PolicyValidationError("repository is not allowlisted") from error


def _parse_repository(raw: dict[str, Any]) -> RepositoryPolicy:
    patch = raw.get("patch", {})
    commands = raw.get("commands", {})
    command_argv = raw.get("command_argv", {})
    if not isinstance(commands, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in commands.items()
    ):
        raise PolicyValidationError("commands must map phase names to command IDs")
    if not isinstance(command_argv, dict) or not all(
        isinstance(key, str)
        and isinstance(value, list)
        and value
        and all(isinstance(argument, str) and argument for argument in value)
        for key, value in command_argv.items()
    ):
        raise PolicyValidationError("command_argv must map command IDs to argv arrays")
    missing = set(commands.values()) - set(command_argv)
    if missing:
        raise PolicyValidationError(
            f"commands have no server-side argv mapping: {', '.join(sorted(missing))}"
        )
    return RepositoryPolicy(
        id=str(raw["id"]),
        clone_url=str(raw["clone_url"]),
        allowed_branches=tuple(str(value) for value in raw["allowed_branches"]),
        command_slots={str(key): str(value) for key, value in commands.items()},
        command_argv={
            str(key): tuple(str(argument) for argument in value)
            for key, value in command_argv.items()
        },
        patch=PatchPolicy(
            allowed_paths=tuple(str(value) for value in patch["allowed_paths"]),
            denied_paths=tuple(str(value) for value in patch["denied_paths"]),
            allow_new_files=bool(patch.get("allow_new_files", True)),
            allow_deletes=bool(patch.get("allow_deletes", False)),
            allow_renames=bool(patch.get("allow_renames", False)),
            allow_binary=bool(patch.get("allow_binary", False)),
            allow_symlinks=bool(patch.get("allow_symlinks", False)),
            allow_submodules=bool(patch.get("allow_submodules", False)),
            allow_mode_changes=bool(patch.get("allow_mode_changes", False)),
            max_files=int(patch.get("max_files", 40)),
            max_bytes=int(patch.get("max_bytes", 524_288)),
        ),
        limits={str(key): int(value) for key, value in raw.get("limits", {}).items()},
        junit_globs=tuple(
            str(value) for value in raw.get("artifacts", {}).get("junit_globs", [])
        ),
        coverage_globs=tuple(
            str(value) for value in raw.get("artifacts", {}).get("coverage_globs", [])
        ),
    )


def ensure_safe_relative_path(path: str) -> None:
    candidate = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "" in candidate.parts
    ):
        raise PolicyValidationError(
            "path must be a safe repository-relative POSIX path"
        )


def static_glob_prefix(pattern: str) -> tuple[str, ...]:
    parts: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if any(character in part for character in "*?["):
            break
        parts.append(part)
    return tuple(parts)


def glob_patterns_overlap(left: str, right: str) -> bool:
    left_prefix = static_glob_prefix(left)
    right_prefix = static_glob_prefix(right)
    shorter = min(len(left_prefix), len(right_prefix))
    return left_prefix[:shorter] == right_prefix[:shorter]
