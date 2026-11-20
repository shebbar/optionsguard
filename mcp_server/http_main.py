"""Streamable HTTP entrypoint for the optionguard MCP server -- an
alternative to the stdio entrypoint in server.py (`python -m
mcp_server.server`), for running this as a long-lived container that
Claude Desktop reaches over the network via a stdio-to-HTTP bridge like
`mcp-remote`, instead of spawning it as a local subprocess.

Tool definitions live entirely in server.py -- this file only adds a
transport and an auth check, mirroring the separation and reasoning in
~/apps/pwm/backend/src/mcp-server.ts.

Bearer-token auth via a single static shared secret (MCP_AUTH_TOKEN), checked
with a timing-safe comparison -- not OAuth. OAuth 2.1 is MCP's own
recommended mechanism, but it's real infrastructure (authorization server,
client registration, per-caller revocation) built for many-user servers.
This has exactly one known caller (your own Claude Desktop), so a static
secret gives the same practical protection without standing up machinery
for a problem that doesn't exist here.
"""
from __future__ import annotations

import hmac
import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server.server import mcp

MCP_HTTP_PORT = int(os.environ.get("MCP_HTTP_PORT", "4101"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


class BearerAuthMiddleware:
    """Raw ASGI middleware, not Starlette's BaseHTTPMiddleware -- the
    wrapped app is a streaming (SSE-capable) endpoint, and
    BaseHTTPMiddleware buffers the entire response before forwarding it,
    which breaks streaming responses."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode()
        scheme, _, presented = auth_header.partition(" ")
        if scheme != "Bearer" or not presented or not hmac.compare_digest(presented, self.token):
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def create_http_app() -> BearerAuthMiddleware:
    if not MCP_AUTH_TOKEN:
        raise RuntimeError(
            "MCP_AUTH_TOKEN must be set to run the HTTP MCP server -- refusing to start "
            "unauthenticated. Generate one with `openssl rand -hex 32` and set it in .env."
        )
    # Binding 0.0.0.0 so other containers on the shared Docker network (the
    # optionguard core service, in practice) can reach it -- stays behind
    # the bearer-token check above either way, and isn't exposed beyond
    # 127.0.0.1 on the host side (see docker-compose.yml's port publish).
    inner_app = mcp.streamable_http_app(stateless_http=True, host="0.0.0.0")
    return BearerAuthMiddleware(inner_app, MCP_AUTH_TOKEN)


def main() -> None:
    import uvicorn

    uvicorn.run(create_http_app(), host="0.0.0.0", port=MCP_HTTP_PORT)


if __name__ == "__main__":
    main()
