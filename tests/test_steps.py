"""create_match, add_player_slot and start_match against the served example game and a fake A2A agent."""

import pytest
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from conftest import FakeAgent, deployed, serving
from test_lobby import Arena
from tictactoe import TicTacToe

from agentenv_game.steps import AddPlayerSlotTaskStep, CreateMatchTaskStep, StartMatchTaskStep

pytestmark = pytest.mark.anyio


def add(id, player_id, player_kind, player_name=None, **more):
    return AddPlayerSlotTaskStep(id=id, version=None, env_id="tictactoe", player_id=player_id,
                                 player_kind=player_kind, player_name=player_name, **more)


def match(**more):
    return CreateMatchTaskStep(id="match", version=None, env_id="tictactoe", **more)


async def test_an_agent_gets_its_slots_mcp_address_from_the_slot_card_and_start_match_creates_the_game():
    game, alice = TicTacToe(), FakeAgent()
    async with deployed(game) as env, serving(alice.app) as agent_url:
        context = TaskStepContext(deployed_envs=[env], deployed_agents=[alice.deployed(agent_url, "alice")])
        await match().execute(context)
        step = add("slot-x", "x", "agent", "alice")
        await step.execute(context)
        await step.execute(context)   # a rerun: the same player slot, registered once
        await add("slot-o", "o", "ai").execute(context)
        await StartMatchTaskStep(id="start", version=None, env_id="tictactoe").execute(context)
    url = env.environment_url + "/players/x/mcp"
    assert alice.servers == {"tictactoe": {"url": url, "headers": None, "name": "tictactoe"}}
    slots = context.metadata["game_slots"]
    assert slots["x"]["registered"] == [url] and slots["x"]["interfaces"] == [{"url": url, "transport": "mcp"}]
    assert slots["o"] == {"player_id": "o", "player_kind": "ai", "game_settings": {}, "headers": {}}
    assert context.metadata["game_lobby"]["status"] == "closed" and game.board == [" "] * 9


async def test_an_agent_that_already_has_the_envs_address_is_refused():
    alice = FakeAgent()
    async with deployed(TicTacToe()) as env, serving(alice.app) as agent_url:
        alice.servers["tictactoe"] = {"url": env.mcp_url}
        context = TaskStepContext(deployed_envs=[env], deployed_agents=[alice.deployed(agent_url, "alice")])
        await match().execute(context)
        with pytest.raises(RuntimeError, match='deploy players with "env_ids": \\[\\]'):
            await add("slot", "x", "agent", "alice").execute(context)


async def test_a_slot_kept_for_a_player_that_connects_on_its_own_and_a_refusal():
    async with deployed(TicTacToe()) as env:
        context = TaskStepContext(deployed_envs=[env])
        await match().execute(context)
        with pytest.raises(RuntimeError, match="no deploy_agent step deployed an agent named 'alice'"):
            await add("slot", "x", "agent", "alice").execute(context)
        await add("slot", "o", "agent", "bob", register=False).execute(context)
        assert context.metadata["game_slots"]["o"]["interfaces"] == [
            {"url": env.environment_url + "/players/o/mcp", "transport": "mcp"}]
        assert "registered" not in context.metadata["game_slots"]["o"]
        with pytest.raises(RuntimeError, match="lobby fill: slot_taken: player slot 'o' is taken"):
            await add("again", "o", "agent", "carol", register=False).execute(context)


async def test_the_steps_send_the_lobby_they_opened():
    game = TicTacToe()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        await match().execute(context)
        assert context.metadata["game_lobby"]["lobby_id"] == game.lobby.lobby_id
        game.new_lobby()   # someone else opens another
        with pytest.raises(RuntimeError, match="lobby fill: lobby_replaced"):
            await add("slot", "o", "ai").execute(context)
        with pytest.raises(RuntimeError, match="lobby close: lobby_replaced"):
            await StartMatchTaskStep(id="start", version=None, env_id="tictactoe").execute(context)


async def test_a_human_gets_the_page_their_slot_card_gives():
    game = Arena()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        await match().execute(context)
        await add("slot", "dana", "human", "dana", game_settings={"faction": "elf"}).execute(context)
    slot = context.metadata["game_slots"]["dana"]
    assert slot["interfaces"] == [{"url": env.environment_url + "/players/dana/play", "transport": "http"}]
    assert slot["game_settings"] == {"faction": "elf", "label": None}


async def test_create_match_opens_the_lobby_with_the_games_settings_and_a_runs_overrides():
    game = TicTacToe()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        step = match(game_settings={"first": "x"})
        await step.execute(context)
        assert context.metadata["game_lobby"]["game_settings"] == {"first": "x"}
        context.metadata["user_overrides"] = {"step_params": {"match": {"game_settings": {"first": "o"}}}}
        await step.execute(context)
        assert game.lobby.game_settings == {"first": "o"} and game.lobby.player_slots == []
        with pytest.raises(RuntimeError, match="lobby open: bad_settings: game_settings.first"):
            await match(game_settings={"first": "z"}).execute(TaskStepContext(deployed_envs=[env]))
        with pytest.raises(RuntimeError, match="bad_settings: tic-tac-toe has two players"):
            await match(player_slot_limits={"max": 3}).execute(TaskStepContext(deployed_envs=[env]))


def test_the_steps_load_from_task_json():
    registry = get_task_step_registry()
    data = {"id": "slot", "type": "add_player_slot", "env_id": "tictactoe", "player_id": "o", "player_kind": "ai",
            "game_settings": {"faction": "orc"}, "depends_on": ["match"]}
    step = registry["add_player_slot"].from_dict(data)
    assert {k: step.to_dict()[k] for k in ("player_id", "player_kind", "game_settings")} == {
        "player_id": "o", "player_kind": "ai", "game_settings": {"faction": "orc"}}
    assert registry["start_match"].from_dict({"id": "start", "type": "start_match", "env_id": "tictactoe"})
    opened = registry["create_match"].from_dict({"id": "match", "type": "create_match", "env_id": "tictactoe",
                                                 "game_settings": {"first": "o"}})
    assert opened.to_dict()["game_settings"] == {"first": "o"} and opened.to_dict()["player_slot_limits"] is None
    with pytest.raises(ValueError, match=r"min \(3\) is more than max \(2\)"):
        match(player_slot_limits={"min": 3, "max": 2})
    with pytest.raises(ValueError, match="names its agent, player_name"):
        registry["add_player_slot"].from_dict({**data, "player_kind": "agent"})
    with pytest.raises(ValueError, match="player_id"):
        registry["add_player_slot"].from_dict({**data, "player_id": "has space"})
