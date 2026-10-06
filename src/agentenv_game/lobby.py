"""The lobby, `urn:game:lobby/v1`: the player slots of the next game, filled one occupant at a time before the game
exists. The types and the checks every game shares; `AgentEnvGameEnv` (env.py) serves them, and the add_player_slot
and start_match steps (steps.py) call them."""

from __future__ import annotations

import itertools
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

LOBBY = "urn:game:lobby/v1"
NAME = r"^[A-Za-z0-9_.-]{1,64}$"


class LobbyState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class OccupantKind(StrEnum):
    AGENT = "agent"
    """Plays through the env's API: a model, a scripted bot, a person with an MCP client."""
    HUMAN = "human"
    """A person through the game's own UI."""
    AI = "ai"
    """The game's built-in AI."""


class LobbyError(Exception):
    """A refusal: `code` is one of the protocol's (README, Errors), `message` says why. It reaches the caller as the
    message `<code>: <message>`."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Occupant(_Model):
    kind: OccupantKind
    name: str | None = Field(None, pattern=NAME)

    @model_validator(mode="after")
    def _named(self) -> Occupant:
        if self.kind is not OccupantKind.AI and self.name is None:
            raise ValueError(f"{self.kind.value} occupants need a name")
        return self


class PlayerSlotSettings(_Model):
    min: int | None = Field(None, ge=0)
    """At close: at least this many slots filled."""
    max: int | None = Field(None, ge=1)
    """At fill: no more than this many slots, numbered from 0."""
    additional_settings: dict[str, Any] = {}
    """The game's own: which occupants, factions, teams and AI levels it takes."""

    @model_validator(mode="after")
    def _ordered(self) -> PlayerSlotSettings:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min ({self.min}) is more than max ({self.max})")
        return self


class Connect(_Model):
    """How an agent's MCP client reaches its slot: a path under the env's address, and headers to send."""

    path: str
    headers: dict[str, str] = {}


class SlotRequest(_Model):
    """What fill takes: the occupant, a slot (the next free one by default), and the game's settings for the slot."""

    occupant: Occupant
    slot: int | None = Field(None, ge=0)
    additional_settings: dict[str, Any] = {}


class PlayerSlot(_Model):
    slot: int = Field(ge=0)
    occupant: Occupant
    additional_settings: dict[str, Any] = {}
    """The game's own: faction, team, AI level, label."""
    connect: Connect | None = None
    """Agent slots: set by the env."""
    play: str | None = None
    """Human slots: a link for the person, set by the env."""


class Lobby(_Model):
    state: LobbyState = LobbyState.OPEN
    additional_settings: dict[str, Any] = {}
    """The game's and the task's own settings: a map, a seed, a time limit."""
    player_slot_settings: PlayerSlotSettings | None = None
    """None: any number of slots."""
    slots: list[PlayerSlot] = []

    def named(self, name: str) -> PlayerSlot | None:
        return next((s for s in self.slots if s.occupant.name == name), None)

    def place(self, request: SlotRequest) -> tuple[PlayerSlot, bool]:
        """The slot `request` gets, and whether the lobby has it already (a repeated fill: the same name, slot and
        settings). A new slot is not added yet: the game checks it first. Raises LobbyError for the shared checks."""
        if self.state is not LobbyState.OPEN:
            raise LobbyError("lobby_closed", "the lobby is closed: open a new one for another game")
        name = request.occupant.name
        if name is not None and (same := self.named(name)) is not None:
            if (same.occupant == request.occupant and request.slot in (None, same.slot)
                    and same.additional_settings == request.additional_settings):
                return same, True
            raise LobbyError("name_taken", f"{name!r} already has slot {same.slot}, with other settings")
        limit = (self.player_slot_settings or PlayerSlotSettings()).max
        if limit is not None and len(self.slots) >= limit:
            raise LobbyError("lobby_full", f"the lobby's {limit} slots are taken")
        used = {s.slot for s in self.slots}
        index = request.slot if request.slot is not None else next(i for i in itertools.count() if i not in used)
        if limit is not None and index >= limit:
            raise LobbyError("bad_slot", f"slot {index} is out of range: the lobby has slots 0 to {limit - 1}")
        if index in used:
            raise LobbyError("slot_taken", f"slot {index} is taken")
        return PlayerSlot(slot=index, occupant=request.occupant,
                          additional_settings=dict(request.additional_settings)), False

    def add(self, slot: PlayerSlot) -> None:
        self.slots = sorted([*self.slots, slot], key=lambda s: s.slot)

    def check_close(self) -> None:
        least = (self.player_slot_settings or PlayerSlotSettings()).min
        if least is not None and len(self.slots) < least:
            raise LobbyError("too_few_slots", f"the game needs {least} players, the lobby has {len(self.slots)}")
