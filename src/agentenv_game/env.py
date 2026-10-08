"""`AgentEnvGameEnv`, the base of a game env: it serves the lobby (`urn:game:lobby/v1`) and the license
(`urn:game:license/v1`), gives each agent and human player slot its own env card, and tells each request which player
slot it plays. A game declares its settings as pydantic models, `GameSettings` for the lobby's and
`PlayerSlotSettings` for each player slot's, and marks its parts with decorators, as agentenv-protocol's data plane
marks `@reset_data`: `@create_game` (required), `@player_slot_limits`, `@check_player_slot`, `@player_slot_card`, and
for a licensed game `@license_needs` with `@install_license`."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from agentenv_protocol import AgentEnvEnvironment, extension
from agentenv_protocol.types import (
    MCP_PATH,
    MCP_TRANSPORT,
    RPC_PATH,
    EnvironmentCapabilities,
    EnvironmentCard,
    EnvironmentInterface,
    error_body,
)
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .license import LICENSE, LicenseItem, parts_for
from .lobby import LOBBY, Lobby, LobbyError, LobbyStatus, PlayerKind, PlayerSlot, PlayerSlotLimits, SlotRequest
from .routing import PLAYERS, PlayerPaths, current_player

log = logging.getLogger(__name__)
_HOOK = "_agentenv_game_hook"
HOOKS = {"player_slot_limits": (2, False), "check_player_slot": (2, False), "create_game": (1, True),
         "player_slot_card": (1, False), "license_needs": (0, False), "install_license": (1, False)}
"""Each decorator's method: its parameters after self, and whether it is a coroutine."""

ROUTE = f"{RPC_PATH}/ext/lobby"
DESCRIPTION = ("The next game's player slots: open a lobby with the game's settings, fill its player slots one player "
               "at a time (an agent, a person or the game's AI), and close it to create the game.")
NO_SETTINGS = {"type": "object", "maxProperties": 0}


def _embed(schema: dict, field: str, part: dict) -> dict:
    """`schema` with `part` as its `field` property, and `part`'s definitions moved to the root, where its refs
    point."""
    part = dict(part)
    defs = {**schema.get("$defs", {}), **part.pop("$defs", {})}
    return {**schema, "properties": {**schema.get("properties", {}), field: part}, **({"$defs": defs} if defs else {})}


def lobby_params(game_settings: type[BaseModel] | None, slot_settings: type[BaseModel] | None) -> dict:
    """The lobby's advertisement: each method's request, with the game's own settings' schemas in open's and fill's."""
    lobby_id = {"type": "object", "additionalProperties": False, "properties": {"lobby_id": {"type": "string"}}}
    opening = _embed({"type": "object", "additionalProperties": False}, "player_slot_limits",
                     PlayerSlotLimits.model_json_schema())
    return {"endpoint": ROUTE, "methods": {
        "open": {"method": "POST", "endpoint": f"{ROUTE}/open", "request": _embed(
            opening, "game_settings", game_settings.model_json_schema() if game_settings else NO_SETTINGS)},
        "get": {"method": "GET", "endpoint": ROUTE},
        "fill": {"method": "POST", "endpoint": f"{ROUTE}/fill", "request": _embed(
            SlotRequest.model_json_schema(), "game_settings",
            slot_settings.model_json_schema() if slot_settings else NO_SETTINGS)},
        "close": {"method": "POST", "endpoint": f"{ROUTE}/close", "request": lobby_id},
        "cancel": {"method": "POST", "endpoint": f"{ROUTE}/cancel", "request": lobby_id}}}


LICENSE_ROUTE = f"{RPC_PATH}/ext/license"
LICENSE_DESCRIPTION = ("What the game needs from its user to run (license files, keys, terms to accept) and still "
                       "lacks: get lists it, by name and kind, never the contents; add gives it.")
LICENSE_PARAMS = {"endpoint": LICENSE_ROUTE, "methods": {
    "get": {"method": "GET", "endpoint": LICENSE_ROUTE},
    "add": {"method": "POST", "endpoint": f"{LICENSE_ROUTE}/add", "request": {
        "type": "object", "additionalProperties": False,
        "properties": {"files": {"type": "object", "additionalProperties": {"type": "string"},
                                 "description": "file name → its contents in base64"},
                       "keys": {"type": "object", "additionalProperties": {"type": "string"}},
                       "accept": {"type": "array", "items": {"type": "string"}}}}}}}


def _mark(fn: Callable, hook: str) -> Callable:
    setattr(fn, _HOOK, hook)
    return fn


def create_game(fn: Callable) -> Callable:
    """The coroutine that creates the game from the closed lobby's player slots, `(lobby)`. Required. A game it can't
    create leaves the lobby failed."""
    return _mark(fn, "create_game")


def player_slot_limits(fn: Callable) -> Callable:
    """The method that sets a lobby's player slot limits as it opens, `(game_settings, requested)`: the game's
    settings (its GameSettings, checked) and the limits the opener asked for, returning the PlayerSlotLimits the
    lobby keeps (how many players, and of which kinds). Without it, the lobby keeps the request as given. Raise
    ValueError to refuse."""
    return _mark(fn, "player_slot_limits")


def check_player_slot(fn: Callable) -> Callable:
    """The method that checks a player slot before the lobby takes it, `(slot, lobby)`: the game's rules that a
    settings model can't say, those across player slots or by the kind of player. Raise ValueError to refuse
    (bad_settings), or a LobbyError with its code (bad_slot for a player_id the game doesn't have)."""
    return _mark(fn, "check_player_slot")


def player_slot_card(fn: Callable) -> Callable:
    """The method that gives an agent or human player slot its env card, `(slot)`: the env as that player sees it,
    served at the slot's environment_url. Without it, the card has one MCP interface, `/mcp`; a game that takes people
    gives them a page there, an `http` interface."""
    return _mark(fn, "player_slot_card")


def license_needs(fn: Callable) -> Callable:
    """The method that says what the game still lacks to run, `()`, as LicenseItems: files, keys and acceptances.
    An empty list means it is licensed; a lobby doesn't close until it is. A game with it has `@install_license`."""
    return _mark(fn, "license_needs")


def install_license(fn: Callable) -> Callable:
    """The method that puts the parts of a license where the game wants them, `(parts)`: a LicenseParts of files
    (bytes), keys (strings) and acceptances, each already checked against the item it fills. Raise ValueError to
    refuse one."""
    return _mark(fn, "install_license")


def _errors(e: ValidationError, prefix: str = "") -> str:
    return "; ".join(f"{'.'.join(map(str, [p for p in (prefix, *err['loc']) if p != ''])) or 'request'}: "
                     f"{err['msg']}" for err in e.errors())


def _dump(settings: BaseModel | dict) -> dict:
    return settings.model_dump(mode="json") if isinstance(settings, BaseModel) else settings


def _lobby_id(params: dict, method: str) -> str | None:
    if unknown := sorted(set(params) - {"lobby_id"}):
        raise LobbyError("bad_request", f"{method} takes a lobby_id, not {unknown}")
    if not isinstance(params.get("lobby_id", ""), str):
        raise LobbyError("bad_request", "lobby_id is a string")
    return params.get("lobby_id")


class AgentEnvGameEnv(AgentEnvEnvironment):
    """A game env. Its lobby takes the next game's players, one player slot at a time, and closing it creates the
    game; `player()` is the player slot the current request plays, by its `/players/<player_id>` address."""

    GameSettings: ClassVar[type[BaseModel] | None] = None
    """The lobby's game_settings, as a pydantic model: checked at open, every default filled in, and its schema
    published in the card's open request. None: the game takes no settings."""
    PlayerSlotSettings: ClassVar[type[BaseModel] | None] = None
    """Each player slot's game_settings, likewise: checked at fill, its schema in the card's fill request."""
    _lobby: Lobby | None = None
    _closing: asyncio.Lock | None = None
    _installed: list[str] | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Advertises each game's own lobby: its settings' schemas go in the card."""
        super().__init_subclass__(**kwargs)

        async def lobby_get(self) -> dict:
            return self.lobby.model_dump(mode="json", exclude_none=True)

        cls.lobby_get = extension(LOBBY, params=lobby_params(cls.GameSettings, cls.PlayerSlotSettings),
                                  description=DESCRIPTION, method="GET", path=ROUTE)(lobby_get)

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
        if ("license_needs" in found) != ("install_license" in found):
            raise TypeError(f"{type(self).__name__} marks only one of @license_needs and @install_license: a game "
                            "that needs a license installs it")
        for hook, (attr, fn) in found.items():
            count, coroutine = HOOKS[hook]
            given = [p for p in inspect.signature(fn).parameters.values()
                     if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            if len(given) != count or inspect.iscoroutinefunction(fn) != coroutine:
                raise TypeError(f"@{hook} {attr} must be {'an async' if coroutine else 'a plain'} method taking "
                                f"{count} argument{'s' if count > 1 else ''} after self")
        self.__dict__["_found_hooks"] = {hook: fn for hook, (_, fn) in found.items()}
        return self.__dict__["_found_hooks"]

    def _settings(self, model: type[BaseModel] | None, given: Any) -> BaseModel | dict:
        """`given` checked against `model`: the model, or {} for a game that takes no settings here."""
        if not isinstance(given, dict):
            raise LobbyError("bad_request", "game_settings is an object")
        if model is None:
            if given:
                raise LobbyError("bad_settings", f"{type(self).__name__} takes no game_settings here, not "
                                                 f"{sorted(given)}")
            return {}
        try:
            return model.model_validate(given)
        except ValidationError as e:
            raise LobbyError("bad_settings", _errors(e, "game_settings")) from e

    @property
    def lobby(self) -> Lobby:
        """The current lobby: the one opened last, else one not opened yet, with the game's default settings."""
        if self._lobby is None:
            try:
                defaults = _dump(self._settings(self.GameSettings, {}))
            except LobbyError:
                defaults = {}
            self._lobby = Lobby(game_settings=defaults)
        return self._lobby

    def new_lobby(self, game_settings: dict | None = None,
                  player_slot_limits: PlayerSlotLimits | None = None) -> Lobby:
        """Open a new lobby, dropping the last one and its game: `game_settings` checked against the game's
        GameSettings, and the player slot limits its `@player_slot_limits` sets."""
        settings = self._settings(self.GameSettings, {} if game_settings is None else game_settings)
        limits = player_slot_limits or PlayerSlotLimits()
        if hook := self._hooks().get("player_slot_limits"):
            try:
                limits = hook(settings, limits)
                limits = limits if isinstance(limits, PlayerSlotLimits) else PlayerSlotLimits(**limits)
            except ValueError as e:
                raise LobbyError("bad_settings", str(e)) from e
        self._lobby = Lobby(lobby_id=f"lb-{secrets.token_hex(4)}", status=LobbyStatus.OPEN,
                            game_settings=_dump(settings), player_slot_limits=limits)
        return self._lobby

    def fill_slot(self, request: SlotRequest) -> PlayerSlot:
        """Put one player in a player slot: the shared checks, the game's settings and rules, then its environment."""
        lobby = self.lobby
        if self._closing is not None and self._closing.locked():
            raise LobbyError("lobby_not_open", "the lobby is closing: its game is being created")
        lobby.check_open(request.lobby_id)
        settings = _dump(self._settings(self.PlayerSlotSettings, request.game_settings))
        slot, repeated = lobby.place(request.model_copy(update={"game_settings": settings}))
        if repeated:
            return slot
        if check := self._hooks().get("check_player_slot"):
            try:
                check(slot, lobby)
            except ValueError as e:
                raise LobbyError("bad_settings", str(e)) from e
        if slot.player_kind is not PlayerKind.AI:
            slot.environment_url = f"{PLAYERS}/{slot.player_id}"
        lobby.add(slot)
        return slot

    async def close_lobby(self, lobby_id: str | None = None) -> Lobby:
        """Close the lobby, creating the game from its player slots. Closing a closed lobby returns it again; a game
        that can't be created leaves the lobby failed."""
        self._closing = self._closing or asyncio.Lock()
        async with self._closing:
            lobby = self.lobby
            if lobby.status is LobbyStatus.CLOSED and lobby_id in (None, lobby.lobby_id):
                return lobby
            lobby.check_open(lobby_id)
            lobby.check_close()
            if missing := self.license_missing():
                raise LobbyError("not_licensed", f"the game lacks {', '.join(i.label() for i in missing)}: an "
                                                 "add_license step gives a game its license")
            try:
                await self._hooks()["create_game"](lobby)
            except BaseException:
                lobby.status = LobbyStatus.FAILED
                raise
            lobby.status = LobbyStatus.CLOSED
            return lobby

    def cancel_lobby(self, lobby_id: str | None = None) -> Lobby:
        """Abandon the open lobby before its game is created. Cancelling a cancelled lobby returns it again."""
        lobby = self.lobby
        if lobby.status is LobbyStatus.CANCELLED and lobby_id in (None, lobby.lobby_id):
            return lobby
        if self._closing is not None and self._closing.locked():
            raise LobbyError("lobby_not_open", "the lobby is closing: its game is being created")
        lobby.check_open(lobby_id)
        lobby.status = LobbyStatus.CANCELLED
        return lobby

    def slot_card(self, player_id: str) -> dict | None:
        """The env card of the agent or human player slot `player_id`, the env as that player sees it; None for an
        id that plays no such slot."""
        slot = self.lobby.slot(player_id)
        if slot is None or slot.player_kind is PlayerKind.AI:
            return None
        if hook := self._hooks().get("player_slot_card"):
            card = hook(slot)
            card = card if isinstance(card, EnvironmentCard) else EnvironmentCard(**card)
        else:
            card = EnvironmentCard(name=f"{self._build_card().name}/{slot.player_id}",
                                   additionalInterfaces=[EnvironmentInterface(url=MCP_PATH, transport=MCP_TRANSPORT)],
                                   capabilities=EnvironmentCapabilities(operations=[]))
        return card.model_dump(mode="json", exclude_none=True)

    def license_missing(self) -> list[LicenseItem]:
        """What the game still lacks to run (its `@license_needs`); nothing for a game that needs no license."""
        hook = self._hooks().get("license_needs")
        return [i if isinstance(i, LicenseItem) else LicenseItem(**i) for i in hook()] if hook else []

    def add_license(self, files: dict | None = None, keys: dict | None = None, accept: list | None = None) -> dict:
        """Give the game parts of its license: files as base64, keys as strings, acceptances by name. Each is checked
        against the item it fills, then the game's `@install_license` puts them in place."""
        parts = parts_for(self.license_missing(), files or {}, keys or {}, accept or [])
        if parts.files or parts.keys or parts.accepted:
            try:
                self._hooks()["install_license"](parts)
            except (ValueError, ValidationError) as e:
                raise LobbyError("bad_license", str(e)) from e
            self._installed = sorted({*(self._installed or ()), *parts.files, *parts.keys, *parts.accepted})
        return self.license_status()

    def license_status(self) -> dict:
        """What the game lacks and what it was given, by name: never a part's contents."""
        return {"missing": [i.model_dump(mode="json", exclude_none=True) for i in self.license_missing()],
                "installed": list(self._installed or ())}

    def player(self) -> PlayerSlot | None:
        """The player slot the current request plays; None at the env's own address. An id that plays no agent or
        human player slot is refused."""
        player_id = current_player()
        if player_id is None:
            return None
        slot = self.lobby.slot(player_id)
        if slot is None or slot.player_kind is PlayerKind.AI:
            ids = [s.player_id for s in self.lobby.player_slots if s.player_kind is not PlayerKind.AI]
            raise LobbyError("unknown_player", f"no player slot {player_id!r} is played here; this game's are "
                                               f"{', '.join(ids) or 'none yet'}")
        return slot

    # ---- urn:game:lobby/v1 ----
    # The SDK serves one handler per extension: `get` is it, and advertises every method (__init_subclass__ gives
    # each game its own advertisement); create_app adds the routes of open, fill, close and cancel, which answer a
    # refusal with a 400 and its code.

    @extension(LOBBY, params=lobby_params(None, None), description=DESCRIPTION, method="GET", path=ROUTE)
    async def lobby_get(self) -> dict:
        return self.lobby.model_dump(mode="json", exclude_none=True)

    async def _open(self, params: dict) -> dict:
        if unknown := sorted(set(params) - {"game_settings", "player_slot_limits"}):
            raise LobbyError("bad_request", f"open takes game_settings and player_slot_limits, not {unknown}")
        try:
            limits = (PlayerSlotLimits(**params["player_slot_limits"])
                      if params.get("player_slot_limits") is not None else None)
        except TypeError as e:
            raise LobbyError("bad_request", f"player_slot_limits: {e}") from e
        except ValidationError as e:
            raise LobbyError("bad_settings", _errors(e, "player_slot_limits")) from e
        lobby = self.new_lobby(params.get("game_settings"), limits)
        return lobby.model_dump(mode="json", exclude_none=True)

    async def _fill(self, params: dict) -> dict:
        try:
            request = SlotRequest(**params)
        except ValidationError as e:
            player = any(err["loc"][:1] in (("player_kind",), ("player_name",)) for err in e.errors())
            raise LobbyError("bad_player" if player else "bad_request", _errors(e)) from e
        return self.fill_slot(request).model_dump(mode="json", exclude_none=True)

    async def _close(self, params: dict) -> dict:
        lobby = await self.close_lobby(_lobby_id(params, "close"))
        return lobby.model_dump(mode="json", exclude_none=True)

    async def _cancel(self, params: dict) -> dict:
        return self.cancel_lobby(_lobby_id(params, "cancel")).model_dump(mode="json", exclude_none=True)

    # ---- urn:game:license/v1: get, served by the SDK, and add, a route of create_app's ----

    @extension(LICENSE, params=LICENSE_PARAMS, description=LICENSE_DESCRIPTION, method="GET", path=LICENSE_ROUTE)
    async def license_get(self) -> dict:
        return self.license_status()

    async def _add_license(self, params: dict) -> dict:
        if unknown := sorted(set(params) - {"files", "keys", "accept"}):
            raise LobbyError("bad_request", f"add takes files, keys and accept, not {unknown}")
        files, keys, accept = params.get("files") or {}, params.get("keys") or {}, params.get("accept") or []
        if not (isinstance(files, dict) and isinstance(keys, dict) and isinstance(accept, list)):
            raise LobbyError("bad_request", "files and keys are objects of name → value, accept a list of names")
        return self.add_license(files, keys, accept)

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
        for method, call in (("open", self._open), ("fill", self._fill), ("close", self._close),
                             ("cancel", self._cancel)):
            app.custom_route(f"{ROUTE}/{method}", methods=["POST"])(self._route(call))
        app.custom_route(f"{LICENSE_ROUTE}/add", methods=["POST"])(self._route(self._add_license))
        routes = app.streamable_http_app
        app.streamable_http_app = lambda: PlayerPaths(routes(), card=self.slot_card)
        return app
