"""Converge explicitly enabled clients independently after effective catalog changes."""

import asyncio
import logging


class IntegrationCoordinator:
    def __init__(self, codex, dsh):
        self.codex = codex
        self.dsh = dsh
        self.lock = asyncio.Lock()
        self.last_results: dict[str, dict] = {}

    async def _run(self, *, startup=False):
        results = {}
        for name, operation in (
            ("codex", self.codex.converge if startup else self.codex.sync),
            ("dsh", self.dsh.sync),
        ):
            try:
                result = await operation()
                ok = result.get("ok") is not False
                results[name] = {
                    "ok": ok,
                    "state": result.get("status", "current") if ok else "needsAttention",
                    **{
                        key: result[key]
                        for key in ("reason", "message")
                        if isinstance(result.get(key), str)
                    },
                }
            except (ValueError, RuntimeError, OSError) as exc:
                results[name] = {"ok": False, "state": "needsAttention", "message": str(exc)}
        self.last_results = results
        return {
            "ok": all(result.get("ok", True) for result in results.values()),
            "clients": results,
        }

    async def sync(self) -> dict:
        async with self.lock:
            return await self._run()

    async def converge(self) -> dict:
        async with self.lock:
            return await self._run(startup=True)

    async def watch_roles(self) -> None:
        """Refresh external Codex role settings without request-path file access."""
        while True:
            try:
                await asyncio.to_thread(self.codex.refresh_roles)
            except ValueError, RuntimeError, OSError:
                logging.getLogger(__name__).exception("Could not refresh Codex role settings")
            await asyncio.sleep(2)
