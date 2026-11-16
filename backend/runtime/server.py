"""Own the loopback listener, runtime state, and process lock."""

import os
import secrets
import socket
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from .state import instance_lock, write_state


def serve(directory: Path, port: int, create_app=None) -> None:
    if create_app is None:
        from backend.app import create_app
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
