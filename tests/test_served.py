"""The lobby served over HTTP, as agentenv-protocol's client invokes it, and players' MCP clients reaching their
slots by their addresses."""

from contextlib import asynccontextmanager

import httpx
import pytest
from agentenv_protocol import client
from conftest import deployed
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from tictactoe import TicTacToe

from agentenv_game import LOBBY

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def player(base: str, path: str):
    async with streamable_http_client(base + path) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def call(session: ClientSession, tool: str, **args) -> tuple[bool, str]:
    result = await session.call_tool(tool, args)
    return result.isError, "\n".join(c.text for c in result.content if getattr(c, "text", None))


async def test_the_card_advertises_every_method_and_each_works():
    async with deployed(TicTacToe()) as env:
        card, base = env.environment_card, env.environment_url
        methods = client.extension_params(card, LOBBY)["methods"]
        assert {m: (v["method"], v["endpoint"]) for m, v in methods.items()} == {
            "open": ("POST", "/agentenv/ext/lobby/open"), "get": ("GET", "/agentenv/ext/lobby"),
            "fill": ("POST", "/agentenv/ext/lobby/fill"), "close": ("POST", "/agentenv/ext/lobby/close")}
        opened = await client.invoke_extension(base, card, LOBBY, {"additional_settings": {"first": "o"}},
                                               method="open")
        assert opened["state"] == "open" and opened["additional_settings"] == {"first": "o"}
        slot = await client.invoke_extension(base, card, LOBBY, {"occupant": {"kind": "agent", "name": "alice"},
                                                                 "additional_settings": {"faction": "x"}},
                                             method="fill")
        assert slot["connect"] == {"path": "/players/alice/mcp", "headers": {}}
        await client.invoke_extension(base, card, LOBBY, {"occupant": {"kind": "ai"},
                                                          "additional_settings": {"faction": "o"}}, method="fill")
        closed = await client.invoke_extension(base, card, LOBBY, {}, method="close")
        assert closed["state"] == "closed" and closed["game"] == {"first": "o"}
        assert (await client.invoke_extension(base, card, LOBBY, method="get"))["state"] == "closed"


async def test_a_refusal_is_a_400_with_the_lobbys_code():
    async with deployed(TicTacToe()) as env:
        base = env.environment_url
        async with httpx.AsyncClient() as http:
            cases = [("fill", {"occupant": {"kind": "agent"}}, "bad_occupant"),
                     ("fill", {"occupant": {"kind": "ai"}, "seat": 1}, "bad_request"),
                     ("fill", {"occupant": {"kind": "ai"}, "additional_settings": {"faction": "q"}}, "bad_settings"),
                     ("open", {"additional_settings": {"first": "z"}}, "bad_settings"),
                     ("open", {"settings": {}}, "bad_request"),
                     ("close", {}, "too_few_slots")]
            for method, body, code in cases:
                response = await http.post(f"{base}/agentenv/ext/lobby/{method}", json=body)
                assert (response.status_code, response.json()["error"]["code"]) == (400, code), (method, body)


async def test_each_player_plays_its_own_slot_by_its_address():
    game = TicTacToe()
    async with deployed(game) as env:
        card, base = env.environment_card, env.environment_url
        for name, mark in (("alice", "x"), ("bob", "o")):
            await client.invoke_extension(base, card, LOBBY, {"occupant": {"kind": "agent", "name": name},
                                                              "additional_settings": {"faction": mark}},
                                          method="fill")
        await client.invoke_extension(base, card, LOBBY, {}, method="close")
        async with player(base, "/players/alice/mcp") as alice, player(base, "/players/bob/mcp") as bob:
            assert (await call(alice, "show_board"))[1].startswith("You are x.")
            assert (await call(bob, "show_board"))[1].startswith("You are o.")
            assert "it is x's turn" in (await call(bob, "mark", cell=0))[1]
            for who, cell in ((alice, 0), (bob, 3), (alice, 1), (bob, 4), (alice, 2)):
                failed, text = await call(who, "mark", cell=cell)
                assert not failed and "Not marked" not in text
        assert game.winner == "x" and text.endswith("x won.")
        async with player(base, "/players/carol/mcp") as carol:
            failed, text = await call(carol, "show_board")
            assert failed and "'carol' plays no slot in this game; its players are alice, bob" in text
        async with player(base, "/mcp") as root:
            failed, text = await call(root, "show_board")
            assert failed and "/players/<name>/mcp" in text


async def test_against_the_games_ai():
    game = TicTacToe()
    async with deployed(game) as env:
        card, base = env.environment_card, env.environment_url
        await client.invoke_extension(base, card, LOBBY, {"additional_settings": {"first": "o"}}, method="open")
        await client.invoke_extension(base, card, LOBBY, {"occupant": {"kind": "ai"},
                                                          "additional_settings": {"faction": "o"}}, method="fill")
        await client.invoke_extension(base, card, LOBBY, {"occupant": {"kind": "agent", "name": "alice"},
                                                          "additional_settings": {"faction": "x"}}, method="fill")
        await client.invoke_extension(base, card, LOBBY, {}, method="close")
        assert game.board[0] == "o"   # the AI moved first, as the game closed
        async with player(base, "/players/alice/mcp") as alice:
            text = (await call(alice, "mark", cell=4))[1]
        assert game.board.count("o") == 2 and game.board[4] == "x" and "x to play" in text


async def test_a_game_that_fails_to_create_leaves_the_lobby_open_for_another_close():
    game = TicTacToe()
    failures = ["the board would not load"]

    async def flaky(lobby):
        if failures:
            raise RuntimeError(failures.pop())
        return await TicTacToe.new_game(game, lobby)

    game.new_game = flaky
    game._hooks()["create_game"] = flaky
    async with deployed(game) as env, httpx.AsyncClient() as http:
        base = env.environment_url + "/agentenv/ext/lobby"
        for body in ({"occupant": {"kind": "agent", "name": "alice"}, "additional_settings": {"faction": "x"}},
                     {"occupant": {"kind": "ai"}, "additional_settings": {"faction": "o"}}):
            assert (await http.post(f"{base}/fill", json=body)).status_code == 200
        failed = await http.post(f"{base}/close", json={})
        assert failed.status_code == 500 and failed.json()["error"] == {
            "code": "lobby_failed", "message": "RuntimeError: the board would not load"}
        assert (await http.get(base)).json()["state"] == "open"
        assert (await http.post(f"{base}/close", json={})).json()["state"] == "closed"
