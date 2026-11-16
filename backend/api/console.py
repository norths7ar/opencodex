"""Same-origin management session and built console hosting."""

import mimetypes
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


def install_console(app, token: str, web_root: Path):
    csrf_token = secrets.token_urlsafe(32)

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
                secrets.compare_digest(gui_key, token)
                or secrets.compare_digest(bearer, f"Bearer {token}")
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
                "<p>网页尚未构建。在项目目录执行 <code>pnpm --dir frontend build:runtime</code>"
                '，然后刷新此页。</p><a href="/healthz">查看健康状态</a></html>',
                status_code=503,
            )
        return JSONResponse({"error": "Not found"}, 404)
