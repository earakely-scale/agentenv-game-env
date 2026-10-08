"""A game env's task steps: `add_license` gives the game the license it lacks, from agent-env's secret store;
`open_lobby` opens its lobby with the game's settings, `add_player_slot` fills one player slot and gives an agent its
slot's MCP address, read from the slot's env card, and `close_lobby` closes the lobby, which creates the game and its
match; `finish_match` plays the match out once its agents have stopped, and `cancel_match` ends it where it stands."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import uuid
from functools import partial
from pathlib import Path
from typing import ClassVar

import httpx
from agent_env.a2a_agent.a2a_agent import A2AAgent
from agent_env.artifact import FileArtifact
from agent_env.config import get_config
from agent_env.entity_refs import EntityRef
from agent_env.env.env import DeployedEnv, DeployedSandboxEnv
from agent_env.providers.sandbox_providers.sandbox_provider import reachable_url, sandbox_request_headers_for_url
from agent_env.task_step.context import DeployedAgent, TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agentenv_protocol import client
from agentenv_protocol.types import MCP_PATH, MCP_TRANSPORT, WELL_KNOWN_PATH
from pydantic import ValidationError

from .license import LICENSE, LicenseItem, LicenseKind
from .lobby import LOBBY, PlayerKind, PlayerSlot, PlayerSlotLimits, SlotRequest
from .match import MATCH

PAGE = "http"
"""A player slot's interface for a person: a page to open."""

log = logging.getLogger(__name__)


class AddLicenseTaskStep(TaskStep):
    """Give a deployed game env the license it lacks (`urn:game:license/v1`), from agent-env's secret store. `files`
    and `keys` map each part's name to the secret holding it: a file's secret holds its base64, a key's the key
    itself. `accept` lists the terms the task agrees to. The step asks the env what it lacks first, then reads only
    those secrets, and none when it lacks nothing. A lobby doesn't close while its game lacks its license, so put this
    before close_lobby. The parts' names go in the run's `metadata["game_license"]`; their contents go nowhere else."""

    type: ClassVar[str] = "add_license"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, files: dict | None = None,
                 keys: dict | None = None, accept: list | None = None, timeout_seconds: int = 60,
                 depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        for field, mapping in (("files", files), ("keys", keys)):
            if mapping is not None and not (isinstance(mapping, dict) and all(
                    isinstance(k, str) and isinstance(v, str) and v for k, v in mapping.items())):
                raise ValueError(f"add_license {field} maps each part's name to the name of the secret holding it")
        if accept is not None and not (isinstance(accept, list) and all(isinstance(a, str) and a for a in accept)):
            raise ValueError("add_license accept is a list of the terms' names")
        self.env_id, self.timeout_seconds = env_id, timeout_seconds
        self.files, self.keys, self.accept = dict(files or {}), dict(keys or {}), list(accept or [])

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "files": self.files, "keys": self.keys,
                "accept": self.accept, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> AddLicenseTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"],
                   **{k: data[k] for k in ("files", "keys", "accept", "timeout_seconds") if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        status = await _invoke(deployed, LICENSE, "get", {}, self.timeout_seconds)
        missing = [LicenseItem(**i) for i in status["missing"]]
        sources = {LicenseKind.FILE: self.files, LicenseKind.KEY: self.keys}
        if unmapped := [i for i in missing if (i.name not in self.accept if i.kind is LicenseKind.ACCEPTANCE
                                               else i.name not in sources[i.kind])]:
            raise RuntimeError(f"env {self.env_id!r} lacks {', '.join(i.label() for i in unmapped)}: map each file "
                               "or key to the secret holding it (files, keys), and list terms you accept (accept)")
        given: dict = {"files": {}, "keys": {}, "accept": [i.name for i in missing
                                                         if i.kind is LicenseKind.ACCEPTANCE]}
        store = get_config().get_secret_store()
        for item in (i for i in missing if i.kind is not LicenseKind.ACCEPTANCE):
            secret = sources[item.kind][item.name]
            value = await asyncio.to_thread(store.get, secret)
            if not value:
                raise RuntimeError(f"agent-env's secret store has no {secret!r} ({item.label()})")
            given["files" if item.kind is LicenseKind.FILE else "keys"][item.name] = value
        if missing:
            status = await _invoke(deployed, LICENSE, "add", given, self.timeout_seconds)
            if status["missing"]:
                raise RuntimeError(f"env {self.env_id!r} still lacks "
                                   f"{', '.join(LicenseItem(**i).label() for i in status['missing'])}")
        context.metadata["game_license"] = {"installed": status["installed"]}
        log.info("add_license: %s has its license (%s)", self.env_id,
                 ", ".join(i.name for i in missing) + " given" if missing else "it lacked nothing")
        return context


class OpenLobbyTaskStep(TaskStep):
    """Open a deployed game env's lobby for a match: `game_settings` are the game's own (WC3: map, seed,
    time_limit_seconds, mode), as its card's open request describes them, and `player_slot_limits` optionally narrow
    its player slots ({min, max}). The env fills in its defaults and refuses what it doesn't take. A run's step
    overrides merge into `game_settings` and replace `player_slot_limits`. The opened lobby, with its `lobby_id`, is
    kept in the run's `metadata["game_lobby"]`."""

    type: ClassVar[str] = "open_lobby"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, game_settings: dict | None = None,
                 player_slot_limits: dict | None = None, timeout_seconds: int = 120, depends_on: list | None = None,
                 fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        if game_settings is not None and not isinstance(game_settings, dict):
            raise ValueError("open_lobby game_settings is an object")
        try:
            if player_slot_limits is not None:
                PlayerSlotLimits(**player_slot_limits)
        except (TypeError, ValidationError) as e:
            raise ValueError(f"open_lobby player_slot_limits: {e}") from e
        self.env_id, self.timeout_seconds = env_id, timeout_seconds
        self.game_settings = dict(game_settings or {})
        self.player_slot_limits = dict(player_slot_limits) if player_slot_limits is not None else None

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "game_settings": self.game_settings,
                "player_slot_limits": self.player_slot_limits, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> OpenLobbyTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"], **{k: data[k] for k in ("game_settings", "player_slot_limits",
                                                                  "timeout_seconds") if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        overrides = self.step_param_overrides(context)
        request = {"game_settings": {**self.game_settings, **(overrides.get("game_settings") or {})},
                   "player_slot_limits": overrides.get("player_slot_limits", self.player_slot_limits)}
        lobby = await _invoke(deployed, LOBBY, "open", {k: v for k, v in request.items() if v is not None},
                              self.timeout_seconds)
        context.metadata["game_lobby"] = lobby
        limits = lobby.get("player_slot_limits") or {}
        log.info("open_lobby: %s opened lobby %s for %s to %s players (%s) with %s", self.env_id,
                 lobby.get("lobby_id"), limits.get("min", 0), limits.get("max", "any number of"),
                 ", ".join(limits.get("player_kinds") or ()), lobby.get("game_settings"))
        return context


class AddPlayerSlotTaskStep(TaskStep):
    """Fill one player slot of a deployed game env's lobby: `player_id` is the game's name for it (WC3: its player
    number, "0" to "11"), `player_kind` who plays it (agent, human or ai), `player_name` who that is, and
    `game_settings` the game's settings for it (WC3: faction, team, ai_level). The step reads the player slot's env
    card. An agent's slot is registered with the deploy_agent agent `player_name`, each of the card's MCP interfaces,
    so its calls play this slot (deploy it with `"env_ids": []`); `register: false` only reserves the slot, for a player
    that connects on its own. A human's page is logged. Each player slot is kept in the run's
    `metadata["game_slots"]`, by its player_id."""

    type: ClassVar[str] = "add_player_slot"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, player_id: str, player_kind: str,
                 player_name: str | None = None, game_settings: dict | None = None, register: bool = True,
                 timeout_seconds: int = 60, depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        try:
            self.slot = SlotRequest(player_id=player_id, player_kind=player_kind, player_name=player_name,
                                    game_settings=game_settings or {})
        except ValidationError as e:
            raise ValueError(f"add_player_slot: {e}") from e
        if self.slot.player_kind is PlayerKind.AGENT and register and player_name is None:
            raise ValueError("add_player_slot: an agent's player slot names its agent, player_name (register: false "
                             "reserves one for a player that connects on its own)")
        self.env_id, self.register, self.timeout_seconds = env_id, register, timeout_seconds

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, **self.slot.model_dump(mode="json", exclude_none=True),
                "register": self.register, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> AddPlayerSlotTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"], player_id=data["player_id"], player_kind=data["player_kind"],
                   **{k: data[k] for k in ("player_name", "game_settings", "register", "timeout_seconds")
                      if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        name = self.slot.player_name
        if self.slot.player_kind is PlayerKind.AGENT and self.register:
            _agent(context, name)   # before the slot is taken, so a missing agent leaves no slot
        lobby_id = (context.metadata.get("game_lobby") or {}).get("lobby_id")
        request = {**self.slot.model_dump(mode="json", exclude_none=True),
                   **({"lobby_id": lobby_id} if lobby_id else {})}
        slot = PlayerSlot(**await _invoke(deployed, LOBBY, "fill", request, self.timeout_seconds))
        record = slot.model_dump(mode="json", exclude_none=True)
        if slot.environment_url is not None:
            base = deployed.mcp_url.removesuffix(MCP_PATH) + slot.environment_url
            card = await _slot_card(base, slot.headers, self.timeout_seconds)
            interfaces = card.get("additionalInterfaces") or []
            record["interfaces"] = [{**i, "url": base + i["url"]} for i in interfaces]
            mcp = [slot.environment_url + i["url"] for i in interfaces if i.get("transport") == MCP_TRANSPORT]
            if slot.player_kind is PlayerKind.AGENT and self.register:
                if not mcp:
                    raise RuntimeError(f"player slot {slot.player_id!r}'s env card has no MCP interface for agent "
                                       f"{name!r}")
                record["registered"] = [await register_mcp(context, deployed, name, path, slot.headers)
                                        for path in mcp]
                log.info("add_player_slot: %s plays player slot %s at %s", name, slot.player_id,
                         ", ".join(record["registered"]))
            for page in (i["url"] for i in record["interfaces"] if i.get("transport") == PAGE):
                log.warning("PLAY player slot %s (%s): %s", slot.player_id, name, page)
        else:
            log.info("add_player_slot: the game's AI plays player slot %s", slot.player_id)
        context.metadata.setdefault("game_slots", {})[slot.player_id] = record
        return context


class CloseLobbyTaskStep(TaskStep):
    """Close a deployed game env's lobby, which creates the game and its match from its player slots. Put it after
    every add_player_slot step of the game and before its players play; closing a closed lobby returns it again. The
    closed lobby is kept in the run's `metadata["game_lobby"]`."""

    type: ClassVar[str] = "close_lobby"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, timeout_seconds: int = 900,
                 depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.timeout_seconds = env_id, timeout_seconds

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> CloseLobbyTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"], **({"timeout_seconds": data["timeout_seconds"]}
                                             if "timeout_seconds" in data else {}))

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        lobby_id = (context.metadata.get("game_lobby") or {}).get("lobby_id")
        result = await _invoke(deployed, LOBBY, "close", {"lobby_id": lobby_id} if lobby_id else {},
                               self.timeout_seconds)
        context.metadata["game_lobby"] = result
        log.info("close_lobby: %s created its game with %d players", self.env_id,
                 len(result.get("player_slots") or ()))
        return context


class _EndMatchTaskStep(TaskStep):
    """End a deployed game env's match with the match protocol's `method`; the final match is kept in the run's
    `metadata["game_match"]`."""

    method: ClassVar[str]
    default_timeout: ClassVar[int]

    def __init__(self, id: str, version: int | None, env_id: str, timeout_seconds: int | None = None,
                 depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.timeout_seconds = env_id, timeout_seconds or self.default_timeout

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> _EndMatchTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"], timeout_seconds=data.get("timeout_seconds"))

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        lobby_id = (context.metadata.get("game_lobby") or {}).get("lobby_id")
        match = await _invoke(deployed, MATCH, self.method, {"lobby_id": lobby_id} if lobby_id else {},
                              self.timeout_seconds)
        context.metadata["game_match"] = match
        log.info("%s: %s's match is %s%s", self.type, self.env_id, match["status"],
                 f" ({match['status_detail']})" if match.get("status_detail") else "")
        return context


class FinishMatchTaskStep(_EndMatchTaskStep):
    """Play a deployed game env's match out once its agents have stopped, before it is graded: put it after every
    prompt_agent step of the match. The game takes no more moves from its players and runs the match to the end its
    rules or limit set, so every match is graded at its end; a game that can't play out without its players ends it
    cancelled. A match already over is left as it is. `timeout_seconds` (default 7200) covers a realtime game's
    remaining time."""

    type: ClassVar[str] = "finish_match"
    entity_refs = (EntityRef.env("env_id"),)
    method = "finish"
    default_timeout = 7200


class CancelMatchTaskStep(_EndMatchTaskStep):
    """End a deployed game env's match where it stands: cancelled, every player without an outcome undecided. A match
    already over is left as it is."""

    type: ClassVar[str] = "cancel_match"
    entity_refs = (EntityRef.env("env_id"),)
    method = "cancel"
    default_timeout = 60


class SaveMatchFilesTaskStep(TaskStep):
    """Keep the files a game env keeps of its finished match (the match protocol's `files`, the game's
    `@match_files`), such as its replay or its recording: put it after finish_match. `kinds` picks among them, else
    the game keeps its default ones. Each file is streamed from the env, through a temporary file, into a file
    artifact, so an hour of video never sits in memory; they are listed in the run's `metadata["match_files"]`, by
    this step's id. What the game says it could not keep is logged."""

    type: ClassVar[str] = "save_match_files"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, kinds: list | None = None,
                 timeout_seconds: int = 3600, depends_on: list | None = None, fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        if kinds is not None and not (isinstance(kinds, list) and all(isinstance(k, str) and k for k in kinds)):
            raise ValueError("save_match_files kinds is a list of the kinds of files to keep")
        self.env_id, self.timeout_seconds = env_id, timeout_seconds
        self.kinds = list(kinds) if kinds is not None else None

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "kinds": self.kinds,
                "timeout_seconds": self.timeout_seconds, "fail_task_on_error": self.fail_task_on_error}

    @classmethod
    def from_dict(cls, data: dict) -> SaveMatchFilesTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", False)},
                   env_id=data["env_id"], **{k: data[k] for k in ("kinds", "timeout_seconds") if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        lobby_id = (context.metadata.get("game_lobby") or {}).get("lobby_id")
        request = {k: v for k, v in (("lobby_id", lobby_id), ("kinds", self.kinds)) if v is not None}
        listed = await _invoke(deployed, MATCH, "files", request, self.timeout_seconds)
        for note in listed["notes"]:
            log.warning("save_match_files: %s", note)
        stem = context.instance_id or uuid.uuid4().hex
        saved = []
        for file in listed["files"]:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / file["name"]
                await _download(deployed.environment_url.rstrip("/") + file["path"], path, self.timeout_seconds)
                artifact = await asyncio.to_thread(FileArtifact.put, f"{stem}-{file['name']}",
                                                   description=f"The {file['kind']} of env {self.env_id!r}'s match",
                                                   file_path=str(path))
            saved.append({"name": file["name"], "kind": file["kind"], "artifact_id": artifact.id,
                          "version": artifact.version, "bytes": file["bytes"]})
            log.info("save_match_files: %s (%s, %d bytes) is file artifact %s v%d", file["name"], file["kind"],
                     file["bytes"], artifact.id, artifact.version)
        context.metadata.setdefault("match_files", {})[self.id] = saved
        return context


async def _download(url: str, path: Path, timeout: float) -> None:
    headers = sandbox_request_headers_for_url(url) or {}
    async with (httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30)) as http,
                http.stream("GET", url, headers=headers) as response):
        if response.status_code != 200:
            raise RuntimeError(f"{url}: {response.status_code} {_message((await response.aread()).decode())}")
        with path.open("wb") as out:
            async for chunk in response.aiter_bytes(1 << 20):
                out.write(chunk)


async def _slot_card(base: str, headers: dict, timeout: int) -> dict:
    """The env card a player slot serves at `base`, its environment's address."""
    url = base + WELL_KNOWN_PATH
    async with httpx.AsyncClient() as http:
        response = await http.get(url, headers={**(sandbox_request_headers_for_url(url) or {}), **headers},
                                  timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"player slot env card at {url}: {response.status_code} {_message(response.text)}")
    return response.json()


async def register_mcp(context: TaskStepContext, env: DeployedEnv, name: str, path: str, headers: dict) -> str:
    """Registers a player slot's MCP address, `path` under the env's, with the agent named `name`
    (urn:agentenv:mcp-config/v1, which only adds), so its MCP calls play that slot, and returns the address as the
    agent reaches it. One already registered is left alone, so a rerun works; an agent that also has the env's own
    address, where it would not play its slot, is refused."""
    agent = _agent(context, name)
    reach = (partial(reachable_url, from_sandbox_type=env.sandbox_type, to_sandbox_type=agent.sandbox_type)
             if isinstance(env, DeployedSandboxEnv) else str)
    url, own = reach(env.mcp_url.removesuffix(MCP_PATH) + path), reach(env.mcp_url)
    ext = A2AAgent.find_extension(agent.a2a_card or {}, A2AAgent.EXT_MCP_CONFIG)
    endpoint = agent.a2a_url + ext["params"]["endpoint"]
    async with httpx.AsyncClient() as http:
        listed = list(((await http.get(endpoint, timeout=60)).json().get("mcp_servers") or {}).values())
        same = next((v for v in listed if v.get("url") == url), None)
        if same is not None and (same.get("has_headers") or not headers):
            return url
        if same is not None or any(v.get("url") == own for v in listed):
            raise RuntimeError(f"agent {name!r} already has the env's address without its slot's, so it would not "
                               "play its slot: deploy players with \"env_ids\": [] (add_player_slot gives each its "
                               "address)")
        card_name = (env.environment_card or {}).get("name") or env.env_id
        sent = {**(sandbox_request_headers_for_url(url) or {}), **headers}
        body = {"url": url, "headers": sent or None, "name": card_name if not listed else f"{card_name}-{name}"}
        (await http.post(endpoint, json=body, timeout=180)).raise_for_status()
    return url


def _agent(context: TaskStepContext, name: str) -> DeployedAgent:
    """The deployed agent named `name`, if it can be given an MCP server."""
    agent = next((a for a in context.deployed_agents if a.agent_name == name), None)
    if agent is None:
        raise RuntimeError(f"slot {name!r}: no deploy_agent step deployed an agent named {name!r} (add_player_slot "
                           "with \"register\": false keeps the slot for a player that connects on its own)")
    if A2AAgent.find_extension(agent.a2a_card or {}, A2AAgent.EXT_MCP_CONFIG) is None or not agent.a2a_url:
        raise RuntimeError(f"agent {name!r} takes no MCP servers ({A2AAgent.EXT_MCP_CONFIG})")
    return agent


async def _invoke(deployed: DeployedEnv, uri: str, method: str, params: dict, timeout: int) -> dict:
    card = _card(deployed, uri)
    what = {LICENSE: "license", LOBBY: "lobby", MATCH: "match"}[uri]
    try:
        return await client.invoke_extension(deployed.environment_url, card, uri, params, timeout=timeout,
                                             method=method)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"env {deployed.env_id!r} {what} {method}: {_message(e.response.text)}") from e


def _message(body: str) -> str:
    """What an env's error reply says: the protocol's {"error": {"code", "message"}}, else the body itself."""
    try:
        error = json.loads(body)["error"]
        return f"{error['code']}: {error['message']}"
    except (ValueError, KeyError, TypeError):
        return body


def _deployed(context: TaskStepContext, env_id: str) -> DeployedEnv:
    deployed = next((d for d in context.deployed_envs if d.env_id == env_id), None)
    if deployed is None:
        raise RuntimeError(f"env {env_id!r} is not deployed in this run")
    return deployed


def _card(deployed: DeployedEnv, uri: str) -> dict:
    """The card that advertises `uri`: the env's own, else one of its children's (an env behind a gateway)."""
    own = deployed.environment_card or {}
    card = next((c for c in [own, *(own.get("children_environments") or [])] if client.find_extension(c, uri)), None)
    if card is None:
        raise RuntimeError(f"env {deployed.env_id!r} doesn't serve {uri}: it is not a game env")
    return card
