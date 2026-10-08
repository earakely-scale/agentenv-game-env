"""The broadcast's task steps. `start_broadcast` streams a game env's match, its spectator view under the overlay, to
Twitch or X, or only records it, and returns once the stream is live, so the match's players start on air;
`save_broadcast` waits for it to end and keeps its video as a file artifact. Both read only the match protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import ClassVar

import httpx
from agent_env.artifact import FileArtifact
from agent_env.entity_refs import EntityRef
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from pydantic import ValidationError

from ..match import FINAL, MATCH
from ..steps import _deployed, _invoke
from . import docker
from .docker import DESTINATIONS, KEY_SECRETS, BroadcastError
from .settings import Presentation

log = logging.getLogger(__name__)
FAILURES = (RuntimeError, OSError, ValueError, httpx.HTTPError)   # BroadcastError is a RuntimeError


class StartBroadcastTaskStep(TaskStep):
    """Start broadcasting a game env's match, and return once the stream is live: put it after close_lobby, and the
    match's prompt_agent steps after it (depends_on), so the broadcast has the match from its first move.
    save_broadcast, after finish_match, keeps its video. The stream is the game's spectator view (its
    `@spectator_card`) under the overlay: `title`, `names` (a player slot's display name by player_id), `theme`,
    `banners` and the `overlay`'s widgets (see `agentenv_game.broadcast.settings`). It goes `to` twitch or x, with
    the stream keys in agent-env's secret store (`key_secrets` names them), or, with no `to`, is only recorded
    (`record`, the default, keeps the video). It ends `linger_seconds` after the match is over, or once the env has
    been gone a minute, so a run that stops early leaves no stream up. A broadcast that fails to start is noted in
    the run's `metadata["broadcast_errors"]` and the run goes on, unless `fail_task_on_error`."""

    type: ClassVar[str] = "start_broadcast"
    entity_refs = (EntityRef.env("env_id"),)
    OPTIONS = ("to", "key_secrets", "record", "title", "names", "theme", "banners", "overlay", "size", "fps",
               "bitrate", "linger_seconds", "test")
    LIVE_SECONDS = 120.0
    """The longest it waits for the stream to come up."""

    def __init__(self, id: str, version: int | None, env_id: str, to: list[str] | None = None,
                 key_secrets: dict | None = None, record: bool = True, title: str | None = None,
                 names: dict | None = None, theme: dict | None = None, banners: list | None = None,
                 overlay: list | None = None, size: str = "1920x1080", fps: int = 30, bitrate: str = "4500k",
                 linger_seconds: int = 60, test: bool = False, depends_on: list | None = None,
                 fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.to, self.key_secrets, self.record = env_id, list(to or []), dict(key_secrets or {}), record
        self.title, self.names, self.theme, self.banners, self.overlay = title, names, theme, banners, overlay
        self.size, self.fps, self.bitrate, self.linger_seconds, self.test = size, fps, bitrate, linger_seconds, test
        if unknown := sorted(set(self.to) - set(DESTINATIONS)):
            raise ValueError(f"start_broadcast goes to {' or '.join(DESTINATIONS)}, not {unknown}")
        if unknown := sorted(set(self.key_secrets) - set(KEY_SECRETS)):
            raise ValueError(f"start_broadcast key_secrets names the secrets of {', '.join(KEY_SECRETS)}, not "
                             f"{unknown}")
        if not self.to and not record:
            raise ValueError("start_broadcast goes nowhere: give `to` (twitch, x) or keep `record`")
        try:
            self.presentation = Presentation(**{k: v for k, v in (("title", title), ("names", names),
                                                                  ("theme", theme), ("banners", banners),
                                                                  ("overlay", overlay)) if v is not None})
        except ValidationError as e:
            raise ValueError(f"start_broadcast: {e}") from e

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, **{k: getattr(self, k) for k in self.OPTIONS},
                "fail_task_on_error": self.fail_task_on_error}

    @classmethod
    def from_dict(cls, data: dict) -> StartBroadcastTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", False)},
                   env_id=data["env_id"], **{k: data[k] for k in cls.OPTIONS if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        broadcasts = context.metadata.setdefault("broadcasts", {})
        broadcasts[self.id] = {"container": None, "folder": None, "record": self.record,
                               "linger_seconds": self.linger_seconds, "videos": []}
        try:
            match = await _match(deployed)
            if match is None:
                raise BroadcastError(f"env {self.env_id!r} has no match: put start_broadcast after close_lobby")
            if match["status"] in FINAL:
                log.warning("start_broadcast: the match of %s is already %s: nothing to broadcast", self.env_id,
                            match["status"])
                return context
            if not match.get("spectator_url"):
                raise BroadcastError(f"env {self.env_id!r}'s game has no spectator view (@spectator_card) to "
                                     "broadcast")
            broadcasts[self.id].update(await self._start(deployed.environment_url))
        except FAILURES as e:
            if self.fail_task_on_error:
                raise
            log.error("start_broadcast: %s", e)
            context.metadata.setdefault("broadcast_errors", {})[self.id] = str(e)
        return context

    async def _start(self, env_url: str) -> dict:
        """Starts the streamer, detached, and returns once it is live: its recording has begun (or, streaming only,
        20 s on), or LIVE_SECONDS have passed."""
        urls, shown = await asyncio.to_thread(docker.targets, self.to, self.key_secrets, self.test)
        presentation = await asyncio.to_thread(self.presentation.inlined)
        tag = docker.image()
        if not await asyncio.to_thread(docker.exists, tag):
            log.info("start_broadcast: building %s (a few minutes the first time)", tag)
            await asyncio.to_thread(docker.build, tag)
        folder = Path(tempfile.mkdtemp(prefix="game-broadcast-"))
        layout = [w.model_dump(mode="json", exclude_none=True) for w in presentation.layout()]
        (folder / "config.json").write_text(json.dumps(
            {**presentation.model_dump(mode="json", exclude={"overlay"}), "layout": layout}))
        name = f"game-broadcast-{uuid.uuid4().hex[:12]}"
        command = docker.command(env_url, tag, folder, name, size=self.size, fps=self.fps, bitrate=self.bitrate,
                                 linger=self.linger_seconds, record=self.record)
        proc = await asyncio.create_subprocess_exec(*command, env={**os.environ, "STREAM_URL": "\n".join(urls)},
                                                    stdout=asyncio.subprocess.DEVNULL)
        if await proc.wait():
            shutil.rmtree(folder, ignore_errors=True)
            raise BroadcastError(f"the streamer did not start (docker run exited {proc.returncode})")
        log.info("start_broadcast: %s %s", env_url, " and ".join([*shown, *(["recorded"] if self.record else [])]))
        began = time.monotonic()
        while time.monotonic() - began < self.LIVE_SECONDS and await docker.running(name):
            if self.record and any(p.stat().st_size for p in folder.glob("*.mkv")):
                break
            if not self.record and time.monotonic() - began > 20:
                break
            await asyncio.sleep(1)
        log.info("start_broadcast: live after %.0f s; the match may start", time.monotonic() - began)
        return {"container": name, "folder": str(folder)}


class SaveBroadcastTaskStep(TaskStep):
    """Keep a broadcast's video: put it after finish_match. It waits for the streamer that start_broadcast
    `broadcast` (that step's id) started to end, `linger_seconds` after the match is over, stopping it if the match's
    clock stands still for `stall_seconds` (a run whose play failed, so its match never ends) or the match has been
    over a while, then keeps the video as a file artifact (what a streamer that died had recorded too), in the run's
    `metadata["broadcasts"]`. If it fails, the error is noted in `metadata["broadcast_errors"]` and the run goes on,
    unless `fail_task_on_error`."""

    type: ClassVar[str] = "save_broadcast"
    entity_refs = (EntityRef.env("env_id"),)
    POLL_SECONDS = 10.0

    def __init__(self, id: str, version: int | None, env_id: str, broadcast: str = "broadcast",
                 stall_seconds: int = 900, depends_on: list | None = None, fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.broadcast, self.stall_seconds = env_id, broadcast, stall_seconds

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "broadcast": self.broadcast,
                "stall_seconds": self.stall_seconds, "fail_task_on_error": self.fail_task_on_error}

    @classmethod
    def from_dict(cls, data: dict) -> SaveBroadcastTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", False)},
                   env_id=data["env_id"], **{k: data[k] for k in ("broadcast", "stall_seconds") if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        started = (context.metadata.get("broadcasts") or {}).get(self.broadcast)
        if started is None:
            raise RuntimeError(f"save_broadcast: no start_broadcast step {self.broadcast!r} ran before it")
        try:
            if started["container"]:
                await self._until_ended(deployed, started)
            if started["folder"]:
                started["videos"] = await self._store(context, Path(started["folder"]))
                if started["record"] and not started["videos"]:
                    raise BroadcastError("the broadcast recorded nothing")
        except FAILURES as e:
            if self.fail_task_on_error:
                raise
            log.error("save_broadcast: %s", e)
            context.metadata.setdefault("broadcast_errors", {})[self.broadcast] = str(e)
        return context

    async def _until_ended(self, deployed: DeployedEnv, started: dict) -> None:
        """Waits for the streamer to end, ending it if the match's clock stands still for stall_seconds, or the
        match has been over longer than the streamer lingers (or the step is cancelled)."""
        name, clock, moved, over = started["container"], None, time.monotonic(), None
        try:
            while await docker.running(name):
                try:
                    match = await _match(deployed)
                except (RuntimeError, httpx.HTTPError):
                    match = None   # the env is going: the streamer ends itself a minute after it has gone
                now = time.monotonic()
                if match is None or match["status"] in FINAL:
                    over = over or now
                    stop = now - over > started["linger_seconds"] + 120
                elif match["status"] == "paused" or (value := (match.get("progress") or [{}])[0].get("value")) != clock:
                    clock, moved, stop = None if match["status"] == "paused" else value, now, False
                else:
                    stop = now - moved > self.stall_seconds
                    if stop:
                        log.warning("save_broadcast: the match's clock has stood still for %d s: ending the "
                                    "broadcast", self.stall_seconds)
                if stop:
                    await docker.stop(name)
                    return
                await asyncio.sleep(self.POLL_SECONDS)
        except asyncio.CancelledError:
            await docker.stop(name)
            raise

    async def _store(self, context: TaskStepContext, folder: Path) -> list[dict]:
        stem = f"{context.instance_id or uuid.uuid4().hex}-{self.broadcast}"
        saved = []
        for path in sorted(folder.glob("*.mp4")) or sorted(folder.glob("*.mkv")):   # an mkv: cut off
            artifact = await asyncio.to_thread(FileArtifact.put, f"{stem}-{path.name}",
                                               description=f"Broadcast of env {self.env_id!r}", file_path=str(path))
            saved.append({"name": path.name, "artifact_id": artifact.id, "version": artifact.version,
                          "bytes": path.stat().st_size})
            log.info("save_broadcast: %s (%d bytes) is file artifact %s v%d", path.name, path.stat().st_size,
                     artifact.id, artifact.version)
        shutil.rmtree(folder, ignore_errors=True)
        return saved


async def _match(deployed: DeployedEnv) -> dict | None:
    """The env's match; None until its lobby closes."""
    return await _invoke(deployed, MATCH, "get", {}, 60)
