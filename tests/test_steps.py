"""The lobby and match steps against the served example game and a fake A2A agent."""

import json
import logging

import pytest
from agent_env.artifact import FileArtifact
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from conftest import FakeAgent, deployed, serving
from test_lobby import Arena
from test_match import Race
from tictactoe import TicTacToe

from agentenv_game.steps import (
    AddPlayerSlotTaskStep,
    CancelMatchTaskStep,
    CloseLobbyTaskStep,
    FinishMatchTaskStep,
    OpenLobbyTaskStep,
    SaveMatchFilesTaskStep,
)

pytestmark = pytest.mark.anyio


def add(id, player_id, player_kind, player_name=None, **more):
    return AddPlayerSlotTaskStep(id=id, version=None, env_id="tictactoe", player_id=player_id,
                                 player_kind=player_kind, player_name=player_name, **more)


def opening(**more):
    return OpenLobbyTaskStep(id="lobby", version=None, env_id="tictactoe", **more)


async def test_an_agent_gets_its_slots_mcp_address_from_the_slot_card_and_close_lobby_creates_the_game():
    game, alice = TicTacToe(), FakeAgent()
    async with deployed(game) as env, serving(alice.app) as agent_url:
        context = TaskStepContext(deployed_envs=[env], deployed_agents=[alice.deployed(agent_url, "alice")])
        await opening().execute(context)
        step = add("slot-x", "x", "agent", "alice")
        await step.execute(context)
        await step.execute(context)   # a rerun: the same player slot, registered once
        await add("slot-o", "o", "ai").execute(context)
        await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
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
        await opening().execute(context)
        with pytest.raises(RuntimeError, match='deploy players with "env_ids": \\[\\]'):
            await add("slot", "x", "agent", "alice").execute(context)


async def test_a_slot_kept_for_a_player_that_connects_on_its_own_and_a_refusal():
    async with deployed(TicTacToe()) as env:
        context = TaskStepContext(deployed_envs=[env])
        await opening().execute(context)
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
        await opening().execute(context)
        assert context.metadata["game_lobby"]["lobby_id"] == game.lobby.lobby_id
        game.new_lobby()   # someone else opens another
        with pytest.raises(RuntimeError, match="lobby fill: lobby_replaced"):
            await add("slot", "o", "ai").execute(context)
        with pytest.raises(RuntimeError, match="lobby close: lobby_replaced"):
            await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)


async def test_a_human_gets_the_page_their_slot_card_gives():
    game = Arena()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        await opening().execute(context)
        await add("slot", "dana", "human", "dana", game_settings={"faction": "elf"}).execute(context)
    slot = context.metadata["game_slots"]["dana"]
    assert slot["interfaces"] == [{"url": env.environment_url + "/players/dana/play", "transport": "http"}]
    assert slot["game_settings"] == {"faction": "elf", "label": None}


async def test_open_lobby_opens_the_lobby_with_the_games_settings_and_a_runs_overrides():
    game = TicTacToe()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        step = opening(game_settings={"first": "x"})
        await step.execute(context)
        assert context.metadata["game_lobby"]["game_settings"] == {"first": "x"}
        context.metadata["user_overrides"] = {"step_params": {"lobby": {"game_settings": {"first": "o"}}}}
        await step.execute(context)
        assert game.lobby.game_settings == {"first": "o"} and game.lobby.player_slots == []
        with pytest.raises(RuntimeError, match="lobby open: bad_settings: game_settings.first"):
            await opening(game_settings={"first": "z"}).execute(TaskStepContext(deployed_envs=[env]))
        with pytest.raises(RuntimeError, match="bad_settings: tic-tac-toe has two players"):
            await opening(player_slot_limits={"max": 3}).execute(TaskStepContext(deployed_envs=[env]))


async def test_finish_match_plays_the_match_out_and_keeps_it_and_cancel_match_leaves_a_final_one():
    game = Race()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        finish = FinishMatchTaskStep(id="finish", version=None, env_id="tictactoe")
        await opening().execute(context)
        with pytest.raises(RuntimeError, match="match finish: no_match"):
            await finish.execute(context)
        await add("slot-a", "a", "agent", "alice", register=False).execute(context)
        await add("slot-c", "c", "ai").execute(context)
        await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
        await finish.execute(context)
        match = context.metadata["game_match"]
        assert (match["lobby_id"], match["status"], match["status_detail"]) == (
            context.metadata["game_lobby"]["lobby_id"], "finished", "c won")
        await CancelMatchTaskStep(id="cancel", version=None, env_id="tictactoe").execute(context)
        assert context.metadata["game_match"] == match


async def test_cancel_match_ends_the_match_where_it_stands():
    async with deployed(Race()) as env:
        context = TaskStepContext(deployed_envs=[env])
        await opening().execute(context)
        await add("slot-a", "a", "agent", "alice", register=False).execute(context)
        await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
        await CancelMatchTaskStep(id="cancel", version=None, env_id="tictactoe").execute(context)
    assert context.metadata["game_match"]["status"] == "cancelled"
    assert context.metadata["game_match"]["player_states"]["a"]["status"] == "undecided"


async def test_save_match_files_keeps_each_file_of_the_finished_match_as_a_file_artifact(local_stores, caplog):
    game = TicTacToe()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env], instance_id="run-1")
        save = SaveMatchFilesTaskStep(id="files", version=None, env_id="tictactoe")
        await opening().execute(context)
        await add("slot-x", "x", "agent", "alice", register=False).execute(context)
        await add("slot-o", "o", "ai").execute(context)
        await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
        with pytest.raises(RuntimeError, match="match files: match_not_final"):
            await save.execute(context)
        game._put(0)
        game._ai_moves()
        game._put(3)
        game._ai_moves()
        game._put(6)
        await save.execute(context)
        with pytest.raises(RuntimeError, match="keeps only its moves"):
            await SaveMatchFilesTaskStep(id="replay", version=None, env_id="tictactoe", kinds=["replay"]).execute(
                context)
    [kept] = context.metadata["match_files"]["files"]
    name = f"tictactoe-{game.lobby.lobby_id}.json"
    assert (kept["name"], kept["kind"]) == (name, "moves") and kept["artifact_id"].endswith(f"run-1-{name}")
    content = FileArtifact.get(kept["artifact_id"], kept["version"]).load()
    assert len(content) == kept["bytes"] and json.loads(content)["winner"] == "x"
    async with deployed(Race()) as env:
        context = TaskStepContext(deployed_envs=[env])
        await opening().execute(context)
        await add("slot-c", "c", "ai").execute(context)
        await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
        await FinishMatchTaskStep(id="finish", version=None, env_id="tictactoe").execute(context)
        with caplog.at_level(logging.WARNING):
            await SaveMatchFilesTaskStep(id="files", version=None, env_id="tictactoe").execute(context)
    assert context.metadata["match_files"] == {"files": []} and "keeps no files of its matches" in caplog.text


def test_the_steps_load_from_task_json():
    registry = get_task_step_registry()
    data = {"id": "slot", "type": "add_player_slot", "env_id": "tictactoe", "player_id": "o", "player_kind": "ai",
            "game_settings": {"faction": "orc"}, "depends_on": ["lobby"]}
    step = registry["add_player_slot"].from_dict(data)
    assert {k: step.to_dict()[k] for k in ("player_id", "player_kind", "game_settings")} == {
        "player_id": "o", "player_kind": "ai", "game_settings": {"faction": "orc"}}
    assert registry["close_lobby"].from_dict({"id": "close", "type": "close_lobby", "env_id": "tictactoe"})
    opened = registry["open_lobby"].from_dict({"id": "lobby", "type": "open_lobby", "env_id": "tictactoe",
                                                 "game_settings": {"first": "o"}})
    assert opened.to_dict()["game_settings"] == {"first": "o"} and opened.to_dict()["player_slot_limits"] is None
    finish = registry["finish_match"].from_dict({"id": "finish", "type": "finish_match", "env_id": "tictactoe"})
    cancel = registry["cancel_match"].from_dict({"id": "cancel", "type": "cancel_match", "env_id": "tictactoe",
                                                 "timeout_seconds": 30})
    assert (finish.to_dict()["timeout_seconds"], cancel.to_dict()["timeout_seconds"]) == (7200, 30)
    assert finish.entity_refs == cancel.entity_refs and finish.entity_refs
    files = registry["save_match_files"].from_dict({"id": "files", "type": "save_match_files", "env_id": "tictactoe",
                                                   "kinds": ["moves"]})
    assert files.to_dict()["kinds"] == ["moves"] and files.fail_task_on_error is False
    with pytest.raises(ValueError, match="kinds is a list"):
        SaveMatchFilesTaskStep(id="files", version=None, env_id="tictactoe", kinds="moves")
    with pytest.raises(ValueError, match=r"min \(3\) is more than max \(2\)"):
        opening(player_slot_limits={"min": 3, "max": 2})
    with pytest.raises(ValueError, match="names its agent, player_name"):
        registry["add_player_slot"].from_dict({**data, "player_kind": "agent"})
    with pytest.raises(ValueError, match="player_id"):
        registry["add_player_slot"].from_dict({**data, "player_id": "has space"})
