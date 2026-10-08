"""The lobby and the match served over HTTP, as agentenv-protocol's client invokes them; each player slot's env card;
and players' MCP clients reaching their player slots at the addresses those cards give."""

from contextlib import asynccontextmanager

import httpx
import pytest
from agentenv_protocol import client
from conftest import deployed
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_match import Race
from tictactoe import TicTacToe

from agentenv_game import LOBBY, MATCH

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def player(base: str, path: str):
    async with streamable_http_client(base + path) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def call(session: ClientSession, tool: str, **args) -> tuple[bool, str]:
    result = await session.call_tool(tool, args)
    return result.isError, "\n".join(c.text for c in result.content if getattr(c, "text", None))


async def fill(env, **slot) -> dict:
    return await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, slot, method="fill")


async def test_the_card_advertises_every_method_and_the_games_settings():
    async with deployed(TicTacToe()) as env:
        card, base = env.environment_card, env.environment_url
        methods = client.extension_params(card, LOBBY)["methods"]
        assert {m: (v["method"], v["endpoint"]) for m, v in methods.items()} == {
            "open": ("POST", "/agentenv/ext/lobby/open"), "get": ("GET", "/agentenv/ext/lobby"),
            "fill": ("POST", "/agentenv/ext/lobby/fill"), "close": ("POST", "/agentenv/ext/lobby/close"),
            "cancel": ("POST", "/agentenv/ext/lobby/cancel")}
        settings = methods["open"]["request"]["properties"]["game_settings"]
        assert settings["properties"]["first"]["enum"] == ["x", "o"] and settings["additionalProperties"] is False
        assert methods["fill"]["request"]["properties"]["game_settings"] == {"type": "object", "maxProperties": 0}
        assert (await client.invoke_extension(base, card, LOBBY, method="get"))["status"] == "not_opened"
        opened = await client.invoke_extension(base, card, LOBBY, {"game_settings": {"first": "o"}}, method="open")
        assert opened["status"] == "open" and opened["game_settings"] == {"first": "o"}
        assert opened["player_slot_limits"] == {"min": 2, "max": 2, "player_kinds": ["agent", "ai"]}
        slot = await fill(env, lobby_id=opened["lobby_id"], player_id="x", player_kind="agent", player_name="alice")
        assert slot == {"player_id": "x", "player_kind": "agent", "player_name": "alice", "game_settings": {},
                        "environment_url": "/players/x", "headers": {}}
        await fill(env, player_id="o", player_kind="ai")
        closed = await client.invoke_extension(base, card, LOBBY, {"lobby_id": opened["lobby_id"]}, method="close")
        assert closed["status"] == "closed" and "game" not in closed and len(closed["player_slots"]) == 2
        assert closed["player_teams"] == [{"team_id": "x", "player_ids": ["x"]}, {"team_id": "o", "player_ids": ["o"]}]
        assert (await client.invoke_extension(base, card, LOBBY, method="get"))["status"] == "closed"


async def test_a_refusal_is_a_400_with_the_lobbys_code():
    async with deployed(TicTacToe()) as env, httpx.AsyncClient() as http:
        lobby = f"{env.environment_url}/agentenv/ext/lobby"

        async def post(method, body):
            response = await http.post(f"{lobby}/{method}", json=body)
            return response.status_code, response.json().get("error", {}).get("code")

        assert await post("fill", {"player_id": "x", "player_kind": "ai"}) == (400, "lobby_not_open")
        assert await post("open", {"settings": {}}) == (400, "bad_request")
        assert await post("open", {"game_settings": {"first": "z"}}) == (400, "bad_settings")
        assert await post("open", {"game_settings": []}) == (400, "bad_request")
        assert await post("open", {"player_slot_limits": {"min": 3, "max": 2}}) == (400, "bad_settings")
        assert (await http.post(f"{lobby}/open", json={})).status_code == 200
        cases = [({"player_id": "x", "player_kind": "robot"}, "bad_player"),
                 ({"player_id": "x", "player_kind": "agent", "player_name": "has space"}, "bad_player"),
                 ({"player_kind": "ai"}, "bad_request"),
                 ({"player_id": "x", "player_kind": "ai", "slot": 1}, "bad_request"),
                 ({"player_id": "z", "player_kind": "ai"}, "bad_slot"),
                 ({"player_id": "x", "player_kind": "ai", "game_settings": {"faction": "x"}}, "bad_settings"),
                 ({"player_id": "x", "player_kind": "ai", "lobby_id": "lb-old"}, "lobby_replaced")]
        for body, code in cases:
            assert await post("fill", body) == (400, code), body
        assert await post("close", {}) == (400, "too_few_slots")
        assert await post("close", {"id": 1}) == (400, "bad_request")


async def test_each_player_slot_serves_its_own_card():
    async with deployed(TicTacToe()) as env, httpx.AsyncClient() as http:
        base = env.environment_url
        await client.invoke_extension(base, env.environment_card, LOBBY, {}, method="open")
        await fill(env, player_id="x", player_kind="agent", player_name="alice")
        await fill(env, player_id="o", player_kind="ai")
        card = (await http.get(f"{base}/players/x/.well-known/agent-env.json")).json()
        assert card["name"] == "tictactoe/x" and client.mcp_path(card) == "/mcp"
        assert card["capabilities"] == {"operations": []}
        for missing in ("o", "carol"):   # the AI's slot has no environment; carol plays none
            response = await http.get(f"{base}/players/{missing}/.well-known/agent-env.json")
            assert response.status_code == 404 and response.json()["error"]["code"] == "unknown_player"
        assert (await http.get(f"{base}/.well-known/agent-env.json")).json()["name"] == "tictactoe"


async def test_each_player_plays_its_own_slot_by_its_address():
    game = TicTacToe()
    async with deployed(game) as env:
        await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, {}, method="open")
        for mark, name in (("x", "alice"), ("o", "bob")):
            await fill(env, player_id=mark, player_kind="agent", player_name=name)
        await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, {}, method="close")
        base = env.environment_url
        async with player(base, "/players/x/mcp") as alice, player(base, "/players/o/mcp") as bob:
            assert (await call(alice, "show_board"))[1].startswith("You are x.")
            assert (await call(bob, "show_board"))[1].startswith("You are o.")
            assert "it is x's turn" in (await call(bob, "mark", cell=0))[1]
            for who, cell in ((alice, 0), (bob, 3), (alice, 1), (bob, 4), (alice, 2)):
                failed, text = await call(who, "mark", cell=cell)
                assert not failed and "Not marked" not in text
            assert "Not marked: the match is finished." in (await call(bob, "mark", cell=5))[1]
        assert game.winner == "x" and text.endswith("x won.")
        match = await client.invoke_extension(base, env.environment_card, MATCH, method="get")
        assert {k: match[k] for k in ("status", "status_detail", "player_states")} == {
            "status": "finished", "status_detail": "x won",
            "player_states": {"x": {"status": "won"}, "o": {"status": "lost"}}}
        async with player(base, "/players/carol/mcp") as carol:
            failed, text = await call(carol, "show_board")
            assert failed and "no player slot 'carol' is played here; this game's are x, o" in text
        async with player(base, "/mcp") as root:
            failed, text = await call(root, "show_board")
            assert failed and "/players/<x|o>/mcp" in text


async def test_against_the_games_ai():
    game = TicTacToe()
    async with deployed(game) as env:
        card, base = env.environment_card, env.environment_url
        await client.invoke_extension(base, card, LOBBY, {"game_settings": {"first": "o"}}, method="open")
        await fill(env, player_id="o", player_kind="ai")
        await fill(env, player_id="x", player_kind="agent", player_name="alice")
        await client.invoke_extension(base, card, LOBBY, {}, method="close")
        assert game.board[0] == "o"   # the AI moved first, as the game was created
        async with player(base, "/players/x/mcp") as alice:
            text = (await call(alice, "mark", cell=4))[1]
        assert game.board.count("o") == 2 and game.board[4] == "x" and "x to play" in text


async def test_a_game_that_fails_to_create_leaves_the_lobby_failed_and_cancel_ends_one():
    game = TicTacToe()

    async def broken(lobby):
        raise RuntimeError("the board would not load")

    async with deployed(game) as env, httpx.AsyncClient() as http:
        game._hooks()["create_game"] = broken
        lobby = env.environment_url + "/agentenv/ext/lobby"
        await http.post(f"{lobby}/open", json={})
        for body in ({"player_id": "x", "player_kind": "agent", "player_name": "alice"},
                     {"player_id": "o", "player_kind": "ai"}):
            assert (await http.post(f"{lobby}/fill", json=body)).status_code == 200
        failed = await http.post(f"{lobby}/close", json={})
        assert failed.status_code == 500 and failed.json()["error"] == {
            "code": "lobby_failed", "message": "RuntimeError: the board would not load"}
        assert (await http.get(lobby)).json()["status"] == "failed"
        again = await http.post(f"{lobby}/close", json={})
        assert again.status_code == 400 and again.json()["error"]["code"] == "lobby_not_open"
        opened = (await http.post(f"{lobby}/open", json={})).json()
        cancelled = await http.post(f"{lobby}/cancel", json={"lobby_id": opened["lobby_id"]})
        assert cancelled.json()["status"] == "cancelled" and cancelled.json()["lobby_id"] == opened["lobby_id"]


async def test_the_match_over_http():
    async with deployed(TicTacToe()) as env, httpx.AsyncClient() as http:
        card, base = env.environment_card, env.environment_url
        methods = client.extension_params(card, MATCH)["methods"]
        assert {m: (v["method"], v["endpoint"]) for m, v in methods.items()} == {
            "get": ("GET", "/agentenv/ext/match"), "player_ready": ("POST", "/agentenv/ext/match/player_ready"),
            "finish": ("POST", "/agentenv/ext/match/finish"), "cancel": ("POST", "/agentenv/ext/match/cancel"),
            "files": ("POST", "/agentenv/ext/match/files")}
        match = f"{base}/agentenv/ext/match"

        async def post(method, body):
            response = await http.post(f"{match}/{method}", json=body)
            return response.status_code, response.json().get("error", {}).get("code")

        assert (await http.get(match)).json() is None
        assert await post("finish", {}) == (400, "no_match")
        opened = await client.invoke_extension(base, card, LOBBY, {}, method="open")
        await fill(env, player_id="x", player_kind="agent", player_name="alice")
        await fill(env, player_id="o", player_kind="ai")
        await client.invoke_extension(base, card, LOBBY, {}, method="close")
        assert await client.invoke_extension(base, card, MATCH, method="get") == {
            "lobby_id": opened["lobby_id"], "status": "started",
            "progress": [{"name": "game", "unit": "moves", "value": 0, "limit": 9}],
            "player_states": {"x": {"status": "undecided"}, "o": {"status": "undecided"}},
            "spectator_url": "/spectators"}
        for method, body, code in (("player_ready", {}, "bad_request"),
                                   ("player_ready", {"player_id": "z"}, "bad_player"),
                                   ("player_ready", {"player_id": 1}, "bad_request"),
                                   ("finish", {"lobby_id": "lb-old"}, "lobby_replaced"),
                                   ("cancel", {"reason": "x"}, "bad_request")):
            assert await post(method, body) == (400, code), (method, body)
        ready = await client.invoke_extension(base, card, MATCH, {"player_id": "x"}, method="player_ready")
        assert ready["status"] == "started"
        finished = await client.invoke_extension(base, card, MATCH, {"lobby_id": opened["lobby_id"]}, method="finish")
        assert (finished["status"], finished["status_detail"]) == (
            "cancelled", "tictactoe can't be played out without its players")
        assert await client.invoke_extension(base, card, MATCH, {}, method="cancel") == finished


async def test_a_gated_match_starts_once_its_players_say_they_are_ready_and_a_broken_play_out_answers_500():
    game = Race()
    async with deployed(game) as env, httpx.AsyncClient() as http:
        card, base = env.environment_card, env.environment_url
        await client.invoke_extension(base, card, LOBBY, {}, method="open")
        await fill(env, player_id="a", player_kind="agent", player_name="alice")
        await fill(env, player_id="c", player_kind="ai")
        await client.invoke_extension(base, card, LOBBY, {}, method="close")
        waiting = await client.invoke_extension(base, card, MATCH, method="get")
        assert waiting["status"] == "not_started" and waiting["player_states"]["a"]["status"] == "not_ready"
        started = await client.invoke_extension(base, card, MATCH, {"player_id": "a"}, method="player_ready")
        assert started["status"] == "started" and game.begun == 1

        async def broken():
            raise RuntimeError("the engine stopped")

        game._hooks()["play_out"] = broken
        failed = await http.post(f"{base}/agentenv/ext/match/finish", json={})
        assert failed.status_code == 500 and failed.json()["error"] == {
            "code": "match_failed", "message": "RuntimeError: the engine stopped"}
        assert (await http.get(f"{base}/agentenv/ext/match")).json()["status"] == "failed"


async def two_ais(env) -> str:
    """A tic-tac-toe match between the game's AIs, played to its end as the lobby closes; its lobby_id."""
    opened = await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, {}, method="open")
    await fill(env, player_id="x", player_kind="ai")
    await fill(env, player_id="o", player_kind="ai")
    await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, {}, method="close")
    return opened["lobby_id"]


async def test_spectators_get_their_own_card_and_the_game_knows_their_requests():
    async with deployed(TicTacToe()) as env, httpx.AsyncClient() as http:
        card = f"{env.environment_url}/spectators/.well-known/agent-env.json"
        before = await http.get(card)
        assert before.status_code == 404 and before.json()["error"]["code"] == "no_spectator_view"
        await two_ais(env)
        spectators = (await http.get(card)).json()
        assert spectators["name"] == "tictactoe/spectators"
        assert spectators["additionalInterfaces"] == [{"url": "/spectate", "transport": "http"}]
        assert "fetch(\"board.json\")" in (await http.get(f"{env.environment_url}/spectators/spectate")).text
        watched = (await http.get(f"{env.environment_url}/spectators/board.json")).json()
        assert watched["spectating"] is True and watched["winner"] is not None
        assert (await http.get(f"{env.environment_url}/board.json")).json()["spectating"] is False
    async with deployed(Race()) as env, httpx.AsyncClient() as http:
        await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, {}, method="open")
        await fill(env, player_id="c", player_kind="ai")
        await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, {}, method="close")
        assert "spectator_url" not in await client.invoke_extension(env.environment_url, env.environment_card,
                                                                    MATCH, method="get")
        gone = await http.get(f"{env.environment_url}/spectators/.well-known/agent-env.json")
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "no_spectator_view"


async def test_a_finished_match_lists_its_files_and_serves_each_until_the_next_lobby():
    async with deployed(TicTacToe()) as env, httpx.AsyncClient() as http:
        base, card = env.environment_url, env.environment_card
        files = f"{base}/agentenv/ext/match/files"
        await client.invoke_extension(base, card, LOBBY, {}, method="open")
        await fill(env, player_id="x", player_kind="agent", player_name="alice")
        await fill(env, player_id="o", player_kind="ai")
        await client.invoke_extension(base, card, LOBBY, {}, method="close")
        early = await http.post(files, json={})
        assert early.status_code == 400 and early.json()["error"]["code"] == "match_not_final"
        lobby_id = await two_ais(env)
        listed = await client.invoke_extension(base, card, MATCH, {"lobby_id": lobby_id}, method="files")
        [kept] = listed["files"]
        name = f"tictactoe-{lobby_id}.json"
        assert {k: kept[k] for k in ("name", "kind", "content_type", "path")} == {
            "name": name, "kind": "moves", "content_type": "application/json",
            "path": f"/agentenv/ext/match/files/{name}"} and listed["notes"] == []
        served = await http.get(base + kept["path"])
        assert served.headers["content-type"] == "application/json" and len(served.content) == kept["bytes"]
        assert served.json()["moves"][0] == {"mark": "x", "cell": 0}
        refused = await http.post(files, json={"kinds": ["replay"]})
        assert refused.status_code == 400 and "keeps only its moves" in refused.json()["error"]["message"]
        assert (await http.post(files, json={"kinds": "moves"})).json()["error"]["code"] == "bad_request"
        unknown = await http.get(f"{files}/other.json")
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "unknown_file"
        await client.invoke_extension(base, card, LOBBY, {}, method="open")
        assert (await http.get(base + kept["path"])).status_code == 404
    async with deployed(Race()) as env:
        await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, {}, method="open")
        await fill(env, player_id="c", player_kind="ai")
        await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, {}, method="close")
        await client.invoke_extension(env.environment_url, env.environment_card, MATCH, {}, method="finish")
        none = await client.invoke_extension(env.environment_url, env.environment_card, MATCH, {}, method="files")
        assert none["files"] == [] and none["notes"][0].endswith("keeps no files of its matches")
