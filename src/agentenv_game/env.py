"""`AgentEnvGameEnv`, the base of a game env: it serves the lobby (`urn:game:lobby/v1`), the match its close creates
(`urn:game:match/v1`) and the license (`urn:game:license/v1`), gives each agent and human player slot its own env
card, and tells each request which player slot it plays. A game declares its settings as pydantic models,
`GameSettings` for the lobby's and `PlayerSlotSettings` for each player slot's, and marks its parts with decorators,
as agentenv-protocol's data plane marks `@reset_data`: `@create_game` (required), `@player_slot_limits`,
`@check_player_slot`, `@player_slot_card` and `@player_teams` for the lobby; `@match_report`, `@begin_game`,
`@play_out`, `@spectator_card` and `@match_files` for the match; and for a licensed game `@license_needs` with
`@install_license`."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from agentenv_protocol import AgentEnvEnvironment
from agentenv_protocol.types import (
    MCP_PATH,
    MCP_TRANSPORT,
    RPC_PATH,
    EnvironmentCapabilities,
    EnvironmentCard,
    EnvironmentExtension,
    EnvironmentInterface,
    error_body,
)
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from .license import LICENSE, LicenseItem, parts_for
from .lobby import (
    LOBBY,
    GameError,
    Lobby,
    LobbyStatus,
    PlayerKind,
    PlayerSlot,
    PlayerSlotLimits,
    PlayerTeam,
    SlotRequest,
)
from .match import FINAL, MATCH, Match, MatchFile, MatchFiles, MatchReport, MatchStatus, PlayerState, PlayerStatus
from .routing import PLAYERS, SPECTATORS, PlayerPaths, current_player, current_spectator

log = logging.getLogger(__name__)
_HOOK = "_agentenv_game_hook"
HOOKS = {"player_slot_limits": (2, False), "check_player_slot": (2, False), "create_game": (1, True),
         "player_slot_card": (1, False), "player_teams": (1, False), "match_report": (0, False),
         "begin_game": (0, True), "play_out": (0, True), "spectator_card": (0, False), "match_files": (1, True),
         "license_needs": (0, False), "install_license": (1, False)}
"""Each decorator's method: its parameters after self, and whether it is a coroutine."""

LOBBY_ID = {"type": "object", "additionalProperties": False, "properties": {"lobby_id": {"type": "string"}}}

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
    opening = _embed({"type": "object", "additionalProperties": False}, "player_slot_limits",
                     PlayerSlotLimits.model_json_schema())
    return {"endpoint": ROUTE, "methods": {
        "open": {"method": "POST", "endpoint": f"{ROUTE}/open", "request": _embed(
            opening, "game_settings", game_settings.model_json_schema() if game_settings else NO_SETTINGS)},
        "get": {"method": "GET", "endpoint": ROUTE},
        "fill": {"method": "POST", "endpoint": f"{ROUTE}/fill", "request": _embed(
            SlotRequest.model_json_schema(), "game_settings",
            slot_settings.model_json_schema() if slot_settings else NO_SETTINGS)},
        "close": {"method": "POST", "endpoint": f"{ROUTE}/close", "request": LOBBY_ID},
        "cancel": {"method": "POST", "endpoint": f"{ROUTE}/cancel", "request": LOBBY_ID}}}


MATCH_ROUTE = f"{RPC_PATH}/ext/match"
MATCH_DESCRIPTION = ("The game the lobby's close created, from its start to its end: get how it stands (null until "
                     "the lobby closes), say a player is ready (it starts once every player is), finish it by playing "
                     "it out with no more player moves, cancel it where it stands, and, once it is over, list the "
                     "files the game keeps of it (each downloaded from its path).")
MATCH_PARAMS = {"endpoint": MATCH_ROUTE, "methods": {
    "get": {"method": "GET", "endpoint": MATCH_ROUTE},
    "player_ready": {"method": "POST", "endpoint": f"{MATCH_ROUTE}/player_ready", "request": {
        **LOBBY_ID, "required": ["player_id"],
        "properties": {**LOBBY_ID["properties"], "player_id": {"type": "string"}}}},
    "finish": {"method": "POST", "endpoint": f"{MATCH_ROUTE}/finish", "request": LOBBY_ID},
    "cancel": {"method": "POST", "endpoint": f"{MATCH_ROUTE}/cancel", "request": LOBBY_ID},
    "files": {"method": "POST", "endpoint": f"{MATCH_ROUTE}/files", "request": {
        **LOBBY_ID,
        "properties": {**LOBBY_ID["properties"], "kinds": {"type": "array", "items": {"type": "string"}}}}}}}

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
    (bad_settings), or a GameError with its code (bad_slot for a player_id the game doesn't have)."""
    return _mark(fn, "check_player_slot")


def player_slot_card(fn: Callable) -> Callable:
    """The method that gives an agent or human player slot its env card, `(slot)`: the env as that player sees it,
    served at the slot's environment_url. Without it, the card has one MCP interface, `/mcp`; a game that takes people
    gives them a page there, an `http` interface."""
    return _mark(fn, "player_slot_card")


def player_teams(fn: Callable) -> Callable:
    """The method that puts the lobby's player slots in teams, `(lobby)`, as PlayerTeams: allies on one team, every
    player slot on exactly one. Read as player slots fill, so fixed when the lobby closes. Without it, each player
    slot is a team of its own."""
    return _mark(fn, "player_teams")


def match_report(fn: Callable) -> Callable:
    """The method that says how the match is going, `()`, as a MatchReport: the game's own status (started, paused,
    finished or failed) and its words for it, its progress, the outcomes its rules have decided, and scores. Read on
    every read of the match and kept once the match is final, so it reads the game, never changes it. Without it,
    the match has only its lifecycle."""
    return _mark(fn, "match_report")


def begin_game(fn: Callable) -> Callable:
    """The coroutine that begins play, `()`, once: a realtime game's clock starts, a lockstep game's first step. A
    game with one waits for its players, and its match starts once every player is ready (`player_ready`), when the
    game calls `begin_match`, or when finish plays it out. A game without one starts as its lobby closes."""
    return _mark(fn, "begin_game")


def play_out(fn: Callable) -> Callable:
    """The coroutine that plays the match out once its agents have stopped, `()`: it takes no more moves from agent
    and human players, and returns when the game's rules or its limit have ended the match. finish calls it. Without
    it, finish ends the match cancelled: a game that can't advance without its players has nothing to play out."""
    return _mark(fn, "play_out")


def spectator_card(fn: Callable) -> Callable:
    """The method that gives the match's spectator view its env card, `()`: the env as an onlooker sees it, served at
    the match's spectator_url (`/spectators`), with the game's page as an `http` interface that draws only the game,
    full-frame, for a broadcast's overlay to frame. Requests under it are a spectator's (`spectating()`). Without it,
    the game has no spectator view."""
    return _mark(fn, "spectator_card")


def match_files(fn: Callable) -> Callable:
    """The coroutine that gives a finished match's files, `(kinds)`: the kinds asked for, or None for the game's
    default ones, as MatchFiles (or a list of MatchFile: a name, the game's kind, a content type, the file on disk).
    The SDK serves each at its path. It is called only once the match is over; a ValueError refuses the kinds."""
    return _mark(fn, "match_files")


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


def _strings(params: dict, method: str, *names: str, required: tuple[str, ...] = ()) -> dict:
    """`params` of a method that takes only the strings `names`, `required` among them."""
    if unknown := sorted(set(params) - set(names)):
        raise GameError("bad_request", f"{method} takes {', '.join(names)}, not {unknown}")
    if missing := [n for n in required if n not in params]:
        raise GameError("bad_request", f"{method} needs {', '.join(missing)}")
    if wrong := [n for n in names if n in params and not isinstance(params[n], str)]:
        raise GameError("bad_request", f"{', '.join(wrong)} is a string")
    return params


class AgentEnvGameEnv(AgentEnvEnvironment):
    """A game env. Its lobby takes the next game's players, one player slot at a time, and closing it creates the
    game and its match, which runs from its start gate to its end. `player()` is the player slot the current request
    plays, by its `/players/<player_id>` address, and `match` is how the match stands."""

    GameSettings: ClassVar[type[BaseModel] | None] = None
    """The lobby's game_settings, as a pydantic model: checked at open, every default filled in, and its schema
    published in the card's open request. None: the game takes no settings."""
    PlayerSlotSettings: ClassVar[type[BaseModel] | None] = None
    """Each player slot's game_settings, likewise: checked at fill, its schema in the card's fill request."""
    _lobby: Lobby | None = None
    _match: Match | None = None
    """The match's lifecycle, kept here; once final, the match as it ended."""
    _files: dict[str, MatchFile] | None = None
    """The files the last `files` listed, by name, served until the next lobby opens."""
    _closing: asyncio.Lock | None = None
    _beginning: asyncio.Lock | None = None
    _finishing: asyncio.Lock | None = None
    _installed: list[str] | None = None

    def _build_card(self) -> EnvironmentCard:
        """The game's card, declaring its lobby, match and license: each has several methods and the SDK serves one
        handler per extension, so create_app serves their routes."""
        card = super()._build_card()
        caps = card.capabilities or EnvironmentCapabilities()
        ours = [EnvironmentExtension(uri=LOBBY, description=DESCRIPTION,
                                     params=lobby_params(self.GameSettings, self.PlayerSlotSettings)),
                EnvironmentExtension(uri=MATCH, description=MATCH_DESCRIPTION, params=MATCH_PARAMS),
                EnvironmentExtension(uri=LICENSE, description=LICENSE_DESCRIPTION, params=LICENSE_PARAMS)]
        return card.model_copy(update={"capabilities": caps.model_copy(
            update={"extensions": [*(caps.extensions or []), *ours]})})

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
                                f"{count} argument{'' if count == 1 else 's'} after self")
        self.__dict__["_found_hooks"] = {hook: fn for hook, (_, fn) in found.items()}
        return self.__dict__["_found_hooks"]

    def _settings(self, model: type[BaseModel] | None, given: Any) -> BaseModel | dict:
        """`given` checked against `model`: the model, or {} for a game that takes no settings here."""
        if not isinstance(given, dict):
            raise GameError("bad_request", "game_settings is an object")
        if model is None:
            if given:
                raise GameError("bad_settings", f"{type(self).__name__} takes no game_settings here, not "
                                                f"{sorted(given)}")
            return {}
        try:
            return model.model_validate(given)
        except ValidationError as e:
            raise GameError("bad_settings", _errors(e, "game_settings")) from e

    # ---- the lobby ----

    @property
    def lobby(self) -> Lobby:
        """The current lobby: the one opened last, else one not opened yet, with the game's default settings."""
        if self._lobby is None:
            try:
                defaults = _dump(self._settings(self.GameSettings, {}))
            except GameError:
                defaults = {}
            self._lobby = Lobby(game_settings=defaults)
        return self._lobby

    def new_lobby(self, game_settings: dict | None = None,
                  player_slot_limits: PlayerSlotLimits | None = None) -> Lobby:
        """Open a new lobby, dropping the last one, its game and its match: `game_settings` checked against the game's
        GameSettings, and the player slot limits its `@player_slot_limits` sets."""
        settings = self._settings(self.GameSettings, {} if game_settings is None else game_settings)
        limits = player_slot_limits or PlayerSlotLimits()
        if hook := self._hooks().get("player_slot_limits"):
            try:
                limits = hook(settings, limits)
                limits = limits if isinstance(limits, PlayerSlotLimits) else PlayerSlotLimits(**limits)
            except ValueError as e:
                raise GameError("bad_settings", str(e)) from e
        self._lobby = Lobby(lobby_id=f"lb-{secrets.token_hex(4)}", status=LobbyStatus.OPEN,
                            game_settings=_dump(settings), player_slot_limits=limits)
        self._match = self._files = None
        return self._lobby

    def fill_slot(self, request: SlotRequest) -> PlayerSlot:
        """Put one player in a player slot: the shared checks, the game's settings and rules, then its environment and
        the teams."""
        lobby = self.lobby
        if self._closing is not None and self._closing.locked():
            raise GameError("lobby_not_open", "the lobby is closing: its game is being created")
        lobby.check_open(request.lobby_id)
        settings = _dump(self._settings(self.PlayerSlotSettings, request.game_settings))
        slot, repeated = lobby.place(request.model_copy(update={"game_settings": settings}))
        if repeated:
            return slot
        if check := self._hooks().get("check_player_slot"):
            try:
                check(slot, lobby)
            except ValueError as e:
                raise GameError("bad_settings", str(e)) from e
        if slot.player_kind is not PlayerKind.AI:
            slot.environment_url = f"{PLAYERS}/{slot.player_id}"
        lobby.add(slot)
        lobby.player_teams = self._teams(lobby)
        return slot

    def _teams(self, lobby: Lobby) -> list[PlayerTeam]:
        """The lobby's player slots in teams: the game's `@player_teams`, checked, else each a team of its own."""
        hook = self._hooks().get("player_teams")
        if hook is None:
            return [PlayerTeam(team_id=s.player_id, player_ids=[s.player_id]) for s in lobby.player_slots]
        teams = [t if isinstance(t, PlayerTeam) else PlayerTeam(**t) for t in hook(lobby)]
        placed = sorted(p for t in teams for p in t.player_ids)
        if placed != sorted(s.player_id for s in lobby.player_slots) or len({t.team_id for t in teams}) < len(teams):
            raise ValueError("@player_teams puts every player slot on exactly one team, each team with its own "
                             f"team_id, not {[t.model_dump() for t in teams]}")
        return teams

    async def close_lobby(self, lobby_id: str | None = None) -> Lobby:
        """Close the lobby, creating the game and its match from its player slots. The match starts at once unless the
        game's `@begin_game` has players to wait for. Closing a closed lobby returns it again; a game that can't be
        created, or started at once, leaves the lobby failed and no match."""
        self._closing = self._closing or asyncio.Lock()
        async with self._closing:
            lobby = self.lobby
            if lobby.status is LobbyStatus.CLOSED and lobby_id in (None, lobby.lobby_id):
                return lobby
            lobby.check_open(lobby_id)
            lobby.check_close()
            if missing := self.license_missing():
                raise GameError("not_licensed", f"the game lacks {', '.join(i.label() for i in missing)}: an "
                                                "add_license step gives a game its license")
            try:
                await self._hooks()["create_game"](lobby)
                self._match = Match(
                    lobby_id=lobby.lobby_id, spectator_url=SPECTATORS if "spectator_card" in self._hooks() else None,
                    player_states={s.player_id: PlayerState(
                        status=PlayerStatus.READY if s.player_kind is PlayerKind.AI else PlayerStatus.NOT_READY)
                        for s in lobby.player_slots})
                if "begin_game" not in self._hooks() or all(s.player_kind is PlayerKind.AI
                                                            for s in lobby.player_slots):
                    await self._begin()
            except BaseException:
                lobby.status, self._match = LobbyStatus.FAILED, None
                raise
            lobby.status = LobbyStatus.CLOSED
            return lobby

    def cancel_lobby(self, lobby_id: str | None = None) -> Lobby:
        """Abandon the open lobby before its game is created. Cancelling a cancelled lobby returns it again."""
        lobby = self.lobby
        if lobby.status is LobbyStatus.CANCELLED and lobby_id in (None, lobby.lobby_id):
            return lobby
        if self._closing is not None and self._closing.locked():
            raise GameError("lobby_not_open", "the lobby is closing: its game is being created")
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
            card = EnvironmentCard(name=f"{super()._build_card().name}/{slot.player_id}",
                                   additionalInterfaces=[EnvironmentInterface(url=MCP_PATH, transport=MCP_TRANSPORT)],
                                   capabilities=EnvironmentCapabilities(operations=[]))
        return card.model_dump(mode="json", exclude_none=True)

    def player(self) -> PlayerSlot | None:
        """The player slot the current request plays; None at the env's own address. An id that plays no agent or
        human player slot is refused."""
        player_id = current_player()
        if player_id is None:
            return None
        slot = self.lobby.slot(player_id)
        if slot is None or slot.player_kind is PlayerKind.AI:
            ids = [s.player_id for s in self.lobby.player_slots if s.player_kind is not PlayerKind.AI]
            raise GameError("unknown_player", f"no player slot {player_id!r} is played here; this game's are "
                                              f"{', '.join(ids) or 'none yet'}")
        return slot

    # ---- the match ----

    @property
    def match(self) -> Match | None:
        """How the match stands: None until the lobby closes, then its lifecycle with the game's `@match_report`, and
        once final, as it ended."""
        match = self._match
        if match is None or match.status in FINAL:
            return match
        report = self._report()
        begun = match.status is not MatchStatus.NOT_STARTED
        status = MatchStatus(report.status) if begun or report.status == "failed" else match.status
        current = self._merged(match, report, status)
        if status in FINAL:
            self._match = current
        return current

    def _report(self) -> MatchReport:
        hook = self._hooks().get("match_report")
        if hook is None:
            return MatchReport()
        report = hook()
        return report if isinstance(report, MatchReport) else MatchReport(**report)

    @staticmethod
    def _merged(match: Match, report: MatchReport, status: MatchStatus, detail: str | None = None) -> Match:
        """The lifecycle in `match` with the game's `report`, at `status`: outcomes count once the match has started,
        and once it is final, every player without one is undecided."""
        begun = match.status is not MatchStatus.NOT_STARTED
        players = {}
        for player_id, state in match.player_states.items():
            outcome = report.outcomes.get(player_id) if begun else None
            now = PlayerStatus(outcome) if outcome else PlayerStatus.UNDECIDED if status in FINAL else state.status
            players[player_id] = PlayerState(status=now, scores=report.scores.get(player_id))
        return Match(lobby_id=match.lobby_id, status=status,
                     status_detail=detail or report.status_detail or match.status_detail,
                     progress=report.progress, player_states=players, spectator_url=match.spectator_url)

    def _end(self, status: MatchStatus, detail: str | None = None) -> Match:
        self._match = self._merged(self._match, self._report(), status, detail)
        return self._match

    def _current(self, lobby_id: str | None = None) -> Match:
        """The match a call is for: one for a lobby opened since, or before the lobby closes, is refused."""
        self.lobby.check_id(lobby_id)
        if self._match is None:
            raise GameError("no_match", f"a match is created when its lobby closes, and the lobby is "
                                        f"{self.lobby.status}")
        return self._match

    async def player_ready(self, player_id: str, lobby_id: str | None = None) -> Match:
        """A player is ready: its first move, as the game says, or the protocol's player_ready. Once every player is,
        the match starts. Nothing changes once it has, nor for the game's AI, which starts ready."""
        match = self._current(lobby_id)
        state = match.player_states.get(player_id)
        if state is None:
            raise GameError("bad_player", f"no player slot {player_id!r} plays this match; its player slots are "
                                          f"{', '.join(match.player_states)}")
        if state.status is PlayerStatus.NOT_READY and match.status is MatchStatus.NOT_STARTED:
            state.status = PlayerStatus.READY
            if all(s.status is PlayerStatus.READY for s in match.player_states.values()):
                await self._begin()
        return self.match

    async def begin_match(self, status_detail: str | None = None) -> Match:
        """Start the match now, ready or not: the game's own stall rule, with its words for who was missing. Every
        player becomes undecided and the game's `@begin_game` runs, once; a match that has started is left as it is."""
        self._current()
        await self._begin(status_detail)
        return self.match

    async def _begin(self, detail: str | None = None) -> None:
        self._beginning = self._beginning or asyncio.Lock()
        async with self._beginning:
            match = self._match
            if match is None or match.status is not MatchStatus.NOT_STARTED:
                return
            if hook := self._hooks().get("begin_game"):
                try:
                    await hook()
                except Exception as e:
                    if self._match is match:
                        self._end(MatchStatus.FAILED, f"{type(e).__name__}: {e}")
                    raise
            if self._match is match:
                match.status, match.status_detail = MatchStatus.STARTED, detail
                for state in match.player_states.values():
                    state.status = PlayerStatus.UNDECIDED

    async def finish_match(self, lobby_id: str | None = None) -> Match:
        """End the match by playing it out: the game's `@play_out` runs, with no more moves from agent and human
        players, to the end its rules or limit set, after the match starts if it hasn't. A game without one ends
        cancelled, and one whose play-out breaks, failed. A final match is returned as it is."""
        lobby_id = self._current(lobby_id).lobby_id
        self._finishing = self._finishing or asyncio.Lock()
        async with self._finishing:
            self._current(lobby_id)
            if (match := self.match).status in FINAL:
                return match
            hook = self._hooks().get("play_out")
            if hook is None:
                return self._end(MatchStatus.CANCELLED,
                                 f"{super()._build_card().name} can't be played out without its players")
            try:
                await self._begin()
                await hook()
            except Exception as e:
                if self._match is not None and self._match.lobby_id == lobby_id and self._match.status not in FINAL:
                    self._end(MatchStatus.FAILED, f"{type(e).__name__}: {e}")
                raise
            self._current(lobby_id)
            match = self.match
            return match if match.status in FINAL else self._end(MatchStatus.CANCELLED,
                                                                 "its play-out stopped before the game ended")

    def cancel_match(self, lobby_id: str | None = None) -> Match:
        """End the match where it stands: cancelled, every player without an outcome undecided. A final match is
        returned as it is."""
        self._current(lobby_id)
        match = self.match
        return match if match.status in FINAL else self._end(MatchStatus.CANCELLED)

    async def list_match_files(self, kinds: list[str] | None = None, lobby_id: str | None = None) -> dict:
        """The files the game keeps of its finished match (its `@match_files`), `kinds` of them or its default ones,
        each served at its path until the next lobby opens. Refused before the match is over."""
        self._current(lobby_id)
        if (status := self.match.status) not in FINAL:
            raise GameError("match_not_final", f"a match's files are kept once it is over, and it is {status}")
        hook = self._hooks().get("match_files")
        if hook is None:
            return {"files": [], "notes": [f"{super()._build_card().name} keeps no files of its matches"]}
        try:
            given = await hook(kinds)
        except ValueError as e:
            raise GameError("bad_request", str(e)) from e
        found = given if isinstance(given, MatchFiles) else MatchFiles(files=given)
        self._files = {f.name: f for f in found.files}
        return {"files": [{**f.model_dump(mode="json"), "bytes": f.file.stat().st_size,
                           "path": f"{MATCH_ROUTE}/files/{f.name}"} for f in found.files], "notes": found.notes}

    # ---- the spectator view ----

    def spectators_card(self) -> dict | None:
        """The spectator view's env card, the env as an onlooker sees it (its `@spectator_card`); None for a game
        without one, or before its lobby closes."""
        hook = self._hooks().get("spectator_card")
        if hook is None or self._match is None:
            return None
        card = hook()
        card = card if isinstance(card, EnvironmentCard) else EnvironmentCard(**card)
        return card.model_dump(mode="json", exclude_none=True)

    def spectating(self) -> bool:
        """Whether the current request is a spectator's, under the spectator view's `/spectators`."""
        return current_spectator()

    # ---- the license ----

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
                raise GameError("bad_license", str(e)) from e
            self._installed = sorted({*(self._installed or ()), *parts.files, *parts.keys, *parts.accepted})
        return self.license_status()

    def license_status(self) -> dict:
        """What the game lacks and what it was given, by name: never a part's contents."""
        return {"missing": [i.model_dump(mode="json", exclude_none=True) for i in self.license_missing()],
                "installed": list(self._installed or ())}

    # ---- the routes: each protocol's get at its endpoint, its other methods beneath it ----

    async def _lobby_get(self, params: dict) -> dict:
        return self.lobby.model_dump(mode="json", exclude_none=True)

    async def _open(self, params: dict) -> dict:
        if unknown := sorted(set(params) - {"game_settings", "player_slot_limits"}):
            raise GameError("bad_request", f"open takes game_settings and player_slot_limits, not {unknown}")
        try:
            limits = (PlayerSlotLimits(**params["player_slot_limits"])
                      if params.get("player_slot_limits") is not None else None)
        except TypeError as e:
            raise GameError("bad_request", f"player_slot_limits: {e}") from e
        except ValidationError as e:
            raise GameError("bad_settings", _errors(e, "player_slot_limits")) from e
        lobby = self.new_lobby(params.get("game_settings"), limits)
        return lobby.model_dump(mode="json", exclude_none=True)

    async def _fill(self, params: dict) -> dict:
        try:
            request = SlotRequest(**params)
        except ValidationError as e:
            player = any(err["loc"][:1] in (("player_kind",), ("player_name",)) for err in e.errors())
            raise GameError("bad_player" if player else "bad_request", _errors(e)) from e
        return self.fill_slot(request).model_dump(mode="json", exclude_none=True)

    async def _close(self, params: dict) -> dict:
        lobby = await self.close_lobby(_strings(params, "close", "lobby_id").get("lobby_id"))
        return lobby.model_dump(mode="json", exclude_none=True)

    async def _cancel(self, params: dict) -> dict:
        return self.cancel_lobby(_strings(params, "cancel", "lobby_id").get("lobby_id")).model_dump(
            mode="json", exclude_none=True)

    async def _match_get(self, params: dict) -> dict | None:
        match = self.match
        return None if match is None else match.model_dump(mode="json", exclude_none=True)

    async def _player_ready(self, params: dict) -> dict:
        _strings(params, "player_ready", "lobby_id", "player_id", required=("player_id",))
        match = await self.player_ready(params["player_id"], params.get("lobby_id"))
        return match.model_dump(mode="json", exclude_none=True)

    async def _finish(self, params: dict) -> dict:
        match = await self.finish_match(_strings(params, "finish", "lobby_id").get("lobby_id"))
        return match.model_dump(mode="json", exclude_none=True)

    async def _cancel_match(self, params: dict) -> dict:
        match = self.cancel_match(_strings(params, "cancel", "lobby_id").get("lobby_id"))
        return match.model_dump(mode="json", exclude_none=True)

    async def _match_files(self, params: dict) -> dict:
        if unknown := sorted(set(params) - {"lobby_id", "kinds"}):
            raise GameError("bad_request", f"files takes lobby_id and kinds, not {unknown}")
        kinds = params.get("kinds")
        if kinds is not None and not (isinstance(kinds, list) and all(isinstance(k, str) for k in kinds)):
            raise GameError("bad_request", "kinds is a list of the game's kinds of file")
        return await self.list_match_files(kinds, _strings({k: v for k, v in params.items() if k != "kinds"},
                                                           "files", "lobby_id").get("lobby_id"))

    async def _match_file(self, request: Request):
        name = request.path_params["name"]
        found = (self._files or {}).get(name)
        if found is None or not found.file.is_file():
            return JSONResponse(error_body("unknown_file", f"no file {name!r} of this match: files lists them"),
                                status_code=404)
        return FileResponse(found.file, media_type=found.content_type, filename=found.name)

    async def _license_get(self, params: dict) -> dict:
        return self.license_status()

    async def _add_license(self, params: dict) -> dict:
        if unknown := sorted(set(params) - {"files", "keys", "accept"}):
            raise GameError("bad_request", f"add takes files, keys and accept, not {unknown}")
        files, keys, accept = params.get("files") or {}, params.get("keys") or {}, params.get("accept") or []
        if not (isinstance(files, dict) and isinstance(keys, dict) and isinstance(accept, list)):
            raise GameError("bad_request", "files and keys are objects of name → value, accept a list of names")
        return self.add_license(files, keys, accept)

    def _route(self, call: Callable[[dict], Awaitable[dict | None]], failed: str) -> Callable:
        """A protocol method's handler: a refusal answers 400 with its code, anything else 500 with `failed`."""
        async def handler(request: Request) -> JSONResponse:
            try:
                raw = await request.body()
                params = json.loads(raw) if raw else {}
                if not isinstance(params, dict):
                    raise GameError("bad_request", "the request body is a JSON object")
                return JSONResponse(await call(params))
            except json.JSONDecodeError as e:
                return JSONResponse(error_body("bad_request", f"the request body is not JSON: {e}"), status_code=400)
            except GameError as e:
                return JSONResponse(error_body(e.code, e.message), status_code=400)
            except Exception as e:
                log.exception("%s failed", request.url.path)
                return JSONResponse(error_body(failed, f"{type(e).__name__}: {e}"), status_code=500)
        return handler

    def create_app(self) -> Any:
        self._hooks()
        app = super().create_app()
        for route, failed, methods in (
                (ROUTE, "lobby_failed", {"": self._lobby_get, "open": self._open, "fill": self._fill,
                                         "close": self._close, "cancel": self._cancel}),
                (MATCH_ROUTE, "match_failed", {"": self._match_get, "player_ready": self._player_ready,
                                               "finish": self._finish, "cancel": self._cancel_match,
                                               "files": self._match_files}),
                (LICENSE_ROUTE, "license_failed", {"": self._license_get, "add": self._add_license})):
            for method, call in methods.items():
                app.custom_route(f"{route}/{method}" if method else route, methods=["POST" if method else "GET"])(
                    self._route(call, failed))
        app.custom_route(f"{MATCH_ROUTE}/files/{{name}}", methods=["GET"])(self._match_file)
        routes = app.streamable_http_app
        app.streamable_http_app = lambda: PlayerPaths(routes(), card=self.slot_card, spectators=self.spectators_card)
        return app
