"""Which player a request plays. `PlayerPaths` serves `/players/<name>/<rest>` as `/<rest>` played by `name`, or, for
an env that routes by header, reads the name from that header at any path; `current_player()` reads it back in a tool
(tools run in the MCP session's own task, so from the request's scope) or an extension (from the request's context)."""

from __future__ import annotations

import re
from contextvars import ContextVar

from mcp.server.lowlevel.server import request_ctx

PLAYERS = "/players"
PLAYER_PATH = re.compile(r"^/players/(?P<name>[A-Za-z0-9_.-]{1,64})(?P<rest>/.*)?$")
SCOPE_KEY = "agentenv_game_player"
PLAYER: ContextVar[str | None] = ContextVar(SCOPE_KEY, default=None)


class PlayerPaths:
    """ASGI middleware in front of a game env's app."""

    def __init__(self, app, header: str | None = None):
        self.app, self.header = app, header.lower().encode() if header else None

    async def __call__(self, scope, receive, send):
        name = rest = None
        if scope["type"] in ("http", "websocket"):
            if m := PLAYER_PATH.match(scope.get("path") or ""):
                name, rest = m["name"], m["rest"] or "/"
            elif self.header:
                name = next((v.decode(errors="replace") for k, v in scope.get("headers") or () if k == self.header),
                            None)
        if name is None:
            return await self.app(scope, receive, send)
        scope = {**scope, SCOPE_KEY: name}
        if rest is not None:
            scope.update(path=rest, raw_path=rest.encode())
        token = PLAYER.set(name)
        try:
            return await self.app(scope, receive, send)
        finally:
            PLAYER.reset(token)


def current_player() -> str | None:
    """The name the current request plays as; None at the env's own address."""
    try:
        request = request_ctx.get().request
    except LookupError:
        request = None
    return (getattr(request, "scope", None) or {}).get(SCOPE_KEY) or PLAYER.get()
