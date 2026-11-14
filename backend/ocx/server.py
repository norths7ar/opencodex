"""Loopback management API and same-origin console hosting."""

import asyncio
import mimetypes
import os
import re
import secrets
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import ValidationError

from . import __version__
from .agent_api import agent_router
from .auxiliary_api import auxiliary_router
from .client_api import client_router
from .debug_api import debug_router
from .dsh_api import dsh_router
from .fallback_api import fallback_router
from .legacy_import_api import legacy_import_router
from .management import management_router
from .runtime import WEB_ROOT, instance_lock, write_state
from .services import AppServices
from .settings_api import settings_router
from .startup_api import startup_router
from .storage_api import storage_router
from .subagent_api import subagent_router
from .usage_api import usage_router
from .workflow_api import workflow_router


def create_app(directory: Path, state: dict, stop):
    services = AppServices(directory, state["port"])
    store, gateway = services.store, services.gateway
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=services.lifespan)
    csrf_token = secrets.token_urlsafe(32)
    started = time.monotonic()

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Pydantic's default error response includes the raw submitted credential in `input`.
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
    async def bad_configuration(request: Request, exc: ValueError):
        return JSONResponse({"error": str(exc), "detail": str(exc)}, 400)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request: Request, exc: RuntimeError):
        return JSONResponse({"error": str(exc), "detail": str(exc)}, 409)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc):
        payload = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        return JSONResponse(payload, exc.status_code)

    def report():
        return {
            "status": "ok",
            "service": "opencodex-lite",
            "runtime": "python",
            "version": __version__,
            "pid": state["pid"],
            "port": state["port"],
            "startedAt": state["startedAt"],
            "uptimeSeconds": round(time.monotonic() - started, 1),
            "consoleReady": (WEB_ROOT / "index.html").is_file(),
        }

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        try:
            parsed = urlsplit(f"http://{host}")
            valid = parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port
        except ValueError:
            valid = False
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if not valid or (origin and origin != f"http://{host}"):
            return JSONResponse({"error": "Only same-origin loopback access is allowed."}, 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "Cross-site access is not allowed."}, 403)
        if referer and urlsplit(referer).netloc != host:
            return JSONResponse({"error": "Cross-origin access is not allowed."}, 403)
        if request.url.path.startswith("/api/"):
            gui_key = request.headers.get("x-opencodex-api-key", "")
            bearer = request.headers.get("authorization", "")
            if not (
                secrets.compare_digest(gui_key, state["token"])
                or secrets.compare_digest(bearer, f"Bearer {state['token']}")
            ):
                return JSONResponse({"error": "A local console session is required."}, 401)
            if (
                gui_key
                and request.method not in {"GET", "HEAD"}
                and not secrets.compare_digest(
                    request.headers.get("x-opencodex-csrf-token", ""), csrf_token
                )
            ):
                return JSONResponse({"error": "A current console CSRF token is required."}, 403)
        # Like the original loopback listener, local clients need no gateway credential.
        # The browser management plane above still requires its own console session.
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz")
    async def health():
        return report()

    @app.get("/api/system/health")
    async def system_health():
        data = report()
        return {
            **data,
            "uptime": data["uptimeSeconds"],
            "spendLedger": services.spend_diagnostics(),
        }

    @app.get("/opencodex-session")
    async def session(request: Request):
        origin = f"http://{request.headers['host']}"
        return {
            "token": state["token"],
            "csrfToken": csrf_token,
            "browserOrigin": origin,
            "serverOrigin": origin,
        }

    @app.get("/api/status")
    async def runtime_status():
        return {
            **report(),
            "logPath": str(directory / "runtime.log"),
            "stateDirectory": str(directory),
        }

    @app.post("/api/stop")
    async def stop_instance():
        services.stopping.set()
        asyncio.get_running_loop().call_later(0.1, stop)
        return {"status": "stopping"}

    app.include_router(
        management_router(
            store,
            gateway,
            directory,
            state["port"],
            inventory=services.inventory,
            quota=services.quota,
            kimi_quota=services.kimi_quota,
            catalog_refresh=services.refresh_catalog,
            stopping=services.stopping,
        )
    )
    app.include_router(legacy_import_router(store, services.imported_providers))
    app.include_router(subagent_router(services.subagents))
    app.include_router(auxiliary_router(services.auxiliary, services.integration))
    app.include_router(workflow_router(store, gateway.workflow_budget))
    app.include_router(agent_router(store))
    app.include_router(settings_router(store, services.integration))
    app.include_router(fallback_router(gateway.fallback))
    app.include_router(debug_router(services.diagnostics))
    app.include_router(
        startup_router(directory, state["port"], client_status=services.integration.status)
    )
    app.include_router(dsh_router(directory, state["port"], services.inventory.rows))
    app.include_router(usage_router(gateway.usage_store, store, services.inventory.official))
    app.include_router(storage_router(directory, gateway.usage_store))
    app.include_router(client_router(services.integration))
    app.include_router(gateway.router())

    @app.get("/v1/responses")
    async def responses_transport(request: Request):
        # Original Design B: the built-in openai provider tries WS first. 426
        # selects its immediate HTTP fallback while WebSockets are disabled.
        if request.headers.get("upgrade", "").lower() == "websocket":
            return JSONResponse(
                {
                    "error": {
                        "code": "upgrade_required",
                        "message": "Responses WebSocket transport is disabled; use HTTP",
                    }
                },
                426,
            )
        return JSONResponse({"error": "Use POST for Responses requests."}, 405)

    @app.api_route("/api/{route:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def not_migrated(route: str):
        return JSONResponse({"error": "This feature has not been migrated yet."}, 501)

    @app.get("/{route:path}")
    async def console(route: str):
        if route == "healthz" or route.startswith(("api/", "v1/")):
            return JSONResponse({"error": "Not implemented in this migration stage."}, 501)
        file = (WEB_ROOT / (route or "index.html")).resolve()
        if not file.is_relative_to(WEB_ROOT.resolve()):
            return JSONResponse({"error": "Not found"}, 404)
        if file.is_dir():
            file = file / "index.html"
        if file.is_file():
            cache = (
                "public, max-age=31536000, immutable"
                if re.search(r"-[\w-]{8,}\.", file.name)
                else "no-store"
            )
            return FileResponse(
                file,
                media_type=mimetypes.guess_type(file)[0],
                headers={"Cache-Control": cache},
            )
        if not route and not (WEB_ROOT / "index.html").exists():
            return HTMLResponse(
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                "<title>OpenCodex Lite</title><h1>OpenCodex Lite 已启动</h1>"
                "<p>网页尚未构建。在项目目录执行 <code>pnpm --dir frontend build:runtime</code>"
                '，然后刷新此页。</p><a href="/healthz">查看健康状态</a></html>',
                status_code=503,
            )
        return JSONResponse({"error": "Not found"}, 404)

    return app


def serve(directory: Path, port: int) -> None:
    with instance_lock(directory, "instance.lock"):
        state = {
            "state": "starting",
            "pid": os.getpid(),
            "port": port,
            "token": "ocx_session_" + secrets.token_urlsafe(32),
            "startedAt": datetime.now(UTC).isoformat(),
        }
        write_state(directory, state)
        try:
            # Bind before advertising readiness; never silently pick a different gateway port.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(("127.0.0.1", port))
                listener.listen(128)
                server = uvicorn.Server(
                    uvicorn.Config(
                        create_app(directory, state, lambda: setattr(server, "should_exit", True)),
                        host="127.0.0.1",
                        port=port,
                        access_log=False,
                        timeout_graceful_shutdown=10,
                        proxy_headers=False,
                        ws="none",
                    )
                )
                state["state"] = "running"
                write_state(directory, state)
                server.run(sockets=[listener])
            state["state"] = "stopped"
        except KeyboardInterrupt:
            state["state"] = "stopped"
        except BaseException as exc:
            state.update(state="failed", error=str(exc) or type(exc).__name__)
            raise
        finally:
            state.pop("token", None)
            write_state(directory, state)
