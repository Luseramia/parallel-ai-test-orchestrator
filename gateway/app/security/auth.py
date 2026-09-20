from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authorization scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _require(authorization: str | None, *expected: str) -> None:
    presented = _bearer_token(authorization)
    # Every candidate is compared so that the work does not depend on which
    # workload token matched, or on how many are configured.
    matched = False
    for candidate in expected:
        if candidate and secrets.compare_digest(presented, candidate):
            matched = True
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_api_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    _require(authorization, request.app.state.settings.api_token)


def require_runner_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    _require(authorization, *request.app.state.settings.runner_tokens)
