from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


class RequestSizeLimitMiddleware:
    def __init__(
        self, app: ASGIApp, max_bytes: int, artifact_max_bytes: int | None = None
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.artifact_max_bytes = artifact_max_bytes or max_bytes

    async def __call__(
        self, scope: dict, receive: Callable[[], Awaitable[dict]], send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        request_limit = (
            self.artifact_max_bytes
            if "/artifacts/" in scope.get("path", "")
            else self.max_bytes
        )
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > request_limit:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return

        consumed = 0

        async def limited_receive() -> dict:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > request_limit:
                    raise RequestTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestTooLargeError:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: dict, receive, send) -> None:
        request = Request(scope, receive=receive)
        response: Response = JSONResponse(
            {"detail": "request body exceeds configured limit"}, status_code=413
        )
        await response(request.scope, receive, send)


class RequestTooLargeError(Exception):
    pass
