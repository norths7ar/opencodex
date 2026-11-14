"""Search helper picker and patch semantics from management/web-search-sidecar-options."""

BACKENDS = {"openai", "anthropic", "xai", "gemini", "exa"}
AUTH_SLOTS = {"openai": "gpt-5.6-luna", "anthropic": "claude-haiku-4-5"}


def candidate_rows(rows, available):
    """Picker-visible native subscription rows plus each logged-in channel's auth slot."""
    candidates = {}
    for row in rows:
        backend = row.get("provider")
        if backend not in AUTH_SLOTS or backend not in available or row.get("disabled"):
            continue
        if backend == "openai" and (not row.get("native") or "/" in row["id"]):
            continue
        candidates[(backend, row["id"])] = {**row, "backend": backend}
    for backend, identity in AUTH_SLOTS.items():
        if backend in available:
            candidates[(backend, identity)] = {
                "provider": backend,
                "backend": backend,
                "id": identity,
                "authSlot": True,
            }
    return list(candidates.values())


def model_options(stored, candidates):
    """Retain a saved unavailable selection for display, not as admission evidence."""
    options = {}
    for candidate in candidates:
        identity = candidate["id"]
        if identity not in options:
            options[identity] = {
                "value": identity,
                "label": identity,
                "backend": candidate["backend"],
                "model": identity,
                **({"authSlot": True} if candidate.get("authSlot") else {}),
            }
    saved = stored.get("model")
    if saved and saved not in options:
        options[saved] = {
            "value": saved,
            "label": saved,
            "backend": stored.get("backend", "openai"),
            "model": saved,
        }
    return sorted(options.values(), key=lambda row: row["value"].casefold())


def accepts_model(backend, model, candidates):
    # Auth-slot selections remain legal before login; execution checks credentials.
    for family, identity in AUTH_SLOTS.items():
        if identity == model:
            return family == backend
    return any(row["backend"] == backend and row["id"] == model for row in candidates)


def apply_patch(stored, patch, candidates):
    """Merge the migrated single-account controls; preserve untouched backend settings."""
    allowed = {"enabled", "backend", "model", "reasoning", "streamRoutedModelOutput"}
    if not isinstance(patch, dict) or set(patch) - allowed:
        raise ValueError("Unknown search helper setting.")
    result = dict(stored)
    for key in ("enabled", "streamRoutedModelOutput"):
        if key in patch:
            value = patch[key]
            if type(value) is not bool:
                raise ValueError(f"{key} must be boolean.")
            if value == (key == "enabled"):
                result.pop(key, None)
            else:
                result[key] = value
    if "backend" in patch:
        backend = patch["backend"]
        if backend is None:
            result.pop("backend", None)
        elif isinstance(backend, str) and backend in BACKENDS:
            result["backend"] = backend
        else:
            raise ValueError("Unknown search backend.")
    if "model" in patch:
        model = patch["model"]
        if not isinstance(model, str):
            raise ValueError("Search model must be a string.")
        if model:
            result["model"] = model
        else:
            result.pop("model", None)
    if "reasoning" in patch:
        if not isinstance(patch["reasoning"], str):
            raise ValueError("Search reasoning must be a string.")
        result["reasoning"] = patch["reasoning"]
    if (
        {"model", "backend"} & patch.keys()
        and result.get("model")
        and not accepts_model(result.get("backend", "openai"), result["model"], candidates)
    ):
        raise ValueError("Select a model supported by the chosen search backend.")
    return result
