import asyncio
import os
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
from agent_env.a2a_agent.a2a_agent import A2AAgent
from agent_env.config import reset_config
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import DeployedAgent
from agentenv_protocol import client
from agentenv_protocol.types import WELL_KNOWN_PATH
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def local_stores(monkeypatch, tmp_path):
    """agent-env on its local default stores under tmp_path, whatever config the machine has."""
    config = tmp_path / "config.toml"
    config.write_text("")
    for var in [v for v in os.environ if v.startswith("AGENT_ENV_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("AGENT_ENV_CONFIG", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    reset_config()
    yield tmp_path / "state"
    reset_config()


@asynccontextmanager
async def serving(app):
    """An ASGI app served on a free local port; yields its base URL."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def deployed(env):
    """A game env served, as the record a deploy_env step leaves."""
    async with serving(env.create_app().streamable_http_app()) as base:
        yield DeployedEnv(env_id="tictactoe", env_version=1, environment_card_url=base + WELL_KNOWN_PATH,
                          environment_card=await client.get_card(base))


class FakeAgent:
    """An A2A agent's MCP-config extension (urn:agentenv:mcp-config/v1): add and list, as agentenv-protocol's agent
    framework serves them."""

    def __init__(self):
        self.servers: dict[str, dict] = {}
        self.app = Starlette(routes=[Route("/ext/mcp-config", self.listing, methods=["GET"]),
                                     Route("/ext/mcp-config", self.add, methods=["POST"])])

    async def listing(self, request: Request) -> JSONResponse:
        return JSONResponse({"mcp_servers": {n: {"url": v["url"], "has_headers": bool(v.get("headers"))}
                                             for n, v in self.servers.items()}})

    async def add(self, request: Request) -> JSONResponse:
        body = await request.json()
        if any(v["url"] == body["url"] for v in self.servers.values()):
            return JSONResponse({"detail": "already registered"}, status_code=409)
        self.servers[body.get("name") or f"mcp_{len(self.servers)}"] = body
        return JSONResponse({"status": "added"})

    def deployed(self, base: str, name: str) -> DeployedAgent:
        card = {"capabilities": {"extensions": [{"uri": A2AAgent.EXT_MCP_CONFIG,
                                                 "params": {"endpoint": "/ext/mcp-config"}}]}}
        return DeployedAgent(agent_name=name, api_url=base, a2a_url=base, a2a_card=card)
