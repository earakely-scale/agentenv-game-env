"""Which player slot a request plays. `PlayerPaths` serves `/players/<player_id>/<rest>` as `/<rest>` played by that
player slot, and answers its env card there; `current_player()` reads the id back in a tool (tools run in the MCP
session's own task, so from the request's scope) or an extension (from the request's context)."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar

from agentenv_protocol.types import WELL_KNOWN_PATH, error_body
from mcp.server.lowlevel.server import request_ctx
from starlette.responses import JSONResponse

PLAYERS = "/players"
PLAYER_PATH = re.compile(r"^/players/(?P<id>[A-Za-z0-9_.-]{1,64})(?P<rest>/.*)?$")
SCOPE_KEY = "agentenv_game_player"
PLAYER: ContextVar[str | None] = ContextVar(SCOPE_KEY, default=None)


class PlayerPaths:
    """ASGI middleware in front of a game env's app. `card(player_id)` is a player slot's env card, None for an id
    that plays no slot."""

    def __init__(self, app, card: Callable[[str], dict | None]):
        self.app, self.card = app, card

    async def __call__(self, scope, receive, send):
        m = PLAYER_PATH.match(scope.get("path") or "") if scope["type"] in ("http", "websocket") else None
        if m is None:
            return await self.app(scope, receive, send)
        player_id, rest = m["id"], m["rest"] or "/"
        if scope["type"] == "http" and rest == WELL_KNOWN_PATH:
            card = self.card(player_id)
            response = (JSONResponse(card) if card is not None else JSONResponse(
                error_body("unknown_player", f"no player slot {player_id!r} is played here"), status_code=404))
            return await response(scope, receive, send)
        scope = {**scope, SCOPE_KEY: player_id, "path": rest, "raw_path": rest.encode()}
        token = PLAYER.set(player_id)
        try:
            return await self.app(scope, receive, send)
        finally:
            PLAYER.reset(token)


def current_player() -> str | None:
    """The id of the player slot the current request plays; None at the env's own address."""
    try:
        request = request_ctx.get().request
    except LookupError:
        request = None
    return (getattr(request, "scope", None) or {}).get(SCOPE_KEY) or PLAYER.get()
