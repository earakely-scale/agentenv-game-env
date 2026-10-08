"""Who a request comes from. `PlayerPaths` serves `/players/<player_id>/<rest>` as `/<rest>` played by that player slot,
and `/spectators/<rest>` as `/<rest>` watched by a spectator, and answers each one's env card there;
`current_player()` and `current_spectator()` read it back in a tool (tools run in the MCP session's own task, so from
the request's scope) or an extension (from the request's context)."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar

from agentenv_protocol.types import WELL_KNOWN_PATH, error_body
from mcp.server.lowlevel.server import request_ctx
from starlette.responses import JSONResponse

PLAYERS = "/players"
SPECTATORS = "/spectators"
PLAYER_PATH = re.compile(r"^/players/(?P<id>[A-Za-z0-9_.-]{1,64})(?P<rest>/.*)?$")
SPECTATOR_PATH = re.compile(r"^/spectators(?P<rest>/.*)?$")
SCOPE_KEY = "agentenv_game_player"
SPECTATOR_KEY = "agentenv_game_spectator"
PLAYER: ContextVar[str | None] = ContextVar(SCOPE_KEY, default=None)
SPECTATOR: ContextVar[bool] = ContextVar(SPECTATOR_KEY, default=False)


class PlayerPaths:
    """ASGI middleware in front of a game env's app. `card(player_id)` is a player slot's env card, None for an id
    that plays no slot; `spectators()` is the spectator view's, None for a game without one."""

    def __init__(self, app, card: Callable[[str], dict | None], spectators: Callable[[], dict | None]):
        self.app, self.card, self.spectators = app, card, spectators

    async def __call__(self, scope, receive, send):
        path = (scope.get("path") or "") if scope["type"] in ("http", "websocket") else ""
        if (m := PLAYER_PATH.match(path)) is not None:
            rest = m["rest"] or "/"
            if scope["type"] == "http" and rest == WELL_KNOWN_PATH:
                return await self._card(self.card(m["id"]), "unknown_player",
                                        f"no player slot {m['id']!r} is played here", scope, receive, send)
            return await self._as(PLAYER, SCOPE_KEY, m["id"], rest, scope, receive, send)
        if (m := SPECTATOR_PATH.match(path)) is not None:
            rest = m["rest"] or "/"
            if scope["type"] == "http" and rest == WELL_KNOWN_PATH:
                return await self._card(self.spectators(), "no_spectator_view",
                                        "this game has no spectator view, or no match yet", scope, receive, send)
            return await self._as(SPECTATOR, SPECTATOR_KEY, True, rest, scope, receive, send)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _card(card: dict | None, code: str, message: str, scope, receive, send):
        response = JSONResponse(card) if card is not None else JSONResponse(error_body(code, message), status_code=404)
        return await response(scope, receive, send)

    async def _as(self, var: ContextVar, key: str, value, rest: str, scope, receive, send):
        token = var.set(value)
        try:
            return await self.app({**scope, key: value, "path": rest, "raw_path": rest.encode()}, receive, send)
        finally:
            var.reset(token)


def _scope() -> dict:
    try:
        return getattr(request_ctx.get().request, "scope", None) or {}
    except LookupError:
        return {}


def current_player() -> str | None:
    """The id of the player slot the current request plays; None at the env's own address."""
    return _scope().get(SCOPE_KEY) or PLAYER.get()


def current_spectator() -> bool:
    """Whether the current request is a spectator's, under `/spectators`."""
    return bool(_scope().get(SPECTATOR_KEY) or SPECTATOR.get())
