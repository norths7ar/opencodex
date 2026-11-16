"""Compose the Lite HTTP application from the selected domain services."""

import asyncio
import os
import time
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend.api.catalog_routes import catalog_router
from backend.api.channel_routes import channel_router
from backend.api.console import install_console
from backend.api.generation_routes import generation_router
from backend.api.import_routes import import_router
from backend.api.integration_routes import integration_router
from backend.api.observability_routes import observability_router
from backend.api.service_routes import service_router
from backend.api.settings import SettingsService
from backend.api.settings_routes import settings_router
from backend.gateway.probes import CompletionProbe
from backend.gateway.sizing import SizingRecommendations
from backend.runtime.state import WEB_ROOT
from backend.services import Services


def create_app(directory: Path, state: dict, stop, *, services=None, web_root=WEB_ROOT):
    services = services or Services(
        directory,
        state["port"],
        codex_home=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        dsh_home=Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")),
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=services.lifespan)
    app.state.services = services
    started = time.monotonic()

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid_request(request: Request, exc):
        return JSONResponse(
            {
                "error": "Invalid configuration.",
                "detail": [
                    {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
                ],
            },
            422,
        )

    @app.exception_handler(ValueError)
    async def bad_configuration(request: Request, exc):
        return JSONResponse({"error": str(exc), "detail": str(exc)}, 400)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request: Request, exc):
        return JSONResponse({"error": str(exc), "detail": str(exc)}, 409)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc):
        return JSONResponse(
            exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}, exc.status_code
        )

    def report():
        return {
            "status": "ok",
            "service": "opencodex-lite",
            "runtime": "python",
            "version": version("opencodex-lite"),
            "pid": state["pid"],
            "port": state["port"],
            "startedAt": state["startedAt"],
            "uptimeSeconds": round(time.monotonic() - started, 1),
            "consoleReady": (web_root / "index.html").is_file(),
        }

    @app.get("/healthz")
    def health():
        return report()

    @app.get("/api/status")
    def status():
        return {
            **report(),
            "logPath": str(directory / "runtime.log"),
            "stateDirectory": str(directory),
        }

    @app.get("/api/system/health")
    def system_health():
        result = report()
        return {
            **result,
            "uptime": result["uptimeSeconds"],
            "spendLedger": services.spend.diagnostics(),
        }

    @app.post("/api/stop")
    async def stop_instance():
        services.stopping.set()
        asyncio.get_running_loop().call_later(0.1, stop)
        return {"status": "stopping"}

    @app.get("/api/capabilities")
    def capabilities():
        return {
            "runtime": "python",
            "channelApi": True,
            "pages": [
                "home",
                "providers",
                "models",
                "integrations",
                "logs",
                "usage",
                "startup",
                "subagents",
                "dashboard",
                "storage",
            ],
            "usage": True,
            "debugLogs": True,
            "quota": True,
            "clients": ["codex", "dsh"],
            "adapters": ["openai-chat", "openai-responses", "anthropic"],
            "oauth": True,
            "oauthProviders": ["openai"],
        }

    @app.get("/api/access")
    def access():
        return {"baseUrl": f"http://127.0.0.1:{state['port']}/v1", "requiresApiKey": False}

    @app.get("/api/protocols")
    def protocols(provider: str):
        channel = services.channels.get(provider)
        overrides = [
            {"model": model, "adapter": protocol, "source": "operator"}
            for model, protocol in channel.models.adapters.items()
        ]
        return {
            "provider": {
                "name": channel.id,
                "adapter": channel.protocol,
                "adapterSource": "provider-default",
                "authMode": channel.auth_mode,
                "upstream": {
                    "openai-chat": "chat",
                    "openai-responses": "responses",
                    "anthropic": "messages",
                }[channel.protocol],
                "modelOverrides": overrides[:64],
                **({"modelOverridesTruncated": True} if len(overrides) > 64 else {}),
            }
        }

    app.include_router(
        channel_router(
            channels=services.channels,
            authentication=services.authentication,
            catalog=services.catalog,
            quota=services.quota,
            on_changed=services.changed,
            port=state["port"],
            completion_probe=CompletionProbe(services.dispatcher),
        )
    )
    app.include_router(
        catalog_router(
            store=services.store,
            catalog=services.catalog,
            on_changed=services.changed,
            models=services.models,
        )
    )
    app.include_router(
        settings_router(
            SettingsService(services.store),
            services.models,
            services.codex,
            services.workflow,
            services.dispatcher.fallback,
            auth_status=lambda _: services.auth.status(),
            pacer=services.executor.pacer,
            sizing=SizingRecommendations(
                services.dispatcher, services.models, services.configuration
            ),
        )
    )
    app.include_router(integration_router(services.codex, services.dsh))
    app.include_router(
        service_router(directory, state["port"], client_status=services.codex.status)
    )
    app.include_router(import_router(services.store, on_applied=services.changed))
    app.include_router(
        observability_router(
            directory,
            services.telemetry.store,
            services.store,
            services.debug,
            official_prices=lambda: services.official.models,
        )
    )
    app.include_router(generation_router(services.dispatcher, services.models))
    install_console(app, state["token"], web_root)
    return app
