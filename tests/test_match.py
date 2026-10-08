"""The match in process: its start, with a gate and without, the game's report of it, finishing and cancelling it,
and a final match never changing."""

import inspect

import pytest
from agentenv_protocol import environment_card
from pydantic import BaseModel, ConfigDict
from tictactoe import TicTacToe

from agentenv_game import (
    AgentEnvGameEnv,
    Counter,
    GameError,
    MatchReport,
    MatchStatus,
    PlayerKind,
    PlayerSlotLimits,
    PlayerStatus,
    Score,
    SlotRequest,
    begin_game,
    create_game,
    match_report,
    play_out,
    player_slot_limits,
)

pytestmark = pytest.mark.anyio
READY, NOT_READY, UNDECIDED = PlayerStatus.READY, PlayerStatus.NOT_READY, PlayerStatus.UNDECIDED


@environment_card(name="race")
class Race(AgentEnvGameEnv):
    """A race to a finish line `length` steps away, against a clock of `limit` ticks. An agent steps when it likes;
    the game's AI steps every other tick. It starts once every runner is ready, and plays out on its clock."""

    class GameSettings(BaseModel):
        model_config = ConfigDict(extra="forbid")
        length: int = 3
        limit: int = 10

    begun = 0
    broken: str | None = None
    paused: str | None = None

    @player_slot_limits
    def runners(self, game_settings, requested):
        return PlayerSlotLimits(min=1, player_kinds=[PlayerKind.AGENT, PlayerKind.AI])

    @create_game
    async def line_up(self, lobby):
        self.length, self.limit = lobby.game_settings["length"], lobby.game_settings["limit"]
        self.ai = [s.player_id for s in lobby.player_slots if s.player_kind is PlayerKind.AI]
        self.at, self.ticks = {s.player_id: 0 for s in lobby.player_slots}, 0

    @begin_game
    async def go(self):
        self.begun += 1

    @play_out
    async def run_out(self):
        while not self.winners() and self.ticks < self.limit:
            self.tick()

    @match_report
    def report(self):
        winners = self.winners()
        over = bool(winners) or self.ticks >= self.limit
        return MatchReport(
            status="failed" if self.broken else "paused" if self.paused else "finished" if over else "started",
            status_detail=self.broken or self.paused or (f"{', '.join(winners)} won" if winners
                                                         else "out of time" if over else None),
            progress=[Counter(name="race", unit="ticks", value=self.ticks, limit=self.limit)],
            outcomes={p: "won" if p in winners else "lost" for p in self.at} if winners else {},
            scores={p: [Score(name="distance", value=d, better="higher")] for p, d in self.at.items()})

    async def step(self, player_id):
        await self.player_ready(player_id)
        if self.match.status is MatchStatus.STARTED:
            self.at[player_id] += 1

    def tick(self):
        self.ticks += 1
        if self.ticks % 2 == 0:
            for player_id in self.ai:
                self.at[player_id] += 1

    def winners(self):
        return sorted(p for p, d in self.at.items() if d >= self.length)


def runner(player_id, name):
    return SlotRequest(player_id=player_id, player_kind="agent", player_name=name)


def ai(player_id):
    return SlotRequest(player_id=player_id, player_kind="ai")


async def closed(game, *slots, **settings):
    game.new_lobby(settings)
    for slot in slots:
        game.fill_slot(slot)
    await game.close_lobby()
    return game


def statuses(match):
    return {p: s.status for p, s in match.player_states.items()}


async def refused(code, call, *args):
    with pytest.raises(GameError) as e:
        result = call(*args)
        if inspect.isawaitable(result):
            await result
    assert e.value.code == code, e.value
    return e.value.message


async def test_there_is_no_match_until_the_lobby_closes():
    game = Race()
    assert game.match is None
    game.new_lobby()
    game.fill_slot(runner("a", "alice"))
    assert "the lobby is open" in await refused("no_match", game.player_ready, "a")
    await refused("no_match", game.finish_match)
    await refused("no_match", game.cancel_match)
    await refused("no_match", game.begin_match)


async def test_a_game_without_a_gate_starts_as_its_lobby_closes_and_its_report_ends_it():
    game = await closed(TicTacToe(), runner("x", "alice"), ai("o"))
    match = game.match
    assert match.status is MatchStatus.STARTED and statuses(match) == {"x": UNDECIDED, "o": UNDECIDED}
    assert match.progress == [Counter(name="game", unit="moves", value=0, limit=9)]
    for cell in (4, 2, 6):   # the AI takes the first free cell: 0, then 1
        game._put(cell)
        game._ai_moves()
    match = game.match
    assert (match.status, match.status_detail) == (MatchStatus.FINISHED, "x won")
    assert statuses(match) == {"x": PlayerStatus.WON, "o": PlayerStatus.LOST} and match.progress[0].value == 5
    game.board, game.winner = [" "] * 9, None   # nothing the game does now changes a final match
    assert game.match is match and game.cancel_match() is match and await game.finish_match() is match


async def test_a_gated_game_starts_once_every_player_is_ready():
    game = await closed(Race(), runner("a", "alice"), runner("b", "bob"), ai("c"))
    match = game.match
    assert match.status is MatchStatus.NOT_STARTED and game.begun == 0
    assert statuses(match) == {"a": NOT_READY, "b": NOT_READY, "c": READY}
    await game.step("a")   # a first move says the player is ready, and waits for the others
    assert statuses(game.match)["a"] is READY and game.at["a"] == 0
    assert (await game.player_ready("c")).status is MatchStatus.NOT_STARTED   # the AI was ready already
    match = await game.player_ready("b")
    assert match.status is MatchStatus.STARTED and set(statuses(match).values()) == {UNDECIDED} and game.begun == 1
    await game.step("a")
    assert game.match.player_states["a"].scores == [Score(name="distance", value=1, better="higher")]
    assert (await game.player_ready("a")).status is MatchStatus.STARTED and game.begun == 1
    assert "its player slots are a, b, c" in await refused("bad_player", game.player_ready, "z")


async def test_the_games_stall_rule_starts_without_a_silent_player():
    game = await closed(Race(), runner("a", "alice"), runner("b", "bob"))
    await game.step("a")
    match = await game.begin_match("started without b: no first move in 600 s")
    assert (match.status, match.status_detail) == (MatchStatus.STARTED, "started without b: no first move in 600 s")
    assert set(statuses(match).values()) == {UNDECIDED}
    assert (await game.begin_match("again")).status_detail.startswith("started without b") and game.begun == 1


async def test_a_gated_game_of_only_the_games_ai_starts_as_its_lobby_closes():
    game = await closed(Race(), ai("c"), ai("d"))
    assert game.match.status is MatchStatus.STARTED and game.begun == 1


async def test_finish_starts_the_match_if_need_be_and_plays_it_out():
    game = await closed(Race(), runner("a", "alice"), ai("c"))
    match = await game.finish_match()
    assert game.begun == 1 and (match.status, match.status_detail) == (MatchStatus.FINISHED, "c won")
    assert statuses(match) == {"a": PlayerStatus.LOST, "c": PlayerStatus.WON} and match.progress[0].value == 6
    game = await closed(Race(), runner("a", "alice"), ai("c"), length=9, limit=4)
    match = await game.finish_match()
    assert (match.status, match.status_detail) == (MatchStatus.FINISHED, "out of time")
    assert set(statuses(match).values()) == {UNDECIDED} and match.progress[0].value == match.progress[0].limit


async def test_a_game_that_cant_be_played_out_ends_cancelled():
    game = await closed(TicTacToe(), runner("x", "alice"), ai("o"))
    game._put(4)
    game._ai_moves()
    match = await game.finish_match()
    assert (match.status, match.status_detail) == (MatchStatus.CANCELLED,
                                                   "tictactoe can't be played out without its players")
    assert set(statuses(match).values()) == {UNDECIDED} and match.progress[0].value == 2


async def test_cancel_ends_the_match_where_it_stands():
    game = await closed(Race(), runner("a", "alice"), ai("c"))
    await game.begin_match()
    game.at["a"] = 2
    match = game.cancel_match()
    assert match.status is MatchStatus.CANCELLED and set(statuses(match).values()) == {UNDECIDED}
    assert match.player_states["a"].scores[0].value == 2
    game.at["a"] = 3   # the line, too late
    assert game.match is match and game.cancel_match() is match and await game.finish_match() is match
    game = await closed(Race(), runner("a", "alice"))
    assert set(statuses(game.cancel_match()).values()) == {UNDECIDED} and game.begun == 0


async def test_the_game_reports_a_pause_and_a_failure():
    game = await closed(Race(), runner("a", "alice"), runner("b", "bob"))
    game.paused = "b's connection is down"
    assert game.match.status is MatchStatus.NOT_STARTED   # a pause counts once the match has started
    await game.begin_match()
    assert (game.match.status, game.match.status_detail) == (MatchStatus.PAUSED, "b's connection is down")
    game.paused = None
    assert game.match.status is MatchStatus.STARTED
    game = await closed(Race(), runner("a", "alice"))
    game.broken = "the server crashed"   # a failure counts at any time
    match = game.match
    assert (match.status, match.status_detail) == (MatchStatus.FAILED, "the server crashed")
    assert statuses(match) == {"a": UNDECIDED}


async def test_a_play_out_or_a_start_that_breaks_fails_the_match():
    game = await closed(Race(), runner("a", "alice"))

    async def broken():
        raise RuntimeError("the engine stopped")

    game._hooks()["play_out"] = broken
    with pytest.raises(RuntimeError, match="the engine stopped"):
        await game.finish_match()
    assert (game.match.status, game.match.status_detail) == (MatchStatus.FAILED, "RuntimeError: the engine stopped")
    game = await closed(Race(), runner("a", "alice"))
    game._hooks()["begin_game"] = broken
    with pytest.raises(RuntimeError):
        await game.player_ready("a")
    assert game.match.status is MatchStatus.FAILED and game.begun == 0


async def test_a_new_lobby_drops_the_match():
    game = await closed(Race(), runner("a", "alice"))
    old = game.lobby.lobby_id
    game.new_lobby()
    assert game.match is None
    assert f"lobby {old} was replaced" in await refused("lobby_replaced", game.finish_match, old)
