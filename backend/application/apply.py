"""Summarize client projection outcomes after a successful configuration commit."""

from backend.catalog.refresh import RefreshOutcome


def application_result(result):
    clients = result.get("clients")
    if isinstance(clients, dict):
        status = (
            "failed"
            if not result.get("ok")
            else "skipped"
            if not clients or all(client["state"] == "skipped" for client in clients.values())
            else "applied"
        )
        return {"status": status, "clients": clients}
    return result


def catalog_application(outcome: RefreshOutcome | None):
    if outcome and outcome.projection_error:
        return {"status": "failed", "message": outcome.projection_error}
    if outcome and isinstance(outcome.projection_result, dict):
        return application_result(outcome.projection_result)
    return {"status": "skipped"}


async def apply_client_changes(callback):
    if callback is None:
        return {"status": "skipped"}
    try:
        return application_result(await callback())
    except (ValueError, RuntimeError, OSError) as exc:
        return {"status": "failed", "message": str(exc)}
