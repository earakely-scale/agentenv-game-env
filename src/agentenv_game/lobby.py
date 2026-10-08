"""The lobby, `urn:game:lobby/v1`: the player slots of the next game, filled one player at a time before the game
exists. The types and the checks every game shares; `AgentEnvGameEnv` (env.py) serves them, and the open_lobby,
add_player_slot and close_lobby steps (steps.py) call them."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

LOBBY = "urn:game:lobby/v1"
NAME = r"^[A-Za-z0-9_.-]{1,64}$"


class LobbyStatus(StrEnum):
    NOT_OPENED = "not_opened"
    OPEN = "open"
    CLOSED = "closed"
    """The game was created from the player slots."""
    CANCELLED = "cancelled"
    FAILED = "failed"
    """The game couldn't be created from the player slots."""


class PlayerKind(StrEnum):
    AGENT = "agent"
    """Plays through the env's API: a model, a scripted bot, a person with an MCP client."""
    HUMAN = "human"
    """A person through the game's own UI."""
    AI = "ai"
    """The game's built-in AI."""


NOT_OPEN = {LobbyStatus.NOT_OPENED: "no lobby is open: open one first",
            LobbyStatus.CLOSED: "the lobby is closed, its game created: open a new one for another game",
            LobbyStatus.CANCELLED: "the lobby was cancelled: open a new one for another game",
            LobbyStatus.FAILED: "the lobby failed, its game not created: open a new one to try again"}


class GameError(Exception):
    """A refusal by one of the game's protocols, the lobby, the match or the license: `code` is one of the protocol's
    (README, Errors), `message` says why. It reaches the caller as an HTTP 400 with both."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlayerSlotLimits(_Model):
    min: int | None = Field(None, ge=0)
    """At close: at least this many player slots filled."""
    max: int | None = Field(None, ge=1)
    """At fill: no more than this many."""
    player_kinds: list[PlayerKind] = [PlayerKind.AGENT]
    """Who may play: the kinds of player the game takes."""

    @model_validator(mode="after")
    def _ordered(self) -> PlayerSlotLimits:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min ({self.min}) is more than max ({self.max})")
        return self


class SlotRequest(_Model):
    """What fill takes: the player slot's id, who plays it, and the game's settings for it."""

    lobby_id: str | None = None
    """The lobby the fill is for: a lobby opened since refuses it."""
    player_id: str = Field(pattern=NAME)
    player_kind: PlayerKind
    player_name: str | None = Field(None, pattern=NAME)
    game_settings: dict[str, Any] = {}


class PlayerSlot(_Model):
    player_id: str = Field(pattern=NAME)
    """The game's own name for the player slot (a player number, a role, a colour): the protocol never reads it."""
    player_kind: PlayerKind
    player_name: str | None = Field(None, pattern=NAME)
    """Who plays it; for an agent, the agent-env agent the slot is registered with. Never for the game's AI."""
    game_settings: dict[str, Any] = {}
    """The game's own, checked against its PlayerSlotSettings: faction, team, label, AI level."""
    environment_url: str | None = None
    """Agent and human slots: the path, under the env's address, where the player slot's own env card is served."""
    headers: dict[str, str] = {}
    """What a client sends to reach that environment."""


class PlayerTeam(_Model):
    team_id: str = Field(pattern=NAME)
    """The game's own name for the team, which the protocol never reads."""
    player_ids: list[str] = Field(min_length=1)
    """Its members, by player_id: allies share a team, opponents share none."""


class Lobby(_Model):
    lobby_id: str | None = None
    """New on each open; none until one is opened."""
    status: LobbyStatus = LobbyStatus.NOT_OPENED
    game_settings: dict[str, Any] = {}
    """The game's own, checked against its GameSettings, every default filled in: a map, a seed, a time limit."""
    player_slot_limits: PlayerSlotLimits | None = None
    player_slots: list[PlayerSlot] = []
    """In the order they were filled, which means nothing: look a player slot up by its player_id."""
    player_teams: list[PlayerTeam] = []
    """Every player slot on exactly one team: the game's `@player_teams`, else a team of its own. Fixed at close."""

    def slot(self, player_id: str) -> PlayerSlot | None:
        return next((s for s in self.player_slots if s.player_id == player_id), None)

    def named(self, name: str) -> PlayerSlot | None:
        return next((s for s in self.player_slots if s.player_name == name), None)

    def check_id(self, lobby_id: str | None) -> None:
        """Refuses a call for a lobby opened before this one, or for its match."""
        if lobby_id is not None and self.lobby_id is not None and lobby_id != self.lobby_id:
            raise GameError("lobby_replaced", f"lobby {lobby_id} was replaced by {self.lobby_id}")

    def check_open(self, lobby_id: str | None = None) -> None:
        """Refuses a call for a lobby opened before this one, then one for a lobby that isn't open."""
        self.check_id(lobby_id)
        if self.status is not LobbyStatus.OPEN:
            raise GameError("lobby_not_open", NOT_OPEN[self.status])

    def place(self, request: SlotRequest) -> tuple[PlayerSlot, bool]:
        """The player slot `request` gets in this open lobby, and whether the lobby has it already (a repeated fill:
        the same id, player and settings). A new one is not added yet: the game checks it first. Raises GameError
        for the shared checks."""
        limits = self.player_slot_limits or PlayerSlotLimits()
        if request.player_kind not in limits.player_kinds:
            raise GameError("bad_player", f"the game takes {', '.join(limits.player_kinds)} players, not "
                                           f"{request.player_kind}")
        if request.player_kind is PlayerKind.AI and request.player_name is not None:
            raise GameError("bad_player", "the game's AI plays an ai player slot, so it has no player_name")
        slot = PlayerSlot(player_id=request.player_id, player_kind=request.player_kind,
                          player_name=request.player_name, game_settings=dict(request.game_settings))
        if (same := self.slot(slot.player_id)) is not None:
            if (same.player_kind, same.player_name, same.game_settings) == (
                    slot.player_kind, slot.player_name, slot.game_settings):
                return same, True
            raise GameError("slot_taken", f"player slot {slot.player_id!r} is taken")
        if slot.player_name is not None and (other := self.named(slot.player_name)) is not None:
            raise GameError("name_taken", f"{slot.player_name!r} already plays player slot {other.player_id!r}")
        if limits.max is not None and len(self.player_slots) >= limits.max:
            raise GameError("lobby_full", f"the lobby's {limits.max} player slots are taken")
        return slot, False

    def add(self, slot: PlayerSlot) -> None:
        self.player_slots = [*self.player_slots, slot]

    def check_close(self) -> None:
        least = (self.player_slot_limits or PlayerSlotLimits()).min
        if least is not None and len(self.player_slots) < least:
            raise GameError("too_few_slots", f"the game needs {least} players, the lobby has "
                                              f"{len(self.player_slots)}")
