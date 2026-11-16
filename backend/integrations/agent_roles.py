"""Read user-authored Codex role fallback declarations into an immutable snapshot."""

from pathlib import Path

from .codex_config import read_document
from .files import ClientConfigError


def fallback_snapshot(home: Path) -> dict[str, tuple[str, ...]]:
    models = {}
    for path in (home / "agents").glob("*.toml"):
        try:
            document = read_document(path)
        except ClientConfigError:
            continue
        primary, chain = document.get("model"), document.get("model_fallback", [])
        if (
            isinstance(primary, str)
            and isinstance(chain, list)
            and all(isinstance(value, str) for value in chain)
        ):
            models.setdefault(primary, []).extend(chain)
    return {primary: tuple(chain) for primary, chain in models.items()}
