from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode


class ArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_key: str
    sha256: str
    size_bytes: int


class FilesystemArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, object_key: str, content: bytes) -> StoredObject:
        destination = self._resolve(object_key)
        digest = hashlib.sha256(content).hexdigest()
        if destination.exists():
            existing = destination.read_bytes()
            if not hmac.compare_digest(hashlib.sha256(existing).hexdigest(), digest):
                raise ArtifactError(
                    "immutable artifact key already contains other data"
                )
            return StoredObject(object_key, digest, len(existing))
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".artifact-", dir=destination.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return StoredObject(object_key, digest, len(content))

    def read_bytes(self, object_key: str) -> bytes:
        path = self._resolve(object_key)
        if not path.is_file():
            raise ArtifactError("artifact object does not exist")
        return path.read_bytes()

    def delete(self, object_key: str) -> bool:
        """Remove one object, reporting whether it was still there."""

        path = self._resolve(object_key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def describe(self, object_key: str) -> StoredObject:
        content = self.read_bytes(object_key)
        return StoredObject(
            object_key, hashlib.sha256(content).hexdigest(), len(content)
        )

    def _resolve(self, object_key: str) -> Path:
        candidate = PurePosixPath(object_key)
        if (
            not object_key
            or candidate.is_absolute()
            or ".." in candidate.parts
            or "\\" in object_key
        ):
            raise ArtifactError("artifact key must be a safe relative POSIX path")
        resolved = self._root.joinpath(*candidate.parts).resolve()
        if not resolved.is_relative_to(self._root):
            raise ArtifactError("artifact key escapes the configured root")
        return resolved


class ArtifactLinkSigner:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        if len(secret) < 16:
            raise ValueError("artifact signing key must contain at least 16 characters")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def create_url(self, job_id: str, artifact_id: str, now: int | None = None) -> str:
        expires = (now or int(time.time())) + self._ttl_seconds
        signature = self._signature(job_id, artifact_id, expires)
        query = urlencode({"expires": expires, "signature": signature})
        return f"/api/v1/test-jobs/{job_id}/artifacts/{artifact_id}?{query}"

    def verify(
        self,
        job_id: str,
        artifact_id: str,
        expires: int,
        signature: str,
        now: int | None = None,
    ) -> None:
        current_time = now or int(time.time())
        if expires < current_time or expires > current_time + self._ttl_seconds + 30:
            raise ArtifactError(
                "artifact link is expired or outside its validity window"
            )
        expected = self._signature(job_id, artifact_id, expires)
        if not hmac.compare_digest(signature, expected):
            raise ArtifactError("artifact link signature is invalid")

    def _signature(self, job_id: str, artifact_id: str, expires: int) -> str:
        message = f"{job_id}\n{artifact_id}\n{expires}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()
