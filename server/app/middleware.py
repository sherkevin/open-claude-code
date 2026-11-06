from .db import dump


class BodyLimitMiddleware:
    """Bound the aggregate body before Starlette can spool multipart files.

    Deliberately buffers at most maximum bytes, avoiding an unbounded temporary
    file before endpoint-level UploadFile checks. SSE/GET remain streaming.
    """

    def __init__(self, app, maximum: int):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            packet = await receive()
            if packet["type"] == "http.disconnect":
                return
            chunk = packet.get("body", b"")
            if len(body) + len(chunk) > self.maximum:
                payload = dump({"error": {"code": "UPLOAD_TOO_LARGE", "message": "请求内容超过大小限制"}}).encode()
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
                return await send({"type": "http.response.body", "body": payload})
            body.extend(chunk)
            if not packet.get("more_body", False):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
