"""The broadcast without Docker or the network: the presentation a task gives, the docker run with its stream keys in
the environment only, start_broadcast and save_broadcast against a fake docker, and the streamer's side (what it
reads from the env, what it serves the overlay, when it ends, what it keeps out of its output)."""

import asyncio
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from agent_env.artifact import FileArtifact
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from agentenv_protocol import client
from conftest import deployed
from test_match import Race
from test_served import fill
from tictactoe import TicTacToe

from agentenv_game import LOBBY
from agentenv_game.broadcast import Presentation, SaveBroadcastTaskStep, StartBroadcastTaskStep, docker
from agentenv_game.broadcast.docker import BroadcastError
from agentenv_game.steps import CancelMatchTaskStep, CloseLobbyTaskStep, OpenLobbyTaskStep

sys.path.insert(0, str(docker.STREAMER))
import serve  # noqa: E402
import stream  # noqa: E402

STREAM_KEY, X_KEY = "live_123_secret", "x_456_secret"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def test_a_presentation_shows_the_score_bug_alone_by_default_and_places_what_a_task_adds():
    assert [w.widget for w in Presentation().layout()] == ["score_bug"]
    with_banners = Presentation(banners=[{"text": "Brought to you by {acme}", "logos": {"acme": "https://a/x.png"}}])
    assert [w.widget for w in with_banners.layout()] == ["score_bug", "banners"]
    custom = Presentation(overlay=[{"widget": "title_card"}, {"widget": "page", "url": "env:/panel", "at": "left",
                                                             "size": [400, 600]},
                                   {"widget": "page", "url": "https://x.example/p", "box": [0, 980, 1920, 100]}])
    assert [w.widget for w in custom.layout()] == ["title_card", "page", "page"]
    for overlay, why in (([{"widget": "page", "at": "top"}], "needs a url"),
                         ([{"widget": "page", "url": "file:///etc/passwd", "at": "top"}], "needs a url"),
                         ([{"widget": "page", "url": "env:/panel"}], "an anchor"),
                         ([{"widget": "score_bug", "url": "env:/x"}], "takes no url"),
                         ([{"widget": "score_bug", "box": [1800, 0, 200, 100]}], "within the 1920x1080 frame"),
                         ([{"widget": "score_bug", "at": "middle"}], "at"),
                         ([{"widget": "ticker"}], "widget")):
        with pytest.raises(ValueError, match=why):
            Presentation(overlay=overlay)
    for banner, why in (({"text": "by {acme}"}, "places logos it has no source for"),
                        ({"text": "by acme", "logos": {"acme": "https://a/x.png"}}, "never places"),
                        ({"text": "by {acme}", "logos": {"acme": "http://a/x.png"}}, "https:// URL")):
        with pytest.raises(ValueError, match=why):
            Presentation(banners=[banner])
    with pytest.raises(ValueError, match="1 to 30 characters"):
        Presentation(names={"x": " "})
    with pytest.raises(ValueError, match="accent"):
        Presentation(theme={"accent": "gold"})


def test_banner_logos_are_inlined_so_the_broadcast_never_waits_on_their_host(tmp_path):
    (tmp_path / "acme.png").write_bytes(PNG)
    (tmp_path / "notes.txt").write_text("not an image")
    (tmp_path / "huge.png").write_bytes(PNG + b"\0" * 600_000)
    shown = Presentation(banners=[{"text": "{acme} Acme", "logos": {"acme": str(tmp_path / "acme.png")}}]).inlined()
    assert shown.banners[0].logos["acme"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    for file, why in (("notes.txt", "not a PNG"), ("huge.png", "over 524,288 bytes"),
                      ("gone.png", "could not be read")):
        with pytest.raises(ValueError, match=why):
            Presentation(banners=[{"text": "{a}", "logos": {"a": str(tmp_path / file)}}]).inlined()
    with pytest.raises(ValueError, match="not a base64 data: URI"):
        Presentation(banners=[{"text": "{a}", "logos": {"a": "data:text/html;base64,PGI+"}}]).inlined()


def test_the_stream_keys_come_from_the_secrets_a_task_names_and_reach_the_container_only_in_its_environment(
        local_stores, monkeypatch):
    monkeypatch.setenv("TWITCH_STREAM_KEY", STREAM_KEY)
    monkeypatch.setenv("MY_X_SERVER", "rtmps://x.example:443/x/")
    monkeypatch.setenv("MY_X_KEY", X_KEY)
    urls, shown = docker.targets(["twitch", "x", "twitch"], {"x_server": "MY_X_SERVER", "x": "MY_X_KEY"})
    assert urls == [f"{docker.TWITCH}/{STREAM_KEY}", f"rtmps://x.example:443/x/{X_KEY}"]
    assert not any(STREAM_KEY in s or X_KEY in s for s in shown)
    assert docker.targets(["twitch"], {}, test=True)[0] == [f"{docker.TWITCH}/{STREAM_KEY}?bandwidthtest=true"]
    with pytest.raises(BroadcastError, match="OTHER_KEY"):
        docker.targets(["twitch"], {"twitch": "OTHER_KEY"})
    with pytest.raises(BroadcastError, match="X_STREAM_SERVER and X_STREAM_KEY"):
        docker.targets(["x"], {})
    run = docker.command("http://127.0.0.1:41589", "agentenv-game-streamer:abc", Path("/tmp/b"), "game-broadcast-1",
                         size="1280x720", fps=30, bitrate="3000k", linger=60, record=True)
    assert run[:3] == ["docker", "run", "-d"] and run[run.index("-e", run.index("HOME=/tmp")) + 1] == "STREAM_URL"
    assert run[run.index("--env") + 1] == (
        "http://host.docker.internal:41589" if sys.platform == "darwin" else "http://127.0.0.1:41589")
    assert run[-1] == "--record" and run[run.index("-v") + 1].endswith(":/broadcast")
    assert docker.image().startswith("agentenv-game-streamer:") and docker.image() == docker.image()


FAKE_DOCKER = """#!{python}
import json, os, pathlib, shutil, signal, sys, time
args = sys.argv[1:]
here = pathlib.Path(os.environ["FAKE_DOCKER_DIR"])
with (here / "calls.jsonl").open("a") as f:
    f.write(json.dumps(args) + "\\n")


def alive(name):
    try:
        os.kill(int((here / (name + ".pid")).read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


if args[0] == "stop":
    if alive(args[-1]):
        os.kill(int((here / (args[-1] + ".pid")).read_text()), signal.SIGTERM)
    while alive(args[-1]):
        time.sleep(0.05)
elif args[0] == "inspect":
    if not alive(args[-1]):
        sys.exit(1)   # gone: --rm removed it
    print("true")
elif args[0] == "run":
    if os.fork():
        time.sleep(0.3)   # detached: docker returns once the container runs
        sys.exit(0)
    (here / (args[args.index("--name") + 1] + ".pid")).write_text(str(os.getpid()))
    folder = pathlib.Path(next(a.split(":")[0] for a in args if a.endswith(":/broadcast")))
    shutil.copy(folder / "config.json", here / "config.json")
    (here / "stream_url").write_text(os.environ.get("STREAM_URL", ""))
    if "--record" in args:
        (folder / "stream-test.mkv").write_bytes(b"live")   # the streamer records as it goes

    def finish(*_):
        if "--record" in args:
            (folder / "stream-test.mp4").write_bytes(b"\\0\\0\\0\\x18ftypisom" + b"x" * 100)
            (folder / "stream-test.mkv").unlink()
        os._exit(0)

    signal.signal(signal.SIGTERM, finish)
    if os.environ.get("FAKE_STREAM") == "end":
        time.sleep(1)
        finish()
    if os.environ.get("FAKE_STREAM") == "die":   # killed: its live recording is all there is
        time.sleep(1)
        os._exit(137)
    while True:
        time.sleep(0.1)
"""


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` on PATH that records its calls, what the streamer would read (config.json, STREAM_URL), and has
    the streamer's image; a detached `run` records and exits after a second (FAKE_STREAM=end), dies (die), or
    streams until `docker stop`, as the streamer does; `inspect` says whether it still runs."""
    directory = tmp_path / "docker-bin"
    directory.mkdir()
    (directory / "docker").write_text(FAKE_DOCKER.format(python=sys.executable))
    (directory / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(directory))
    monkeypatch.setattr(SaveBroadcastTaskStep, "POLL_SECONDS", 0.2)
    calls = directory / "calls.jsonl"
    return directory, lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.is_file() else []


async def match_of(env, *players) -> TaskStepContext:
    """A run whose tic-tac-toe lobby has closed, with `players` (agent or ai) as x and o."""
    context = TaskStepContext(deployed_envs=[env], instance_id="run-1")
    await OpenLobbyTaskStep(id="lobby", version=None, env_id="tictactoe").execute(context)
    for mark, kind in zip(("x", "o"), players, strict=True):
        await fill(env, player_id=mark, player_kind=kind, **({"player_name": mark} if kind == "agent" else {}))
    await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
    return context


async def on_air(context, **options) -> TaskStepContext:
    return await StartBroadcastTaskStep(id="broadcast", version=None, env_id="tictactoe", **options).execute(context)


async def saved(context, **options) -> TaskStepContext:
    return await SaveBroadcastTaskStep(id="save", version=None, env_id="tictactoe", **options).execute(context)


@pytest.mark.anyio
async def test_a_broadcast_is_live_before_its_players_move_and_its_video_is_kept_once_it_ends(
        local_stores, fake_docker, monkeypatch):
    here, calls = fake_docker
    monkeypatch.setenv("FAKE_STREAM", "end")
    monkeypatch.setenv("TWITCH_STREAM_KEY", STREAM_KEY)
    game = TicTacToe()
    async with deployed(game) as env:
        context = await on_air(await match_of(env, "agent", "ai"), to=["twitch"], title="Noughts", names={"x": "Ada"})
        started = context.metadata["broadcasts"]["broadcast"]
        assert started["container"].startswith("game-broadcast-") and Path(started["folder"]).is_dir()
        assert game.board == [" "] * 9   # live before the first move
        context = await saved(context)
    [video] = context.metadata["broadcasts"]["broadcast"]["videos"]
    assert video["name"] == "stream-test.mp4" and video["artifact_id"].endswith("run-1-broadcast-stream-test.mp4")
    assert FileArtifact.get(video["artifact_id"], 1).load()[4:8] == b"ftyp" and not Path(started["folder"]).exists()
    run = next(c for c in calls() if c[0] == "run")
    assert not any(STREAM_KEY in a for c in calls() for a in c)
    assert (here / "stream_url").read_text() == f"{docker.TWITCH}/{STREAM_KEY}"
    assert run[run.index("--env") + 1] == (docker.from_container(env.environment_url) if sys.platform == "darwin"
                                           else env.environment_url)
    config = json.loads((here / "config.json").read_text())
    assert (config["title"], config["names"], config["layout"]) == ("Noughts", {"x": "Ada"}, [{"widget": "score_bug"}])
    assert "broadcast_errors" not in context.metadata


@pytest.mark.anyio
async def test_a_broadcast_ends_when_the_match_stands_still_or_the_save_is_cancelled(
        local_stores, fake_docker, monkeypatch):
    _, calls = fake_docker
    monkeypatch.setenv("FAKE_STREAM", "hang")
    async with deployed(TicTacToe()) as env:
        context = await match_of(env, "agent", "ai")
        context = await asyncio.wait_for(saved(await on_air(context), stall_seconds=1), 20)   # nobody plays
        assert [c[0] for c in calls() if c[0] not in ("inspect", "image")][-2:] == ["run", "stop"]
        assert context.metadata["broadcasts"]["broadcast"]["videos"][0]["name"] == "stream-test.mp4"
        running = asyncio.create_task(saved(await on_air(context)))
        await asyncio.sleep(1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert [c[0] for c in calls() if c[0] not in ("inspect", "image")][-2:] == ["run", "stop"]


@pytest.mark.anyio
async def test_a_broadcast_without_its_match_its_key_or_a_spectator_view_or_of_a_match_over_starts_nothing(
        local_stores, fake_docker, monkeypatch):
    _, calls = fake_docker
    monkeypatch.delenv("TWITCH_STREAM_KEY", raising=False)
    async with deployed(TicTacToe()) as env:
        noted = await on_air(TaskStepContext(deployed_envs=[env]))   # the run goes on, the error noted
        assert "has no match: put start_broadcast after close_lobby" in noted.metadata["broadcast_errors"]["broadcast"]
        context = await match_of(env, "agent", "ai")
        noted = await on_air(context, to=["twitch"])
        assert "TWITCH_STREAM_KEY" in noted.metadata["broadcast_errors"]["broadcast"]
        with pytest.raises(BroadcastError, match="TWITCH_STREAM_KEY"):
            await on_air(context, to=["twitch"], fail_task_on_error=True)
        await CancelMatchTaskStep(id="cancel", version=None, env_id="tictactoe").execute(context)
        context = await saved(await on_air(TaskStepContext(deployed_envs=[env])))
    assert context.metadata["broadcasts"]["broadcast"]["videos"] == [] and "broadcast_errors" not in context.metadata
    async with deployed(Race()) as env:
        context = TaskStepContext(deployed_envs=[env])
        await OpenLobbyTaskStep(id="lobby", version=None, env_id="tictactoe").execute(context)
        await fill(env, player_id="c", player_kind="ai")
        await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
        noted = await on_air(context)
    assert "has no spectator view (@spectator_card)" in noted.metadata["broadcast_errors"]["broadcast"]
    assert not any(c[0] == "run" for c in calls())
    for options, why in (({"record": False}, "goes nowhere"), ({"to": ["youtube"]}, "not \\['youtube'\\]"),
                         ({"key_secrets": {"kick": "K"}}, "not \\['kick'\\]"),
                         ({"overlay": [{"widget": "page"}]}, "needs a url")):
        with pytest.raises(ValueError, match=why):
            StartBroadcastTaskStep(id="b", version=None, env_id="tictactoe", **options)


@pytest.mark.anyio
async def test_a_streamer_that_dies_leaves_what_it_recorded(local_stores, fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_STREAM", "die")
    async with deployed(TicTacToe()) as env:
        context = await saved(await on_air(await match_of(env, "agent", "ai")))
    [video] = context.metadata["broadcasts"]["broadcast"]["videos"]
    assert video["name"] == "stream-test.mkv" and "broadcast_errors" not in context.metadata


def test_the_steps_load_from_task_json():
    registry = get_task_step_registry()
    step = registry["start_broadcast"].from_dict({
        "id": "broadcast", "type": "start_broadcast", "env_id": "tictactoe", "to": ["twitch"], "title": "Final",
        "banners": [{"text": "{a} A", "logos": {"a": "https://a/x.png"}}], "depends_on": ["close"]})
    assert {k: step.to_dict()[k] for k in ("to", "title", "record", "fail_task_on_error")} == {
        "to": ["twitch"], "title": "Final", "record": True, "fail_task_on_error": False}
    assert registry["start_broadcast"].from_dict(step.to_dict()).to_dict() == step.to_dict()
    save = registry["save_broadcast"].from_dict({"id": "save", "type": "save_broadcast", "env_id": "tictactoe"})
    assert (save.to_dict()["broadcast"], save.to_dict()["stall_seconds"]) == ("broadcast", 900)


@pytest.mark.anyio
async def test_the_streamer_reads_the_lobby_the_match_and_its_spectator_view_and_serves_them_to_the_overlay():
    async with deployed(TicTacToe()) as env:
        watch = serve.Watch(env.environment_url + "/", {"title": "Noughts", "layout": [{"widget": "score_bug"}]})
        assert await asyncio.to_thread(watch.poll)
        before = watch.snapshot()
        assert before["lobby"]["status"] == "not_opened" and before["match"] is None and before["view"] is None
        await client_open(env)
        assert await asyncio.to_thread(watch.poll)
        state = watch.snapshot()
        assert state["match"]["status"] == "started" and state["lobby"]["status"] == "closed"
        assert state["view"] == env.environment_url + "/spectators/spectate"
        assert state["env"] == env.environment_url and state["broadcast"]["title"] == "Noughts"
        server = serve.serve(watch, stream.free_port())
        try:
            page = f"http://127.0.0.1:{server.server_port}"
            got = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(page + "/state.json")))
            assert got["view"] == state["view"]
            index = await asyncio.to_thread(lambda: urllib.request.urlopen(page + "/").read().decode())
            assert '<script src="overlay.js">' in index
            for path in ("/../serve.py", "/nothing.js"):
                with pytest.raises(urllib.error.HTTPError, match="404"):
                    await asyncio.to_thread(urllib.request.urlopen, page + path)
        finally:
            server.shutdown()
    assert not await asyncio.to_thread(watch.poll)   # the env is gone


async def client_open(env) -> None:
    for method, body in (("open", {}), ("fill", {"player_id": "x", "player_kind": "agent", "player_name": "x"}),
                         ("fill", {"player_id": "o", "player_kind": "ai"}), ("close", {})):
        await client.invoke_extension(env.environment_url, env.environment_card, LOBBY, body, method=method)


class Watched:
    """What the streamer last read of the env, as the test sets it, on the test's clock."""

    def __init__(self, clock):
        self.clock, self.state, self.answered = clock, {"match": {"lobby_id": "lb-1", "status": "started"}}, clock[0]

    def snapshot(self):
        return self.state


def test_the_streamer_ends_linger_seconds_after_the_match_is_over_or_replaced(capsys):
    clock = [1000.0]
    watch = Watched(clock)

    def sleep(seconds):
        clock[0] += seconds
        watch.answered = clock[0]
        if clock[0] == 1020:
            watch.state = {"match": {"lobby_id": "lb-1", "status": "finished"}}

    assert stream.follow(watch, 30, lambda: True, now=lambda: clock[0], sleep=sleep)
    assert clock[0] == 1050
    assert "The match is finished; the overlay stays on for 30 s" in capsys.readouterr().out
    assert stream.ended({"match": {"lobby_id": "lb-2", "status": "started"}}, "lb-1") == "replaced by a new lobby's"
    assert stream.ended({"match": None}, "lb-1") is None and stream.ended({"match": None}, None) is None


def test_the_streamer_ends_a_minute_after_the_env_stops_answering_and_with_its_encoder(capsys):
    clock = [1000.0]
    watch = Watched(clock)

    def sleep(seconds):
        clock[0] += seconds
        if clock[0] < 1100:
            watch.answered = clock[0]

    assert not stream.follow(watch, 30, lambda: True, now=lambda: clock[0], sleep=sleep)
    assert clock[0] == 1155   # last answered at 1095
    assert "The env has not answered for 60 s; ending the stream" in capsys.readouterr().out
    assert not stream.follow(watch, 30, lambda: False, sleep=lambda s: pytest.fail("polled after the encoder ended"))


def test_the_streamer_encodes_once_for_one_leg_several_legs_or_only_the_recording():
    one = stream.ffmpeg_command(":3", "1920x1080", 30, "4500k", ["rtmp://t/app/key"], None)
    assert one[-3:] == ["-f", "flv", "rtmp://t/app/key"]
    assert one[one.index("x11grab") - 1:one.index("x11grab") + 9] == [
        "-f", "x11grab", "-video_size", "1920x1080", "-framerate", "30", "-draw_mouse", "0", "-i", ":3"]
    assert one[one.index("-bufsize"):one.index("-bufsize") + 4] == ["-bufsize", "9000k", "-g", "60"]
    legs = stream.ffmpeg_command(":3", "1280x720", 30, "3000k", ["rtmp://t/app/a", "rtmps://x:443/x/b"],
                                 Path("/broadcast/stream.mkv"))
    assert legs[-5:] == ["-flags", "+global_header", "-f", "tee",
                         "[f=flv:onfail=ignore]rtmp://t/app/a|[f=flv:onfail=ignore]rtmps://x:443/x/b"
                         "|[f=matroska]/broadcast/stream.mkv"]
    assert stream.ffmpeg_command(":3", "1280x720", 30, "3000k", [], Path("/b/s.mkv"))[-3:] == [
        "-f", "matroska", "/b/s.mkv"]


def test_the_streamer_relays_each_line_as_it_comes_without_the_stream_keys(capsys):
    chunks = [b"[flv @ 0x1] rtmp://t/app/liv", b"e_9: Broken pipe\nframe=  900 fps= 30 sp", b"eed=   1x    \r", b""]
    printed = []

    class Pipe:
        def read1(self, size):
            printed.append(capsys.readouterr().out)
            return chunks.pop(0)

    stream.relay(Pipe(), stream.secrets_of(["rtmp://t/app/live_9?bandwidthtest=true"]))
    printed.append(capsys.readouterr().out)
    assert printed == ["", "", "[flv @ 0x1] rtmp://t/app/<stream key>: Broken pipe\n",
                       "frame=  900 fps= 30 speed=   1x\n", ""]
