"""The match, `urn:game:match/v1`: the game its lobby's close created, from its start to its end. Its status and each
player's readiness are the protocol's lifecycle, which `AgentEnvGameEnv` (env.py) keeps; whether play is on, paused,
over or broken, its progress, outcomes and scores are the game's, read from its `@match_report`. The finish_match and
cancel_match steps (steps.py) end it."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MATCH = "urn:game:match/v1"
FILE_NAME = r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$"


class MatchStatus(StrEnum):
    NOT_STARTED = "not_started"
    """The game exists, and its start gate waits for every player to be ready."""
    STARTED = "started"
    PAUSED = "paused"
    """Play is suspended, by the game, and will resume: game time doesn't pass."""
    FINISHED = "finished"
    """Ended on its own: by the game's rules, or at the first counter's limit."""
    CANCELLED = "cancelled"
    """The harness ended it first, or it had to be played out and the game can't play out."""
    FAILED = "failed"
    """The engine broke; status_detail says how."""


FINAL = frozenset({MatchStatus.FINISHED, MatchStatus.CANCELLED, MatchStatus.FAILED})
"""After one of these, nothing in the match changes."""


class PlayerStatus(StrEnum):
    NOT_READY = "not_ready"
    READY = "ready"
    """The match waits for the others; the game's AI starts here."""
    UNDECIDED = "undecided"
    """Playing, or the match ended without deciding this player's outcome."""
    WON = "won"
    LOST = "lost"
    DRAWN = "drawn"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Counter(_Model):
    """One scope's progress, outermost first in a match's: a game's seconds or turns, a battle within a turn."""

    name: str
    unit: str
    """Free text. Documented: seconds (game time), wall_seconds, turns."""
    value: int | float = Field(0, ge=0)
    limit: int | float | None = None
    """Ends this scope; the first counter's ends the match. None: no limit."""
    rate: int | float | None = None
    """Units per wall-clock second while it runs on its own: 0 stopped, None only as players act."""


class Score(_Model):
    name: str
    """The same name is the same measure, with the same `better` and `unit`, on every player that has it."""
    value: int | float
    better: Literal["higher", "lower"]
    unit: str | None = None


class PlayerState(_Model):
    status: PlayerStatus
    scores: list[Score] | None = None
    """The first is the game's main score."""


class Match(_Model):
    lobby_id: str
    """The lobby it was created from, whose player slots and teams say who plays: one match per lobby."""
    status: MatchStatus = MatchStatus.NOT_STARTED
    status_detail: str | None = None
    """The game's own words beside the status ("x won", the engine's error): shown, never parsed."""
    progress: list[Counter] = []
    player_states: dict[str, PlayerState] = {}
    """By player_id, one for each of the lobby's player slots."""
    spectator_url: str | None = None
    """Where the match's spectator view is, relative to the env, its own env card at
    `<spectator_url>/.well-known/agent-env.json`; None for a game without one."""


class MatchReport(_Model):
    """What a game's `@match_report` says of its match, which only the game knows. A status of its own counts once
    the match has started, except `failed`, which counts at any time; outcomes count once it has started."""

    status: Literal["started", "paused", "finished", "failed"] = "started"
    status_detail: str | None = None
    progress: list[Counter] = []
    outcomes: dict[str, Literal["won", "lost", "drawn"]] = {}
    """By player_id, those the rules have decided."""
    scores: dict[str, list[Score]] = {}
    """By player_id."""


class MatchFile(_Model):
    """A file a game keeps of its finished match, as its `@match_files` gives it: a replay, a video, a timeline. `kind`
    is the game's own name for what it is; `file` is where it is on disk, which the SDK serves and never sends."""

    name: str = Field(pattern=FILE_NAME)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    content_type: str = "application/octet-stream"
    file: Path = Field(exclude=True)


class MatchFiles(_Model):
    """What `@match_files` returns when it has something to say beside the files: why one is missing, say."""

    files: list[MatchFile] = []
    notes: list[str] = []
