"""Management contracts for optional agent request controls."""

from fastapi import APIRouter, HTTPException

from .agent_policy import DECLARED_EFFORTS, EFFORTS


def agent_router(store):
    router = APIRouter(prefix="/api")

    @router.get("/effort-caps")
    def effort_caps():
        config = store.load()
        return {
            "effortCap": getattr(config, "effortCap", None),
            "subagentEffortCap": getattr(config, "subagentEffortCap", None),
            "modelPinnedEfforts": getattr(config, "modelPinnedEfforts", {}),
            "efforts": list(EFFORTS),
        }

    @router.put("/effort-caps")
    def save_effort_caps(body: dict):
        with store.mutation_lock:
            config = store.load()
            for key in ("effortCap", "subagentEffortCap"):
                if key not in body:
                    continue
                value = body[key]
                if value is not None and value != "" and value not in EFFORTS:
                    raise HTTPException(400, "Caps must be valid reasoning efforts or null.")
                if value is None or value == "":
                    config.__pydantic_extra__.pop(key, None)
                else:
                    setattr(config, key, value)
            if "modelPinnedEfforts" in body:
                patch = body["modelPinnedEfforts"]
                if patch is not None and not isinstance(patch, dict):
                    raise HTTPException(400, "modelPinnedEfforts must be an object or null.")
                pins = dict(getattr(config, "modelPinnedEfforts", {})) if patch is not None else {}
                for model, effort in (patch or {}).items():
                    if not model.strip() or (
                        effort is not None and effort != "" and effort not in DECLARED_EFFORTS
                    ):
                        raise HTTPException(
                            400, "Model pins require a model and a declared effort."
                        )
                    if effort is None or effort == "":
                        pins.pop(model.strip(), None)
                    else:
                        pins[model.strip()] = effort
                if pins:
                    config.modelPinnedEfforts = pins
                else:
                    config.__pydantic_extra__.pop("modelPinnedEfforts", None)
            store.save(config)
        return effort_caps()

    return router
