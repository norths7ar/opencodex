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
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .config import ConfigStore
from .gateway import Gateway
from .management import management_router
from .runtime import WEB_ROOT, instance_lock, write_state


def create_app(directory: Path, state: dict, stop):
    store = ConfigStore(directory)
    gateway = Gateway(store)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=gateway.lifespan)
    started = time.monotonic()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Pydantic's default error response includes the raw submitted credential in `input`.
        return JSONResponse(
            {
                "detail": [
                    {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
                ]
            },
            422,
        )

    @app.exception_handler(ValueError)
    async def bad_configuration(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request: Request, exc: RuntimeError):
        return JSONResponse({"detail": str(exc)}, 409)

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
            supplied = request.headers.get("authorization", "")
            if not secrets.compare_digest(supplied, f"Bearer {state['token']}"):
                return JSONResponse({"error": "A local console session is required."}, 401)
        if request.url.path.startswith("/v1/"):
            supplied = request.headers.get("authorization", "")
            if not secrets.compare_digest(supplied, f"Bearer {store.gateway_key}"):
                return JSONResponse({"error": {"message": "A gateway API key is required."}}, 401)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz")
    async def health():
        return report()

    @app.get("/opencodex-session")
    async def session():
        return {"token": state["token"]}

    @app.get("/api/status")
    async def runtime_status():
        return {
            **report(),
            "logPath": str(directory / "runtime.log"),
            "stateDirectory": str(directory),
        }

    @app.post("/api/stop")
    async def stop_instance():
        asyncio.get_running_loop().call_later(0.1, stop)
        return {"status": "stopping"}

    app.include_router(management_router(store, gateway, directory, state["port"]))
    app.include_router(gateway.router())

    @app.api_route("/api/{route:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def not_migrated(route: str):
        return JSONResponse({"error": "This API has not been migrated to Python yet."}, 501)

    @app.get("/{route:path}")
    async def console(route: str):
        if route == "healthz" or route.startswith(("api/", "v1/")):
            return JSONResponse({"error": "Not implemented in this migration stage."}, 501)
        file = (WEB_ROOT / (route or "index.html")).resolve()
        if not file.is_relative_to(WEB_ROOT.resolve()):
            return JSONResponse({"error": "Not found"}, 404)
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
            "token": secrets.token_urlsafe(32),
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
