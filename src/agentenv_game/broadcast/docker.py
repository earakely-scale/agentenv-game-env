"""Running the streamer image (streamer/): where a broadcast goes, from the secrets the task names, and the `docker
run` that streams an env's spectator view with the overlay. Stream keys come from agent-env's secret store, else the
environment, and reach the container in its environment, never on a command line."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

from agent_env.config import get_config

STREAMER = Path(__file__).with_name("streamer")
IMAGE = "agentenv-game-streamer"
TWITCH = "rtmp://live.twitch.tv/app"
DESTINATIONS = ("twitch", "x")
KEY_SECRETS = {"twitch": "TWITCH_STREAM_KEY", "x_server": "X_STREAM_SERVER", "x": "X_STREAM_KEY"}
"""The secrets that hold a destination's stream key (and X's server), by default; a task names its own."""


class BroadcastError(RuntimeError):
    pass


def secret(name: str) -> str | None:
    """A secret from agent-env's secret store, else the environment."""
    return get_config().get_secret_store().get(name) or os.environ.get(name)


def targets(to: list[str], key_secrets: dict[str, str], test: bool = False) -> tuple[list[str], list[str]]:
    """The RTMP URLs of `to`, with their keys, and how to name each without its key."""
    names = {**KEY_SECRETS, **key_secrets}
    urls, shown = [], []
    for where in dict.fromkeys(to):
        if where == "twitch":
            if not (key := secret(names["twitch"])):
                raise BroadcastError(f"no stream key: store your Twitch stream key as the secret {names['twitch']} in "
                                     f"agent-env's secret store, or export it; or only record")
            urls.append(f"{TWITCH}/{key}" + ("?bandwidthtest=true" if test else ""))
            shown.append(f"{TWITCH}/<stream key>" + (" as a bandwidth test (not live)" if test else ""))
        else:
            server, key = secret(names["x_server"]), secret(names["x"])
            if not server or not key:
                raise BroadcastError(f"no X stream: create a source in X's Live Studio and store its server URL and "
                                     f"stream key as the secrets {names['x_server']} and {names['x']}")
            urls.append(f"{server.rstrip('/')}/{key}")
            shown.append(f"{server.rstrip('/')}/<stream key> (press Go Live in X's Live Studio once it starts)")
    return urls, shown


def command(env_url: str, tag: str, folder: Path, name: str, *, size: str, fps: int, bitrate: str, linger: int,
            record: bool) -> list[str]:
    """The detached `docker run` of the streamer for the env at `env_url`: `folder` holds the presentation
    (config.json) and gets the recording. STREAM_URL is passed by name, so its value (the keys) stays off the command
    line."""
    network, env = ([], from_container(env_url)) if sys.platform == "darwin" else (["--network", "host"], env_url)
    return ["docker", "run", "-d", "--rm", "--name", name, "--shm-size", "1g", *network,
            "-v", f"{folder.resolve()}:/broadcast", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
            "-e", "STREAM_URL", tag, "--env", env, "--folder", "/broadcast", "--size", size, "--fps", str(fps),
            "--bitrate", bitrate, "--linger", str(linger), *(["--record"] if record else [])]


def image() -> str:
    """The streamer image's tag: a digest of every file under streamer/, so a changed streamer builds a new image
    instead of running an older one."""
    digest = hashlib.sha256()
    for path in sorted(p for p in STREAMER.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(STREAMER)).encode() + b"\0" + path.read_bytes())
    return f"{IMAGE}:{digest.hexdigest()[:12]}"


def exists(tag: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0


def build(tag: str) -> None:
    """Builds the streamer image (about 1.5 GB, a few minutes)."""
    built = subprocess.run(["docker", "build", "-t", tag, str(STREAMER)], capture_output=True, text=True)
    if built.returncode:
        raise BroadcastError(f"docker build of the streamer failed: {built.stderr[-2000:]}")


async def running(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec("docker", "inspect", "-f", "{{.State.Running}}", name,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return proc.returncode == 0 and out.strip() == b"true"


async def stop(name: str) -> None:
    proc = await asyncio.create_subprocess_exec("docker", "stop", "-t", "60", name, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


def from_container(url: str) -> str:
    """`url` as a Docker Desktop container reaches it: this machine's loopback is host.docker.internal there."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname not in ("127.0.0.1", "localhost"):
        return url
    return parts._replace(netloc=parts.netloc.replace(parts.hostname, "host.docker.internal", 1)).geturl()
