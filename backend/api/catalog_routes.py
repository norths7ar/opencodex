"""HTTP mapping for cached model inventory and model editing use cases."""

from fastapi import APIRouter

from backend.application.catalog import CatalogManagement


def catalog_router(*, store, catalog, on_changed=None, models=None):
    router = APIRouter(prefix="/api")
    service = CatalogManagement(store, catalog, on_changed=on_changed, models=models)
    router.add_api_route("/models", service.list_models, methods=["GET"])
    router.add_api_route("/selected-models", service.selection, methods=["GET"])
    router.add_api_route("/channels/{name}/models/refresh", service.refresh, methods=["POST"])
    router.add_api_route("/channels/{name}/models/status", service.status, methods=["GET"])
    router.add_api_route("/aliases", service.aliases, methods=["GET"])
    router.add_api_route("/default-aliases", service.default_aliases, methods=["PUT"])
    router.add_api_route("/model-settings", service.model_settings, methods=["PUT"])
    router.add_api_route("/custom-models", service.custom_models, methods=["GET"])
    router.add_api_route("/custom-models", service.add_custom, methods=["POST"], status_code=201)
    router.add_api_route("/custom-models/{identity}", service.edit_custom, methods=["PUT"])
    router.add_api_route("/custom-models/{identity}", service.delete_custom, methods=["DELETE"])

    @router.put("/channels/{name}/model-aliases")
    async def aliases(name: str, body: dict):
        return await service.labels(name, body, display=False)

    @router.put("/channels/{name}/model-display-names")
    async def display_names(name: str, body: dict):
        return await service.labels(name, body, display=True)

    return router
