"""Offline Lite workflows: private homes, no external connections or processes."""

import asyncio
import os
import socket
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest

from backend.app import create_app
from backend.services import Services


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def sandbox():
    scratch = Path(__file__).resolve().parents[1] / ".tmp"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="20261117-lite-workflow-", dir=scratch) as directory:
        yield Path(directory)


@pytest.fixture(autouse=True)
async def isolate_machine(sandbox, monkeypatch, anyio_backend):
    """A missed mock must fail, not contact an account or manage the real service."""
    for name, folder in {
        "HOME": "home",
        "USERPROFILE": "home",
        "APPDATA": "appdata",
        "LOCALAPPDATA": "localappdata",
        "CODEX_HOME": "codex",
        "DSH_HOME": "dsh",
        "OPENCODEX_LITE_HOME": "state",
        "OPENCODEX_HOME": "legacy",
    }.items():
        path = sandbox / folder
        path.mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(path))

    def blocked(*args, **kwargs):
        raise AssertionError("Lite workflow tests cannot open network connections or subprocesses")

    async def async_blocked(*args, **kwargs):
        blocked()

    # AnyIO has already created Windows' event-loop socket pair at this point.
    # Undo before that loop is torn down, without permitting arbitrary loopback access.
    with monkeypatch.context() as boundary:
        boundary.setattr(socket.socket, "connect", blocked)
        boundary.setattr(socket.socket, "connect_ex", blocked)
        boundary.setattr(socket, "getaddrinfo", blocked)
        # Windows Proactor uses ConnectEx, bypassing socket.socket.connect.
        loop = asyncio.get_running_loop()
        boundary.setattr(loop, "create_connection", async_blocked)
        boundary.setattr(loop, "sock_connect", async_blocked)
        boundary.setattr(loop, "getaddrinfo", async_blocked)
        boundary.setattr(subprocess, "Popen", blocked)
        yield


@pytest.fixture
def open_lite(sandbox):
    """Real HTTP application and stores; only the upstream transport is replaced."""

    @asynccontextmanager
    async def open_instance(handler, *, directory=None):
        directory = directory or sandbox / "state"
        directory.mkdir(exist_ok=True)
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        services = Services(
            directory,
            10199,
            codex_home=sandbox / "codex",
            dsh_home=sandbox / "dsh",
            client=upstream,
            background_refresh=False,
        )
        app = create_app(
            directory,
            {
                "port": 10199,
                "pid": os.getpid(),
                "token": "test-session-only",
                "startedAt": datetime.now(UTC).isoformat(),
            },
            lambda: None,
            services=services,
            web_root=sandbox / "web",
        )
        async with (
            services.lifespan(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://127.0.0.1:10199",
                trust_env=False,
            ) as client,
        ):
            session = (await client.get("/opencodex-session")).json()
            client.headers.update(
                {
                    "x-opencodex-api-key": session["token"],
                    "x-opencodex-csrf-token": session["csrfToken"],
                }
            )
            yield SimpleNamespace(client=client, services=services, app=app)

    return open_instance


@pytest.fixture
def register_channel():
    async def register(client, *, identity="demo", protocol="openai-chat", **overrides):
        channel = {
            "id": identity,
            "name": identity,
            "service_id": "custom",
            "protocol": protocol,
            "auth_mode": "local",
            "endpoints": {"base_url": f"https://{identity}.invalid/v1"},
            "models": {"live": False, "static": ["model-a"], "default": "model-a"},
            **overrides,
        }
        response = await client.post("/api/channels", json={"channel": channel})
        assert response.status_code == 201, response.text
        return response.json()

    return register
