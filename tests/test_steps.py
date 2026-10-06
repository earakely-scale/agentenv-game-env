"""add_player_slot and start_match against the served example game and a fake A2A agent."""

import pytest
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from conftest import FakeAgent, deployed, serving
from tictactoe import TicTacToe

from agentenv_game.steps import AddPlayerSlotTaskStep, StartMatchTaskStep

pytestmark = pytest.mark.anyio


def add(id, occupant, mark, **more):
    return AddPlayerSlotTaskStep(id=id, version=None, env_id="tictactoe", occupant=occupant,
                                 additional_settings={"faction": mark}, **more)


async def test_an_agent_gets_its_slots_address_and_start_match_creates_the_game():
    game, alice = TicTacToe(), FakeAgent()
    async with deployed(game) as env, serving(alice.app) as agent_url:
        context = TaskStepContext(deployed_envs=[env], deployed_agents=[alice.deployed(agent_url, "alice")])
        step = add("seat-alice", {"kind": "agent", "name": "alice"}, "x")
        await step.execute(context)
        await step.execute(context)   # a rerun: the same slot, registered once
        await add("seat-ai", {"kind": "ai"}, "o").execute(context)
        await StartMatchTaskStep(id="start", version=None, env_id="tictactoe").execute(context)
    url = env.environment_url + "/players/alice/mcp"
    assert alice.servers == {"tictactoe": {"url": url, "headers": None, "name": "tictactoe"}}
    assert context.metadata["game_slots"]["alice"]["url"] == url
    assert context.metadata["game_slots"]["slot-1"]["occupant"] == {"kind": "ai"}
    assert context.metadata["game_lobby"]["state"] == "closed" and game.board == [" "] * 9


async def test_an_agent_that_already_has_the_envs_address_is_refused():
    alice = FakeAgent()
    async with deployed(TicTacToe()) as env, serving(alice.app) as agent_url:
        alice.servers["tictactoe"] = {"url": env.mcp_url}
        context = TaskStepContext(deployed_envs=[env], deployed_agents=[alice.deployed(agent_url, "alice")])
        with pytest.raises(RuntimeError, match='deploy players with "env_ids": \\[\\]'):
            await add("seat", {"kind": "agent", "name": "alice"}, "x").execute(context)


async def test_a_slot_kept_for_a_player_that_connects_on_its_own_and_a_refusal():
    async with deployed(TicTacToe()) as env:
        context = TaskStepContext(deployed_envs=[env])
        with pytest.raises(RuntimeError, match="no deploy_agent step deployed an agent named 'alice'"):
            await add("seat", {"kind": "agent", "name": "alice"}, "x").execute(context)
        await add("seat", {"kind": "agent", "name": "bob"}, "o", register=False).execute(context)
        assert context.metadata["game_slots"]["bob"]["url"] == env.environment_url + "/players/bob/mcp"
        with pytest.raises(RuntimeError, match="lobby fill: bad_settings: o is taken"):
            await add("again", {"kind": "agent", "name": "carol"}, "o", register=False).execute(context)


def test_the_steps_load_from_task_json():
    registry = get_task_step_registry()
    data = {"id": "seat", "type": "add_player_slot", "env_id": "tictactoe", "occupant": {"kind": "ai"},
            "additional_settings": {"faction": "o"}, "depends_on": ["match"]}
    step = registry["add_player_slot"].from_dict(data)
    assert step.to_dict()["occupant"] == {"kind": "ai"} and step.to_dict()["additional_settings"] == {"faction": "o"}
    assert registry["start_match"].from_dict({"id": "start", "type": "start_match", "env_id": "tictactoe"})
    with pytest.raises(ValueError, match="agent occupants need a name"):
        registry["add_player_slot"].from_dict({**data, "occupant": {"kind": "agent"}})
