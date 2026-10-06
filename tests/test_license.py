"""Licenses: a game that needs a file, a key and an acceptance, given them in process, over HTTP, and by the
add_license step from agent-env's secret store."""

import base64
import os

import httpx
import pytest
from agent_env.config import reset_config
from agent_env.task_step.context import TaskStepContext
from agentenv_protocol import client, environment_card
from conftest import deployed
from tictactoe import TicTacToe

from agentenv_game import (
    LICENSE,
    AgentEnvGameEnv,
    LicenseItem,
    LicenseParts,
    LobbyError,
    Occupant,
    SlotRequest,
    create_game,
    install_license,
    license_needs,
)
from agentenv_game import steps as game_steps
from agentenv_game.steps import AddLicenseTaskStep

pytestmark = pytest.mark.anyio

BOARD = b"a licensed board"
NEEDS = [LicenseItem(name="board.lic", kind="file", group="ttt", max_bytes=64, description="from your purchase"),
         LicenseItem(name="serial", kind="key", group="ttt", pattern=r"TTT-[0-9]{4}"),
         LicenseItem(name="ttt-terms", kind="acceptance", terms_url="https://example.com/terms")]


@environment_card(name="tictactoe")
class Licensed(TicTacToe):
    """Tic-tac-toe that needs a license of three parts."""

    given: LicenseParts | None = None

    @license_needs
    def needs(self) -> list[LicenseItem]:
        have = self.given or LicenseParts()
        return [i for i in NEEDS if i.name not in {*have.files, *have.keys, *have.accepted}]

    @install_license
    def install(self, parts: LicenseParts) -> None:
        have = self.given or LicenseParts()
        self.given = LicenseParts(files={**have.files, **parts.files}, keys={**have.keys, **parts.keys},
                                  accepted=[*have.accepted, *parts.accepted])


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def seated(game) -> None:
    for name, mark in (("alice", "x"), ("bob", "o")):
        game.fill_slot(SlotRequest(occupant=Occupant(kind="agent", name=name),
                                   additional_settings={"faction": mark}))


def refused(code, call, *args, **kwargs):
    with pytest.raises(LobbyError) as e:
        call(*args, **kwargs)
    assert e.value.code == code, e.value
    return e.value.message


async def test_a_game_without_a_license_needs_nothing():
    game = TicTacToe()
    assert game.license_missing() == [] and game.license_status() == {"missing": [], "installed": []}
    seated(game)
    assert (await game.close_lobby())["state"] == "closed"


async def test_the_lobby_does_not_close_until_every_part_is_given():
    game = Licensed()
    seated(game)
    with pytest.raises(LobbyError) as e:
        await game.close_lobby()
    assert e.value.code == "not_licensed" and "file board.lic (from your purchase)" in e.value.message
    assert "acceptance ttt-terms (https://example.com/terms)" in e.value.message
    game.add_license(files={"board.lic": b64(BOARD)}, keys={"serial": "TTT-0042"}, accept=["ttt-terms"])
    assert game.given == LicenseParts(files={"board.lic": BOARD}, keys={"serial": "TTT-0042"}, accepted=["ttt-terms"])
    assert (await game.close_lobby())["state"] == "closed"


def test_each_part_is_checked_against_the_item_it_fills():
    game = Licensed()
    assert "doesn't need file other.lic" in refused("bad_license", game.add_license, files={"other.lic": b64(b"x")})
    assert "serial is a key, not a file" in refused("bad_license", game.add_license, files={"serial": b64(b"x")})
    assert "not base64" in refused("bad_license", game.add_license, files={"board.lic": "%%%"})
    assert "65 bytes, more than the 64" in refused("bad_license", game.add_license,
                                                    files={"board.lic": b64(b"x" * 65)})
    assert "not in its format" in refused("bad_license", game.add_license, keys={"serial": "1234"})
    assert game.given is None   # nothing refused was installed
    status = game.add_license(keys={"serial": "TTT-0001"})   # a part at a time
    assert [i["name"] for i in status["missing"]] == ["board.lic", "ttt-terms"] and status["installed"] == ["serial"]
    assert "doesn't need key serial" in refused("bad_license", game.add_license, keys={"serial": "TTT-0002"})


def test_a_game_marks_both_license_methods_or_neither():
    class Half(AgentEnvGameEnv):
        @create_game
        async def start(self, lobby):
            return {}

        @license_needs
        def needs(self):
            return []

    with pytest.raises(TypeError, match="only one of @license_needs and @install_license"):
        Half().create_app()


async def test_the_license_is_served_names_only():
    async with deployed(Licensed()) as env:
        card, base = env.environment_card, env.environment_url
        methods = client.extension_params(card, LICENSE)["methods"]
        assert {m: (v["method"], v["endpoint"]) for m, v in methods.items()} == {
            "get": ("GET", "/agentenv/ext/license"), "add": ("POST", "/agentenv/ext/license/add")}
        status = await client.invoke_extension(base, card, LICENSE, method="get")
        assert [i["kind"] for i in status["missing"]] == ["file", "key", "acceptance"]
        async with httpx.AsyncClient() as http:
            bad = await http.post(f"{base}/agentenv/ext/license/add", json={"keys": {"serial": "nope"}})
            assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_license"
            assert "nope" not in bad.text   # a key is never echoed
            unknown = await http.post(f"{base}/agentenv/ext/license/add", json={"license": {}})
            assert unknown.status_code == 400 and unknown.json()["error"]["code"] == "bad_request"
        given = await client.invoke_extension(base, card, LICENSE, {"keys": {"serial": "TTT-0007"}}, method="add")
        assert given["installed"] == ["serial"] and "TTT-0007" not in str(given)


@pytest.fixture
def secrets(monkeypatch, tmp_path):
    """agent-env on its default local secret store, which reads environment variables first."""
    for var in [v for v in os.environ if v.startswith("AGENT_ENV_")]:
        monkeypatch.delenv(var)
    (tmp_path / "config.toml").write_text("")
    monkeypatch.setenv("AGENT_ENV_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("TTT_BOARD", b64(BOARD))
    monkeypatch.setenv("TTT_SERIAL", "TTT-0042")
    reset_config()
    yield
    reset_config()


def step(**parts):
    return AddLicenseTaskStep(id="license", version=None, env_id="tictactoe", **parts)


async def test_add_license_gives_the_game_its_parts_from_the_secret_store(secrets):
    game = Licensed()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        await step(files={"board.lic": "TTT_BOARD"}, keys={"serial": "TTT_SERIAL"}, accept=["ttt-terms"]).execute(
            context)
    assert game.given == LicenseParts(files={"board.lic": BOARD}, keys={"serial": "TTT-0042"}, accepted=["ttt-terms"])
    assert context.metadata["game_license"] == {"installed": ["board.lic", "serial", "ttt-terms"]}


async def test_add_license_reads_no_secret_it_does_not_send(secrets, monkeypatch):
    reads = []

    class Store:
        def get(self, name):
            reads.append(name)
            return {"TTT_BOARD": b64(BOARD)}.get(name)

    class Config:
        def get_secret_store(self):
            return Store()

    monkeypatch.setattr(game_steps, "get_config", Config)
    async with deployed(Licensed()) as env:
        context = TaskStepContext(deployed_envs=[env])
        with pytest.raises(RuntimeError, match="lacks acceptance ttt-terms .*list terms you accept"):
            await step(files={"board.lic": "TTT_BOARD"}, keys={"serial": "TTT_SERIAL"}).execute(context)
        assert reads == []   # a part it can't give: it reads nothing
        with pytest.raises(RuntimeError, match="secret store has no 'TTT_SERIAL' \\(key serial\\)"):
            await step(files={"board.lic": "TTT_BOARD"}, keys={"serial": "TTT_SERIAL"},
                       accept=["ttt-terms"]).execute(context)
    async with deployed(TicTacToe()) as env:   # a game that lacks nothing
        reads.clear()
        context = TaskStepContext(deployed_envs=[env])
        await step(files={"board.lic": "TTT_BOARD"}).execute(context)
        assert reads == [] and context.metadata["game_license"] == {"installed": []}


def test_add_license_loads_from_task_json():
    loaded = AddLicenseTaskStep.from_dict({"id": "license", "type": "add_license", "env_id": "wc3",
                                           "files": {"roc.w3k": "WC3_ROC_W3K"}, "accept": ["terms"]})
    assert loaded.to_dict()["files"] == {"roc.w3k": "WC3_ROC_W3K"} and loaded.to_dict()["keys"] == {}
    with pytest.raises(ValueError, match="maps each part's name to the name of the secret"):
        step(files={"roc.w3k": ""})
    with pytest.raises(ValueError, match="accept is a list"):
        step(accept="terms")
