"""`grade_match`: grade the players of a game env's finished match from the match protocol alone (each player's
outcome and scores, and the lobby's teams), so every game built on this package is graded the same way, with no grader
of its own. Each graded player gets its own verification, scored by agent-env's weighted average, so runs, evals and
the hub read it as they read any verifier's."""

from __future__ import annotations

import logging
from typing import ClassVar

from agent_env.entity_refs import EntityRef
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agent_env.task_step.task_steps.verifiers.scoring import ScoreAggregator, aggregate_score

from .lobby import LOBBY, PlayerKind
from .match import MATCH, MatchStatus, PlayerStatus
from .steps import _deployed, _invoke

log = logging.getLogger(__name__)

OUTCOME = "outcome"
GATE_WEIGHT = -100
POINTS = {PlayerStatus.WON: 1.0, PlayerStatus.DRAWN: 0.5, PlayerStatus.UNDECIDED: 0.5, PlayerStatus.LOST: 0.0}
OUTCOMES = {PlayerStatus.WON: "won", PlayerStatus.DRAWN: "drawn: the game's own tie",
            PlayerStatus.UNDECIDED: "a draw: the match ended without deciding this player", PlayerStatus.LOST: "lost"}
PARAMS = ("player_ids", "weights", "max_missed", "verifier_id", "timeout_seconds")


def share(mine: float, best: float, better: str) -> float:
    """A score against the best opponent's as a share of the two: mine / (mine + theirs) for scores of 0 or more,
    0.5 for an even match, and for any sign 0.5 plus the lead over twice the two's sizes; reversed where lower is
    better."""
    lead, size = (mine - best if better == "higher" else best - mine), abs(mine) + abs(best)
    return 0.5 + lead / (2 * size) if size else 0.5


def ending(match: dict) -> str:
    """How the match ended in the game's words, and where its first counter, its clock, stood."""
    clock = next(iter(match.get("progress") or ()), None)
    at = "" if clock is None else f", at {clock['value']:g}" + (
        f" of {clock['limit']:g}" if clock.get("limit") is not None else "") + f" {clock['unit']}"
    return (match.get("status_detail") or "the match finished") + at


def outcome(status: PlayerStatus, match: dict) -> dict:
    return {"criterion": "the outcome: a win 1, a draw 0.5, a loss 0; undecided at the match's end is a draw",
            "result": status is PlayerStatus.WON, "score": POINTS[status],
            "evidence": f"{OUTCOMES[status]} ({ending(match)})"}


def versus(name: str, states: dict, player: str, opponents: list[str]) -> dict:
    """A score against the best opponent's: the highest, or the lowest where lower is better."""
    def score(player_id: str) -> dict | None:
        return next((s for s in states[player_id].get("scores") or () if s["name"] == name), None)

    criterion = f"{name} against the best opponent's, as a share of the two"
    mine, theirs = score(player), [s for s in map(score, opponents) if s is not None]
    if mine is None or not theirs:
        why = ("this player doesn't report it" if mine is None else "no opponent reports it" if opponents
               else "no opponent to compare with")
        return {"criterion": criterion, "result": False, "score": 0.0, "evidence": why}
    better = mine["better"]
    best = (max if better == "higher" else min)(s["value"] for s in theirs)
    part = share(mine["value"], best, better)
    unit = f" {mine['unit']}" if mine.get("unit") else ""
    return {"criterion": criterion, "result": part > 0.5, "score": part,
            "evidence": f"{mine['value']:g}{unit} vs {best:g}, {part:.0%} of the two"
                        + (" (lower is better)" if better == "lower" else "")}


def gates(moves: dict, max_missed: float | None) -> list[dict]:
    """A player that made no move, or missed more than `max_missed` of its moves, scores 0: the game's own count."""
    made, missed = moves["made"], moves["missed"]
    rows = [{"name": "played", "criterion": "the player made a move", "result": made > 0,
             "evidence": f"made {made}, missed {missed}"}]
    if max_missed is not None:
        share = missed / (made + missed) if made + missed else 0.0
        rows.append({"name": "missed", "criterion": f"missed at most {max_missed:.0%} of its moves",
                     "result": share <= max_missed, "evidence": f"missed {missed} of {made + missed} ({share:.0%})"})
    return [{**row, "weight": GATE_WEIGHT} for row in rows]


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class GradeMatchTaskStep(TaskStep):
    """Grade the players of a deployed game env's finished match: every agent's player slot unless `player_ids` names
    them, on the outcome alone by default, a win 1, a draw 0.5, a loss 0, where a player the match ended without
    deciding (nobody won by its limit) draws. `weights` merges into {"outcome": 1}: a score the game reports counts as
    its share against the best opponent's, and a weight of 0 keeps a criterion for the record only, as every score is
    kept. A game that counts its players' moves gates the grade on them: a player that made none scores 0, and so
    does one that missed more than `max_missed` (a share) of its moves. A match that didn't finish has no outcome, so
    the step fails rather than read it as a loss."""

    type: ClassVar[str] = "grade_match"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, player_ids: list | None = None,
                 weights: dict | None = None, max_missed: float | None = None, verifier_id: str | None = None,
                 timeout_seconds: int = 60,
                 depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        if player_ids is not None and not (isinstance(player_ids, list) and player_ids
                                           and all(isinstance(p, str) and p for p in player_ids)):
            raise ValueError("grade_match player_ids is a list of the player slots to grade")
        if weights is not None and not (isinstance(weights, dict)
                                        and all(isinstance(k, str) and k and _number(w) for k, w in weights.items())):
            raise ValueError("grade_match weights maps each criterion, outcome or the name of a score the game "
                             "reports, to a number")
        if max_missed is not None and not (_number(max_missed) and 0 <= max_missed <= 1):
            raise ValueError("grade_match max_missed is a share of a player's moves, 0 to 1")
        self.env_id, self.timeout_seconds, self.max_missed = env_id, timeout_seconds, max_missed
        self.player_ids = list(player_ids) if player_ids is not None else None
        self.weights, self.verifier_id = dict(weights or {}), verifier_id or id
        if not any(w > 0 for w in self.criteria().values()):
            raise ValueError("grade_match needs a criterion with a weight above 0")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, **{k: getattr(self, k) for k in PARAMS}}

    @classmethod
    def from_dict(cls, data: dict) -> GradeMatchTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"], **{k: data[k] for k in PARAMS if k in data})

    def criteria(self) -> dict[str, float]:
        return {OUTCOME: 1, **self.weights}

    def grade(self, match: dict, lobby: dict) -> dict[str, list[dict]]:
        """The rows of each graded player's verification, by its key: `verifier_id` alone when the game's one agent is
        graded, else `<verifier_id>:<player_id>`."""
        status = MatchStatus(match["status"])
        if status in (MatchStatus.CANCELLED, MatchStatus.FAILED):
            raise RuntimeError(f"grade_match: no outcome to grade, the match {status}"
                               + (f": {match['status_detail']}" if match.get("status_detail") else ""))
        if status is not MatchStatus.FINISHED:
            raise RuntimeError(f"grade_match: the match is still {status}; grade it after finish_match")
        slots = {s["player_id"]: s for s in lobby["player_slots"]}
        if unknown := [p for p in self.player_ids or () if p not in slots]:
            raise RuntimeError(f"grade_match: the match has no player slot {', '.join(map(repr, unknown))}; its "
                               f"player slots are {', '.join(map(repr, slots))}")
        graded = self.player_ids or [p for p, s in slots.items() if s["player_kind"] == PlayerKind.AGENT]
        if not graded:
            raise RuntimeError("grade_match: no agent plays the match; name the player slots to grade, player_ids")
        states = match["player_states"]
        reported = list(dict.fromkeys(s["name"] for state in states.values() for s in state.get("scores") or ()))
        if unknown := sorted(set(self.weights) - {OUTCOME} - set(reported)):
            raise RuntimeError(f"grade_match weighs {', '.join(unknown)}, which the game doesn't report: its scores "
                               f"are {', '.join(reported) or 'none'}")
        team = {p: t["team_id"] for t in lobby["player_teams"] for p in t["player_ids"]}
        alone = self.player_ids is None and len(graded) == 1
        out = {}
        for player in graded:
            opponents = [p for p in slots if team[p] != team[player]]
            rows = [self._row(OUTCOME, outcome(PlayerStatus(states[player]["status"]), match))]
            rows += [self._row(name, versus(name, states, player, opponents)) for name in reported]
            if (moves := states[player].get("moves")) is not None:
                rows += gates(moves, self.max_missed)
            elif self.max_missed is not None:
                raise RuntimeError(f"grade_match max_missed needs the game's count of moves, and it gives none for "
                                   f"player slot {player!r}")
            out[self.verifier_id if alone else f"{self.verifier_id}:{player}"] = rows
        return out

    def _row(self, name: str, row: dict) -> dict:
        weight = self.criteria().get(name, 0)
        if weight:
            return {"name": name, **row, "weight": weight}
        return {"name": name, "criterion": f"{row['criterion']} (information only)", "result": None, "weight": 0,
                "evidence": row["evidence"]}

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        lobby = await _invoke(deployed, LOBBY, "get", {}, self.timeout_seconds)
        match = await _invoke(deployed, MATCH, "get", {}, self.timeout_seconds)
        if match is None:
            raise RuntimeError(f"grade_match: env {self.env_id!r} has no match; grade it after close_lobby and "
                               "finish_match")
        ours = (context.metadata.get("game_lobby") or {}).get("lobby_id")
        if ours and match["lobby_id"] != ours:
            raise RuntimeError(f"grade_match: env {self.env_id!r}'s match is lobby {match['lobby_id']}'s, not this "
                               f"run's, {ours}")
        graded = self.grade(match, lobby)
        context.metadata["game_match"] = match
        verifications = context.metadata.setdefault("verifications", {})
        for key, rows in graded.items():
            verifications[key] = {"results": rows, "score": aggregate_score(rows, ScoreAggregator.WEIGHTED_AVERAGE)}
            log.info("grade_match %s: %s, score %.3f", key, rows[0]["evidence"], verifications[key]["score"])
        return context
