"""The lobby on the example game, in process: the shared checks, the game's, repeated fills, closing, and the
decorators' rules."""

import pytest
from agentenv_protocol import environment_card
from tictactoe import TicTacToe

from agentenv_game import (
    AgentEnvGameEnv,
    Connect,
    LobbyError,
    LobbyState,
    Occupant,
    PlayerSlotSettings,
    SlotRequest,
    check_slot,
    connect_slot,
    create_game,
    play_link,
)

pytestmark = pytest.mark.anyio


def agent(name, mark, slot=None):
    return SlotRequest(occupant=Occupant(kind="agent", name=name), slot=slot, additional_settings={"faction": mark})


def ai(mark):
    return SlotRequest(occupant=Occupant(kind="ai"), additional_settings={"faction": mark})


def refused(code, call, *args):
    with pytest.raises(LobbyError) as e:
        call(*args)
    assert e.value.code == code, e.value
    return e.value.message


def test_a_new_env_has_an_open_lobby_with_the_games_defaults():
    game = TicTacToe()
    lobby = game.lobby
    assert not game.lobby_opened
    assert lobby.state is LobbyState.OPEN and lobby.additional_settings == {"first": "x"} and lobby.slots == []
    assert (lobby.player_slot_settings.min, lobby.player_slot_settings.max) == (2, 2)
    assert lobby.player_slot_settings.additional_settings == {"occupants": ["agent", "ai"], "factions": ["x", "o"]}


async def test_agents_get_an_address_the_ai_none_and_closing_creates_the_game():
    game = TicTacToe()
    first = game.fill_slot(agent("alice", "x"))
    second = game.fill_slot(ai("o"))
    assert (first.slot, first.connect) == (0, Connect(path="/players/alice/mcp"))
    assert (second.slot, second.connect, second.occupant.name) == (1, None, None)
    closed = await game.close_lobby()
    assert closed["state"] == "closed" and closed["game"] == {"first": "x"} and len(closed["slots"]) == 2
    assert game.board == [" "] * 9 and game.turn == "x"
    assert await game.close_lobby() == closed   # closing again returns the same game
    refused("lobby_closed", game.fill_slot, agent("bob", "o"))


def test_the_shared_checks():
    game = TicTacToe()
    game.fill_slot(agent("alice", "x"))
    assert "already has slot 0" in refused("name_taken", game.fill_slot, agent("alice", "o"))
    refused("slot_taken", game.fill_slot, agent("bob", "o", slot=0))
    assert "slots 0 to 1" in refused("bad_slot", game.fill_slot, agent("bob", "o", slot=2))
    game.fill_slot(ai("o"))
    refused("lobby_full", game.fill_slot, agent("carol", "o"))


def test_a_repeated_fill_returns_the_slot_it_has():
    game = TicTacToe()
    first = game.fill_slot(agent("alice", "x"))
    assert game.fill_slot(agent("alice", "x")) == first == game.fill_slot(agent("alice", "x", slot=0))
    assert len(game.lobby.slots) == 1


def test_the_games_own_checks_and_settings():
    game = TicTacToe()
    assert '"x" or "o"' in refused("bad_settings", game.fill_slot, agent("alice", "z"))
    game.fill_slot(agent("alice", "x"))
    assert "x is taken" in refused("bad_settings", game.fill_slot, agent("bob", "x"))
    assert "takes no human players" in refused(
        "bad_occupant", game.fill_slot, SlotRequest(occupant=Occupant(kind="human", name="dana")))
    assert "who moves first" in refused("bad_settings", game.new_lobby, {"first": "z"})
    assert "two players" in refused("bad_settings", game.new_lobby, {}, PlayerSlotSettings(max=3))


async def test_a_lobby_closes_only_with_enough_players_and_a_new_one_drops_the_game():
    game = TicTacToe()
    game.fill_slot(agent("alice", "x"))
    assert "needs 2 players" in await _refused_async("too_few_slots", game.close_lobby)
    game.fill_slot(ai("o"))
    await game.close_lobby()
    lobby = game.new_lobby({"first": "o"})
    assert lobby.state is LobbyState.OPEN and lobby.slots == [] and game.lobby is lobby and game.lobby_opened


async def _refused_async(code, call):
    with pytest.raises(LobbyError) as e:
        await call()
    assert e.value.code == code
    return e.value.message


def test_occupants_need_names_except_the_ai():
    with pytest.raises(ValueError, match="agent occupants need a name"):
        Occupant(kind="agent")
    with pytest.raises(ValueError):
        Occupant(kind="agent", name="has space")
    assert Occupant(kind="ai").name is None


def test_a_game_with_a_header_or_its_own_connect_and_human_players():
    @environment_card(name="by-header")
    class ByHeader(AgentEnvGameEnv):
        player_header = "X-Player"

        @create_game
        async def start(self, lobby):
            return {}

    @environment_card(name="custom")
    class Custom(AgentEnvGameEnv):
        @create_game
        async def start(self, lobby):
            return {}

        @connect_slot
        def address(self, slot):
            return {"path": "/mcp", "headers": {"X-Seat": slot.occupant.name}}

        @play_link
        def link(self, slot):
            return f"/play#{slot.occupant.name}"

    named = SlotRequest(occupant=Occupant(kind="agent", name="alice"))
    assert ByHeader().fill_slot(named).connect == Connect(path="/mcp", headers={"X-Player": "alice"})
    custom = Custom()
    assert custom.fill_slot(named).connect == Connect(path="/mcp", headers={"X-Seat": "alice"})
    person = custom.fill_slot(SlotRequest(occupant=Occupant(kind="human", name="dana")))
    assert (person.play, person.connect) == ("/play#dana", None)


def test_the_decorators_rules():
    class Missing(AgentEnvGameEnv):
        pass

    class Twice(AgentEnvGameEnv):
        @create_game
        async def start(self, lobby):
            return {}

        @check_slot
        def one(self, slot, lobby):
            pass

        @check_slot
        def two(self, slot, lobby):
            pass

    class NotAsync(AgentEnvGameEnv):
        @create_game
        def start(self, lobby):
            return {}

    class WrongArguments(AgentEnvGameEnv):
        @create_game
        async def start(self, lobby):
            return {}

        @check_slot
        def rules(self, slot):
            pass

    with pytest.raises(TypeError, match="marks no @create_game"):
        Missing().create_app()
    with pytest.raises(TypeError, match="Multiple methods marked @check_slot: one and two"):
        Twice().create_app()
    with pytest.raises(TypeError, match="@create_game start must be an async method taking 1 argument"):
        NotAsync().create_app()
    with pytest.raises(TypeError, match="@check_slot rules must be a plain method taking 2 arguments"):
        WrongArguments().create_app()
