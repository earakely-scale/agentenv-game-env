"""The lobby's task steps: `add_player_slot` fills one slot of a deployed game env's lobby and gives an agent its
slot's address; `start_match` closes the lobby, which creates the game."""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import ClassVar

import httpx
from agent_env.a2a_agent.a2a_agent import A2AAgent
from agent_env.entity_refs import EntityRef
from agent_env.env.env import DeployedEnv, DeployedSandboxEnv
from agent_env.providers.sandbox_providers.sandbox_provider import reachable_url, sandbox_request_headers_for_url
from agent_env.task_step.context import DeployedAgent, TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agentenv_protocol import client
from agentenv_protocol.types import MCP_PATH
from pydantic import ValidationError

from .lobby import LOBBY, Connect, Occupant, OccupantKind, PlayerSlot

log = logging.getLogger(__name__)


class AddPlayerSlotTaskStep(TaskStep):
    """Fill one slot of a deployed game env's lobby with `occupant` (`{"kind": "agent" | "human" | "ai", "name"}`), at
    `slot` (the next free one by default), with the game's `additional_settings` for it (WC3: faction, team,
    ai_level). An agent slot's address is registered with the deploy_agent agent of that name, so its calls play this
    slot (deploy it with `"env_ids": []`); `register: false` only reserves the slot, for a player that connects on its
    own. A human slot's link is logged. Each slot is kept in the run's `metadata["game_slots"]`."""

    type: ClassVar[str] = "add_player_slot"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, occupant: dict, slot: int | None = None,
                 additional_settings: dict | None = None, register: bool = True, timeout_seconds: int = 60,
                 depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        try:
            self.occupant = Occupant(**occupant)
        except (TypeError, ValidationError) as e:
            raise ValueError(f"add_player_slot occupant: {e}") from e
        if slot is not None and (not isinstance(slot, int) or slot < 0):
            raise ValueError(f"add_player_slot slot is a slot number from 0, got {slot!r}")
        if additional_settings is not None and not isinstance(additional_settings, dict):
            raise ValueError("add_player_slot additional_settings is an object")
        self.env_id, self.slot, self.register, self.timeout_seconds = env_id, slot, register, timeout_seconds
        self.additional_settings = dict(additional_settings or {})

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "occupant": self.occupant.model_dump(mode="json",
                                                                                                 exclude_none=True),
                "slot": self.slot, "additional_settings": self.additional_settings, "register": self.register,
                "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> AddPlayerSlotTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"], occupant=data["occupant"],
                   **{k: data[k] for k in ("slot", "additional_settings", "register", "timeout_seconds") if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        if self.occupant.kind is OccupantKind.AGENT and self.register:
            _agent(context, self.occupant.name)   # before the slot is taken, so a missing agent leaves no slot
        request = {"occupant": self.occupant.model_dump(mode="json", exclude_none=True),
                   "additional_settings": self.additional_settings,
                   **({"slot": self.slot} if self.slot is not None else {})}
        slot = PlayerSlot(**await _invoke(deployed, "fill", request, self.timeout_seconds))
        name = slot.occupant.name
        record = slot.model_dump(mode="json", exclude_none=True)
        base = deployed.mcp_url.removesuffix(MCP_PATH)
        if slot.connect is not None:
            record["url"] = (await register_mcp(context, deployed, name, slot.connect) if self.register
                             else base + slot.connect.path)
            log.info("add_player_slot: %s plays slot %d at %s", name, slot.slot, record["url"])
        if slot.play is not None:
            record["play_url"] = base + slot.play
            log.warning("PLAY slot %d (%s): %s", slot.slot, name, record["play_url"])
        if slot.occupant.kind is OccupantKind.AI:
            log.info("add_player_slot: the game's AI plays slot %d", slot.slot)
        context.metadata.setdefault("game_slots", {})[name or f"slot-{slot.slot}"] = record
        return context


class StartMatchTaskStep(TaskStep):
    """Close a deployed game env's lobby, which creates the game from its slots. Put it after every add_player_slot
    step of the game and before its players play; closing a closed lobby returns its game again. The lobby and what
    the game reports of itself are kept in the run's `metadata["game_lobby"]`."""

    type: ClassVar[str] = "start_match"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, timeout_seconds: int = 900,
                 depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.timeout_seconds = env_id, timeout_seconds

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> StartMatchTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"], **({"timeout_seconds": data["timeout_seconds"]}
                                             if "timeout_seconds" in data else {}))

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        result = await _invoke(deployed, "close", {}, self.timeout_seconds)
        context.metadata["game_lobby"] = result
        log.info("start_match: %s created its game with %d players", self.env_id, len(result.get("slots") or ()))
        return context


async def register_mcp(context: TaskStepContext, env: DeployedEnv, name: str, connect: Connect) -> str:
    """Registers a slot's address with the agent named `name` (urn:agentenv:mcp-config/v1, which only adds), so its
    MCP calls play that slot, and returns the address. One already registered is left alone, so a rerun works; an
    agent that also has the env's own address, where it would not play its slot, is refused."""
    agent = _agent(context, name)
    reach = (partial(reachable_url, from_sandbox_type=env.sandbox_type, to_sandbox_type=agent.sandbox_type)
             if isinstance(env, DeployedSandboxEnv) else str)
    url, own = reach(env.mcp_url.removesuffix(MCP_PATH) + connect.path), reach(env.mcp_url)
    ext = A2AAgent.find_extension(agent.a2a_card or {}, A2AAgent.EXT_MCP_CONFIG)
    endpoint = agent.a2a_url + ext["params"]["endpoint"]
    async with httpx.AsyncClient() as http:
        listed = list(((await http.get(endpoint, timeout=60)).json().get("mcp_servers") or {}).values())
        same = next((v for v in listed if v.get("url") == url), None)
        if same is not None and (same.get("has_headers") or not connect.headers):
            return url
        if same is not None or any(v.get("url") == own for v in listed):
            raise RuntimeError(f"agent {name!r} already has the env's address without its slot's, so it would not "
                               "play its slot: deploy players with \"env_ids\": [] (add_player_slot gives each its "
                               "address)")
        card_name = (env.environment_card or {}).get("name") or env.env_id
        headers = {**(sandbox_request_headers_for_url(url) or {}), **connect.headers}
        body = {"url": url, "headers": headers or None, "name": card_name if not listed else f"{card_name}-{name}"}
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


async def _invoke(deployed: DeployedEnv, method: str, params: dict, timeout: int) -> dict:
    card = _card(deployed)
    try:
        return await client.invoke_extension(deployed.environment_url, card, LOBBY, params, timeout=timeout,
                                             method=method)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"env {deployed.env_id!r} lobby {method}: {_message(e.response.text)}") from e


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


def _card(deployed: DeployedEnv) -> dict:
    """The card that advertises the lobby: the env's own, else one of its children's (an env behind a gateway)."""
    own = deployed.environment_card or {}
    card = next((c for c in [own, *(own.get("children_environments") or [])] if client.find_extension(c, LOBBY)), None)
    if card is None:
        raise RuntimeError(f"env {deployed.env_id!r} has no lobby ({LOBBY}): it is not a game env")
    return card
