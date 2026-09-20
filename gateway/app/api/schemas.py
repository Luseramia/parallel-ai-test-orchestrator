from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.test_jobs import ArtifactType, EventPhase, JobStatus

GIT_SHA_PATTERN = r"^[0-9a-f]{40}$"


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=200)
    repository: str = Field(min_length=1, max_length=300)
    branch: str = Field(min_length=1, max_length=255)
    base_sha: str = Field(pattern=GIT_SHA_PATTERN)
    plan: dict[str, Any]
    callback_url: str = Field(min_length=1, max_length=2048)


class PrepareResponse(BaseModel):
    job_id: str
    status: JobStatus
    status_url: str


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=300)
    branch: str = Field(min_length=1, max_length=255)
    code_sha: str = Field(pattern=GIT_SHA_PATTERN)
    push_event_id: str = Field(min_length=1, max_length=255)


class VerifyResponse(BaseModel):
    job_id: str
    status: JobStatus
    attempt: int = Field(ge=1)
    duplicate: bool


class RunnerArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ArtifactType
    object_key: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str | None = Field(default=None, max_length=200)


class RunnerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=r"^1\.0$")
    event_id: str = Field(pattern=r"^evt_[A-Za-z0-9_-]{8,100}$")
    attempt: int = Field(ge=1)
    phase: EventPhase
    status: JobStatus
    code_sha: str | None = Field(default=None, pattern=GIT_SHA_PATTERN)
    artifacts: list[RunnerArtifact] = Field(default_factory=list, max_length=100)
    summary: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class EventResponse(BaseModel):
    job_id: str
    status: JobStatus
    version: int
    duplicate: bool


class CancelResponse(BaseModel):
    job_id: str
    status: JobStatus
    duplicate: bool
