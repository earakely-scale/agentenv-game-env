"""The lobby on the example game, in process: its life from not opened to closed, cancelled or failed, the shared
checks and the game's, settings checked against the game's models, player slot cards, and the decorators' rules."""

import pytest
from agentenv_protocol import environment_card
from agentenv_protocol.types import EnvironmentCard, EnvironmentInterface
from pydantic import BaseModel, ConfigDict
from tictactoe import TicTacToe

from agentenv_game import (
    AgentEnvGameEnv,
    LobbyError,
    LobbyStatus,
    PlayerKind,
    PlayerSlotLimits,
    SlotRequest,
    check_player_slot,
    create_game,
    player_slot_card,
    player_slot_limits,
)

pytestmark = pytest.mark.anyio


def agent(player_id, name, **more):
    return SlotRequest(player_id=player_id, player_kind="agent", player_name=name, **more)


def ai(player_id, **more):
    return SlotRequest(player_id=player_id, player_kind="ai", **more)


def refused(code, call, *args, **kwargs):
    with pytest.raises(LobbyError) as e:
        call(*args, **kwargs)
    assert e.value.code == code, e.value
    return e.value.message


async def refused_async(code, call, *args):
    with pytest.raises(LobbyError) as e:
        await call(*args)
    assert e.value.code == code, e.value
    return e.value.message


def opened():
    game = TicTacToe()
    game.new_lobby()
    return game


def test_a_new_env_has_a_lobby_not_opened_with_the_games_defaults():
    game = TicTacToe()
    lobby = game.lobby
    assert (lobby.status, lobby.lobby_id, lobby.game_settings, lobby.player_slots) == (
        LobbyStatus.NOT_OPENED, None, {"first": "x"}, [])
    assert "open one first" in refused("lobby_not_open", game.fill_slot, agent("x", "alice"))


def test_each_open_gives_a_new_lobby_with_the_games_limits():
    game = TicTacToe()
    first = game.new_lobby({"first": "o"})
    assert first.status is LobbyStatus.OPEN and first.lobby_id.startswith("lb-")
    assert first.game_settings == {"first": "o"}
    assert first.player_slot_limits == PlayerSlotLimits(min=2, max=2, player_kinds=["agent", "ai"])
    game.fill_slot(agent("x", "alice"))
    second = game.new_lobby()
    assert second.lobby_id != first.lobby_id and second.player_slots == [] and game.lobby is second


async def test_agents_get_an_environment_the_ai_none_and_closing_creates_the_game():
    game = opened()
    first = game.fill_slot(agent("x", "alice"))
    second = game.fill_slot(ai("o"))
    assert (first.environment_url, first.headers) == ("/players/x", {})
    assert (second.environment_url, second.player_name) == (None, None)
    closed = await game.close_lobby()
    assert closed.status is LobbyStatus.CLOSED and [s.player_id for s in closed.player_slots] == ["x", "o"]
    assert game.board == [" "] * 9 and game.turn == "x"
    assert await game.close_lobby() is closed   # closing again returns the same lobby
    assert "its game created" in refused("lobby_not_open", game.fill_slot, agent("o", "bob"))


def test_the_shared_checks():
    game = opened()
    game.fill_slot(agent("x", "alice"))
    assert "already plays player slot 'x'" in refused("name_taken", game.fill_slot, agent("o", "alice"))
    assert "'x' is taken" in refused("slot_taken", game.fill_slot, agent("x", "bob"))
    game.fill_slot(ai("o"))
    assert "2 player slots are taken" in refused("lobby_full", game.fill_slot, agent("z", "carol"))


def test_the_games_own_checks():
    game = opened()
    assert 'are "x" and "o", not \'z\'' in refused("bad_slot", game.fill_slot, agent("z", "alice"))
    assert "takes agent, ai players, not human" in refused(
        "bad_player", game.fill_slot, SlotRequest(player_id="x", player_kind="human", player_name="dana"))
    assert "no player_name" in refused("bad_player", game.fill_slot, ai("x", player_name="orc"))
    assert game.lobby.player_slots == []


def test_a_repeated_fill_returns_the_slot_it_has():
    game = opened()
    first = game.fill_slot(agent("x", "alice"))
    assert game.fill_slot(agent("x", "alice")) is first and len(game.lobby.player_slots) == 1


def test_settings_are_checked_against_the_games_models():
    game = TicTacToe()
    assert "game_settings.first: Input should be 'x' or 'o'" in refused("bad_settings", game.new_lobby,
                                                                         {"first": "z"})
    assert "game_settings.second: Extra inputs" in refused("bad_settings", game.new_lobby, {"second": 1})
    assert "two players" in refused("bad_settings", game.new_lobby, {}, PlayerSlotLimits(max=3))
    game.new_lobby()
    assert "takes no game_settings here, not ['faction']" in refused(
        "bad_settings", game.fill_slot, agent("x", "alice", game_settings={"faction": "x"}))


class Faction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    faction: str = "random"
    label: str | None = None


@environment_card(name="arena")
class Arena(AgentEnvGameEnv):
    """A game of any number of players, people included, with settings per player slot and its own slot cards."""

    PlayerSlotSettings = Faction
    created = False

    @player_slot_limits
    def everyone(self, game_settings, requested):
        return PlayerSlotLimits(max=requested.max, player_kinds=list(PlayerKind))

    @player_slot_card
    def card(self, slot):
        if slot.player_kind is PlayerKind.HUMAN:
            return EnvironmentCard(name=f"arena/{slot.player_id}",
                                   additionalInterfaces=[EnvironmentInterface(url="/play", transport="http")])
        return {"name": f"arena/{slot.player_id}", "additionalInterfaces": [{"url": "/mcp", "transport": "mcp"}]}

    @create_game
    async def start(self, lobby):
        self.created = True


def test_each_player_slots_settings_get_the_models_defaults():
    game = Arena()
    game.new_lobby()
    slot = game.fill_slot(agent("red", "alice", game_settings={"faction": "orc"}))
    assert slot.game_settings == {"faction": "orc", "label": None}
    assert game.fill_slot(agent("red", "alice", game_settings={"faction": "orc", "label": None})) is slot
    assert "game_settings.faction" in refused("bad_settings", game.fill_slot,
                                              agent("blue", "bob", game_settings={"faction": 7}))


def test_player_slot_cards():
    game = opened()
    game.fill_slot(agent("x", "alice"))
    game.fill_slot(ai("o"))
    assert game.slot_card("x") == {
        "name": "tictactoe/x", "protocolVersion": game.slot_card("x")["protocolVersion"], "url": "/agentenv",
        "preferredTransport": "JSONRPC", "additionalInterfaces": [{"url": "/mcp", "transport": "mcp"}],
        "capabilities": {"operations": []}}
    assert game.slot_card("o") is None and game.slot_card("z") is None
    arena = Arena()
    arena.new_lobby()
    arena.fill_slot(SlotRequest(player_id="dana", player_kind="human", player_name="dana"))
    assert arena.slot_card("dana")["additionalInterfaces"] == [{"url": "/play", "transport": "http"}]


async def test_a_lobby_closes_only_with_enough_players():
    game = opened()
    game.fill_slot(agent("x", "alice"))
    assert "needs 2 players" in await refused_async("too_few_slots", game.close_lobby)
    assert game.lobby.status is LobbyStatus.OPEN


async def test_a_lobby_id_guards_against_a_lobby_opened_since():
    game = TicTacToe()
    old = game.new_lobby().lobby_id
    new = game.new_lobby().lobby_id
    assert f"lobby {old} was replaced by {new}" in refused("lobby_replaced", game.fill_slot,
                                                           agent("x", "alice", lobby_id=old))
    game.fill_slot(agent("x", "alice", lobby_id=new))
    game.fill_slot(ai("o", lobby_id=new))
    await refused_async("lobby_replaced", game.close_lobby, old)
    assert (await game.close_lobby(new)).status is LobbyStatus.CLOSED


async def test_a_cancelled_lobby_takes_nothing_more():
    game = opened()
    game.fill_slot(agent("x", "alice"))
    cancelled = game.cancel_lobby()
    assert cancelled.status is LobbyStatus.CANCELLED and game.cancel_lobby() is cancelled
    assert "cancelled" in refused("lobby_not_open", game.fill_slot, agent("o", "bob"))
    await refused_async("lobby_not_open", game.close_lobby)
    refused("lobby_not_open", TicTacToe().cancel_lobby)   # never opened
    assert game.new_lobby().status is LobbyStatus.OPEN


async def test_a_game_that_fails_to_create_leaves_the_lobby_failed():
    game = opened()
    game.fill_slot(agent("x", "alice"))
    game.fill_slot(ai("o"))

    async def broken(lobby):
        raise RuntimeError("the board would not load")

    game._hooks()["create_game"] = broken
    with pytest.raises(RuntimeError, match="would not load"):
        await game.close_lobby()
    assert game.lobby.status is LobbyStatus.FAILED
    assert "open a new one to try again" in await refused_async("lobby_not_open", game.close_lobby)
    refused("lobby_not_open", game.cancel_lobby)


def test_without_settings_models_a_game_takes_none():
    @environment_card(name="plain")
    class Plain(AgentEnvGameEnv):
        @create_game
        async def start(self, lobby):
            pass

    game = Plain()
    assert "takes no game_settings here, not ['map']" in refused("bad_settings", game.new_lobby, {"map": "x"})
    lobby = game.new_lobby()
    assert lobby.game_settings == {} and lobby.player_slot_limits == PlayerSlotLimits()
    assert "takes agent players, not ai" in refused("bad_player", game.fill_slot, ai("a"))
    assert game.fill_slot(agent("a", "alice")).environment_url == "/players/a"


def test_the_decorators_rules():
    class Missing(AgentEnvGameEnv):
        pass

    class Twice(AgentEnvGameEnv):
        @create_game
        async def start(self, lobby):
            pass

        @check_player_slot
        def one(self, slot, lobby):
            pass

        @check_player_slot
        def two(self, slot, lobby):
            pass

    class NotAsync(AgentEnvGameEnv):
        @create_game
        def start(self, lobby):
            pass

    class WrongArguments(AgentEnvGameEnv):
        @create_game
        async def start(self, lobby):
            pass

        @player_slot_limits
        def limits(self, game_settings):
            pass

    with pytest.raises(TypeError, match="marks no @create_game"):
        Missing().create_app()
    with pytest.raises(TypeError, match="Multiple methods marked @check_player_slot: one and two"):
        Twice().create_app()
    with pytest.raises(TypeError, match="@create_game start must be an async method taking 1 argument"):
        NotAsync().create_app()
    with pytest.raises(TypeError, match="@player_slot_limits limits must be a plain method taking 2 arguments"):
        WrongArguments().create_app()
