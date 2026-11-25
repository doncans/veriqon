from collections import deque

from starlette.responses import JSONResponse


class MaxRequestBodyMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = self.max_bytes + 1
            if declared_length < 0 or declared_length > self.max_bytes:
                await JSONResponse(
                    status_code=413,
                    content={"detail": "Request body exceeds the configured size limit"},
                )(scope, receive, send)
                return

        buffered_messages = []
        total_bytes = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] == "http.request":
                total_bytes += len(message.get("body", b""))
                if total_bytes > self.max_bytes:
                    await JSONResponse(
                        status_code=413,
                        content={"detail": "Request body exceeds the configured size limit"},
                    )(scope, receive, send)
                    return
                buffered_messages.append(message)
                if not message.get("more_body", False):
                    break

        messages = deque(buffered_messages)

        async def replay_receive():
            if messages:
                return messages.popleft()
            return await receive()

        await self.app(scope, replay_receive, send)
