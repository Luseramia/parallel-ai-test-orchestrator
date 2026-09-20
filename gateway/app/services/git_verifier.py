from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

import httpx

from app.services.repository_policy import RepositoryPolicy


class GitVerificationError(ValueError):
    pass


class GitVerifier(Protocol):
    def verify_reachable(
        self, policy: RepositoryPolicy, branch: str, code_sha: str
    ) -> None: ...


class GitHubApiVerifier:
    def __init__(self, token: str | None, timeout_seconds: float = 10.0) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds

    def verify_reachable(
        self, policy: RepositoryPolicy, branch: str, code_sha: str
    ) -> None:
        prefix = "github.com/"
        if not policy.id.startswith(prefix):
            raise GitVerificationError("repository policy is not a GitHub repository")
        owner_and_repository = policy.id.removeprefix(prefix)
        if owner_and_repository.count("/") != 1:
            raise GitVerificationError("GitHub repository identifier is invalid")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        base_url = f"https://api.github.com/repos/{owner_and_repository}"
        try:
            with httpx.Client(timeout=self._timeout_seconds, headers=headers) as client:
                commit_response = client.get(f"{base_url}/commits/{code_sha}")
                if commit_response.status_code == 404:
                    raise GitVerificationError("implementation commit does not exist")
                commit_response.raise_for_status()
                comparison = client.get(
                    f"{base_url}/compare/{code_sha}...{quote(branch, safe='')}"
                )
                if comparison.status_code == 404:
                    raise GitVerificationError(
                        "branch or implementation commit does not exist"
                    )
                comparison.raise_for_status()
                payload = comparison.json()
        except GitVerificationError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise GitVerificationError("GitHub reachability check failed") from error
        merge_base = payload.get("merge_base_commit", {}).get("sha")
        if (
            payload.get("status") not in {"ahead", "identical"}
            or merge_base != code_sha
        ):
            raise GitVerificationError(
                "implementation commit is not reachable from the allowed branch"
            )


class FakeGitVerifier:
    def __init__(self) -> None:
        self.reachable: set[tuple[str, str, str]] = set()
        self.calls: list[tuple[str, str, str]] = []

    def allow(self, repository: str, branch: str, code_sha: str) -> None:
        self.reachable.add((repository, branch, code_sha))

    def verify_reachable(
        self, policy: RepositoryPolicy, branch: str, code_sha: str
    ) -> None:
        identity = (policy.id, branch, code_sha)
        self.calls.append(identity)
        if identity not in self.reachable:
            raise GitVerificationError(
                "implementation commit is not reachable from the allowed branch"
            )
