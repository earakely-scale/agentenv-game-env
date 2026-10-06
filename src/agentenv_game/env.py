"""`AgentEnvGameEnv`, the base of a game env: it serves the lobby (`urn:game:lobby/v1`) and tells each request which
player slot it plays. A game marks its parts of the lobby with decorators, as agentenv-protocol's data plane marks
`@reset_data`: `@create_game` (required), `@open_lobby`, `@check_slot`, `@connect_slot`, `@play_link`."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agentenv_protocol import AgentEnvEnvironment, extension
from agentenv_protocol.types import MCP_PATH, RPC_PATH, error_body
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .lobby import (
    LOBBY,
    Connect,
    Lobby,
    LobbyError,
    LobbyState,
    Occupant,
    OccupantKind,
    PlayerSlot,
    PlayerSlotSettings,
    SlotRequest,
)
from .routing import PLAYERS, PlayerPaths, current_player

log = logging.getLogger(__name__)
_HOOK = "_agentenv_game_hook"
HOOKS = {"open_lobby": (2, False), "check_slot": (2, False), "create_game": (1, True), "connect_slot": (1, False),
         "play_link": (1, False)}
"""Each decorator's method: its parameters after self, and whether it is a coroutine."""

ROUTE = f"{RPC_PATH}/ext/lobby"
DESCRIPTION = ("The next game's player slots: open a lobby with the game's settings, fill its slots one occupant at a "
               "time (an agent, a person or the game's AI), and close it to create the game.")
PARAMS = {"endpoint": ROUTE, "methods": {
    "open": {"method": "POST", "endpoint": f"{ROUTE}/open", "request": {
        "type": "object", "additionalProperties": False,
        "properties": {"additional_settings": {"type": "object"},
                       "player_slot_settings": PlayerSlotSettings.model_json_schema()}}},
    "get": {"method": "GET", "endpoint": ROUTE},
    "fill": {"method": "POST", "endpoint": f"{ROUTE}/fill", "request": SlotRequest.model_json_schema()},
    "close": {"method": "POST", "endpoint": f"{ROUTE}/close"}}}


def _mark(fn: Callable, hook: str) -> Callable:
    setattr(fn, _HOOK, hook)
    return fn


def open_lobby(fn: Callable) -> Callable:
    """The method that takes a lobby's settings as it opens, `(additional_settings, player_slot_settings)`, and returns
    them as the lobby keeps them: checked, with defaults filled in and slot limits set. Raise ValueError to refuse."""
    return _mark(fn, "open_lobby")


def check_slot(fn: Callable) -> Callable:
    """The method that checks a slot before the lobby takes it, `(slot, lobby)`: the game's own rules for its
    occupant and settings. Raise ValueError to refuse."""
    return _mark(fn, "check_slot")


def create_game(fn: Callable) -> Callable:
    """The coroutine that creates the game from the closed lobby's slots, `(lobby)`, and returns what callers learn
    of it (the start locations, say). Required."""
    return _mark(fn, "create_game")


def connect_slot(fn: Callable) -> Callable:
    """The method that says how an agent reaches its slot, `(slot)`, returning a Connect. Without it, the slot's
    address is `/players/<name>/mcp`, or the env's own with `player_header` set to the name."""
    return _mark(fn, "connect_slot")


def play_link(fn: Callable) -> Callable:
    """The method that gives a person their link into the game, `(slot)`, for a human slot. Without it, the env takes
    no human players."""
    return _mark(fn, "play_link")


class AgentEnvGameEnv(AgentEnvEnvironment):
    """A game env. Its lobby takes the next game's players, one slot at a time, and closing it creates the game;
    `player()` is the slot the current request plays, by its `/players/<name>` address or, with `player_header`, by
    that header."""

    player_header: str | None = None
    _lobby: Lobby | None = None
    _game: dict | None = None
    _closing: asyncio.Lock | None = None

    def _hooks(self) -> dict[str, Callable]:
        """The game's marked methods, checked once: one per decorator, @create_game present, signatures as each
        decorator says."""
        if "_found_hooks" in self.__dict__:
            return self.__dict__["_found_hooks"]
        found: dict[str, tuple[str, Callable]] = {}
        for attr in dir(type(self)):
            hook = getattr(getattr(type(self), attr, None), _HOOK, None)
            if hook is None:
                continue
            if hook in found:
                raise TypeError(f"Multiple methods marked @{hook}: {found[hook][0]} and {attr}")
            found[hook] = (attr, getattr(self, attr))
        if "create_game" not in found:
            raise TypeError(f"{type(self).__name__} marks no @create_game method: a game env creates its game when "
                            "the lobby closes")
        for hook, (attr, fn) in found.items():
            count, coroutine = HOOKS[hook]
            given = [p for p in inspect.signature(fn).parameters.values()
                     if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            if len(given) != count or inspect.iscoroutinefunction(fn) != coroutine:
                raise TypeError(f"@{hook} {attr} must be {'an async' if coroutine else 'a plain'} method taking "
                                f"{count} argument{'s' if count > 1 else ''} after self")
        self.__dict__["_found_hooks"] = {hook: fn for hook, (_, fn) in found.items()}
        return self.__dict__["_found_hooks"]

    @property
    def lobby(self) -> Lobby:
        """The current lobby: the one opened last, else an open one with the game's default settings."""
        if self._lobby is None:
            self._lobby = self._opened({}, None)
        return self._lobby

    def _opened(self, additional_settings: dict, player_slot_settings: PlayerSlotSettings | None) -> Lobby:
        if hook := self._hooks().get("open_lobby"):
            try:
                additional_settings, player_slot_settings = hook(dict(additional_settings), player_slot_settings)
            except (ValueError, ValidationError) as e:
                raise LobbyError("bad_settings", str(e)) from e
        return Lobby(additional_settings=additional_settings, player_slot_settings=player_slot_settings)

    def new_lobby(self, additional_settings: dict | None = None,
                  player_slot_settings: PlayerSlotSettings | None = None) -> Lobby:
        """Open a new lobby, dropping the last one and its game."""
        self._lobby, self._game = self._opened(additional_settings or {}, player_slot_settings), None
        return self._lobby

    def fill_slot(self, request: SlotRequest) -> PlayerSlot:
        """Put one occupant in a slot: the shared checks, then the game's, then its address or link."""
        lobby = self.lobby
        if self._closing is not None and self._closing.locked():
            raise LobbyError("lobby_closed", "the lobby is closing: the game is being created")
        slot, repeated = lobby.place(request)
        if repeated:
            return slot
        hooks = self._hooks()
        if slot.occupant.kind is OccupantKind.HUMAN and "play_link" not in hooks:
            raise LobbyError("bad_occupant", f"{type(self).__name__} takes no human players")
        if check := hooks.get("check_slot"):
            try:
                check(slot, lobby)
            except (ValueError, ValidationError) as e:
                raise LobbyError("bad_settings", str(e)) from e
        if slot.occupant.kind is OccupantKind.AGENT:
            slot.connect = self._connect(slot)
        elif slot.occupant.kind is OccupantKind.HUMAN:
            slot.play = hooks["play_link"](slot)
        lobby.add(slot)
        return slot

    def _connect(self, slot: PlayerSlot) -> Connect:
        if hook := self._hooks().get("connect_slot"):
            connect = hook(slot)
            return connect if isinstance(connect, Connect) else Connect(**connect)
        name = slot.occupant.name
        if self.player_header:
            return Connect(path=MCP_PATH, headers={self.player_header: name})
        return Connect(path=f"{PLAYERS}/{name}{MCP_PATH}")

    async def close_lobby(self) -> dict:
        """Close the lobby and create the game from its slots. Closing a closed lobby returns its game again."""
        self._closing = self._closing or asyncio.Lock()
        async with self._closing:
            lobby = self.lobby
            if lobby.state is LobbyState.OPEN:
                lobby.check_close()
                self._game = await self._hooks()["create_game"](lobby) or {}
                lobby.state = LobbyState.CLOSED
            return {**lobby.model_dump(mode="json", exclude_none=True), "game": self._game}

    def player(self) -> PlayerSlot | None:
        """The agent slot the current request plays; None at the env's own address. A name without one is refused."""
        name = current_player()
        if name is None:
            return None
        slot = self.lobby.named(name)
        if slot is None or slot.occupant.kind is not OccupantKind.AGENT:
            players = [s.occupant.name for s in self.lobby.slots if s.occupant.kind is OccupantKind.AGENT]
            raise LobbyError("unknown_player", f"{name!r} plays no slot in this game; its players are "
                                               f"{', '.join(players) or 'none yet'}")
        return slot

    # ---- urn:game:lobby/v1 ----
    # The SDK serves one handler per extension: `get` is it, and advertises every method; create_app adds the routes
    # of open, fill and close, which answer a refusal with a 400 and its code.

    @extension(LOBBY, params=PARAMS, description=DESCRIPTION, method="GET", path=ROUTE)
    async def lobby_get(self) -> dict:
        return self.lobby.model_dump(mode="json", exclude_none=True)

    async def _open(self, params: dict) -> dict:
        if unknown := sorted(set(params) - {"additional_settings", "player_slot_settings"}):
            raise LobbyError("bad_request", f"open takes additional_settings and player_slot_settings, not {unknown}")
        try:
            limits = (PlayerSlotSettings(**params["player_slot_settings"])
                      if params.get("player_slot_settings") is not None else None)
        except (TypeError, ValidationError) as e:
            raise LobbyError("bad_settings", f"player_slot_settings: {e}") from e
        if not isinstance(params.get("additional_settings") or {}, dict):
            raise LobbyError("bad_request", "additional_settings is an object")
        return self.new_lobby(params.get("additional_settings"), limits).model_dump(mode="json", exclude_none=True)

    async def _fill(self, params: dict) -> dict:
        try:
            Occupant(**params.get("occupant") or {})
        except (TypeError, ValidationError) as e:
            raise LobbyError("bad_occupant", str(e)) from e
        try:
            request = SlotRequest(**params)
        except ValidationError as e:
            raise LobbyError("bad_request", str(e)) from e
        return self.fill_slot(request).model_dump(mode="json", exclude_none=True)

    async def _close(self, params: dict) -> dict:
        return await self.close_lobby()

    def _route(self, call: Callable[[dict], Awaitable[dict]]) -> Callable:
        async def handler(request: Request) -> JSONResponse:
            try:
                raw = await request.body()
                params = json.loads(raw) if raw else {}
                if not isinstance(params, dict):
                    raise LobbyError("bad_request", "the request body is a JSON object")
                return JSONResponse(await call(params))
            except json.JSONDecodeError as e:
                return JSONResponse(error_body("bad_request", f"the request body is not JSON: {e}"), status_code=400)
            except LobbyError as e:
                return JSONResponse(error_body(e.code, e.message), status_code=400)
            except Exception as e:
                log.exception("lobby %s failed", request.url.path)
                return JSONResponse(error_body("lobby_failed", f"{type(e).__name__}: {e}"), status_code=500)
        return handler

    def create_app(self) -> Any:
        self._hooks()
        app = super().create_app()
        for method, call in (("open", self._open), ("fill", self._fill), ("close", self._close)):
            app.custom_route(f"{ROUTE}/{method}", methods=["POST"])(self._route(call))
        routes, header = app.streamable_http_app, self.player_header
        app.streamable_http_app = lambda: PlayerPaths(routes(), header=header)
        return app
