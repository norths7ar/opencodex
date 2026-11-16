"""Same-origin management session and built console hosting."""

import mimetypes
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .errors import error_response


def install_console(app, token: str, web_root: Path):
    csrf_token = secrets.token_urlsafe(32)

    def refusal(request, status, message):
        if request.url.path.startswith("/api/"):
            return error_response(
                status, "authentication_required" if status == 401 else "access_denied", message
            )
        return JSONResponse({"error": message}, status)

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
            return refusal(request, 403, "Only same-origin loopback access is allowed.")
        if request.headers.get("sec-fetch-site") == "cross-site":
            return refusal(request, 403, "Cross-site access is not allowed.")
        if referer and urlsplit(referer).netloc != host:
            return refusal(request, 403, "Cross-origin access is not allowed.")
        if request.url.path.startswith("/api/"):
            gui_key = request.headers.get("x-opencodex-api-key", "")
            bearer = request.headers.get("authorization", "")
            if not (
                secrets.compare_digest(gui_key, token)
                or secrets.compare_digest(bearer, f"Bearer {token}")
            ):
                return refusal(request, 401, "A local console session is required.")
            if (
                gui_key
                and request.method not in {"GET", "HEAD"}
                and not secrets.compare_digest(
                    request.headers.get("x-opencodex-csrf-token", ""), csrf_token
                )
            ):
                return refusal(request, 403, "A current console CSRF token is required.")
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/opencodex-session")
    async def session(request: Request):
        origin = f"http://{request.headers['host']}"
        return {
            "token": token,
            "csrfToken": csrf_token,
            "browserOrigin": origin,
            "serverOrigin": origin,
        }

    @app.get("/{route:path}")
    async def console(route: str):
        if route.startswith("api/"):
            return error_response(404, "not_found", "Not found.")
        if route == "healthz" or route.startswith(("api/", "v1/")):
            return JSONResponse({"error": "Not found"}, 404)
        file = (web_root / (route or "index.html")).resolve()
        if not file.is_relative_to(web_root.resolve()):
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
                file, media_type=mimetypes.guess_type(file)[0], headers={"Cache-Control": cache}
            )
        if not route and not (web_root / "index.html").exists():
            return HTMLResponse(
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                "<title>OpenCodex Lite</title><h1>OpenCodex Lite 已启动</h1>"
                "<p>网页尚未发布。在项目目录执行 <code>./scripts/update.ps1</code>"
                "。前台运行时请先停止网关，更新后重新启动。</p>"
                '<a href="/healthz">查看健康状态</a></html>',
                status_code=503,
            )
        return JSONResponse({"error": "Not found"}, 404)
