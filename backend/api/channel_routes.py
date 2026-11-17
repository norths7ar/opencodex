"""HTTP mapping for channel management, with a single public channel contract."""

from fastapi import APIRouter, Response

from backend.application.channels import ChannelManagement
from backend.application.contracts import (
    ChannelConfigurationEdit,
    ChannelConfigurationSaved,
    ChannelDefaultSaved,
    ChannelDetail,
    ChannelList,
    ChannelSaved,
)
from backend.application.errors import ApplicationError

from .subscription_routes import subscription_router


def channel_router(
    *,
    channels,
    authentication,
    catalog,
    quota=None,
    on_changed=None,
    completion_probe=None,
    port=None,
):
    router = APIRouter(prefix="/api")
    service = ChannelManagement(
        channels,
        authentication,
        catalog,
        quota=quota,
        on_changed=on_changed,
        completion_probe=completion_probe,
    )

    @router.get("/services")
    def services():
        return service.presets()

    @router.get("/channels", response_model=ChannelList, response_model_exclude_unset=True)
    def list_channels(response: Response):
        result = service.list()
        response.headers["X-Config-Revision"] = str(result.revision)
        return result

    @router.get(
        "/channels/configuration",
        response_model=ChannelConfigurationEdit,
        response_model_exclude_unset=True,
    )
    def configuration():
        return service.editor_view(service.store.read())

    @router.put(
        "/channels/configuration",
        response_model=ChannelConfigurationSaved,
        response_model_exclude_unset=True,
    )
    async def replace(body: dict):
        return await service.replace_channels(body)

    @router.put(
        "/channels/default", response_model=ChannelDefaultSaved, response_model_exclude_unset=True
    )
    async def default(body: dict):
        if body.keys() - {"channel_id", "model_id", "revision"} or not isinstance(
            body.get("channel_id"), str
        ):
            raise ApplicationError(400, "Provide a channel ID and optional model and revision.")
        return await service.set_default(
            body["channel_id"], body.get("model_id"), body.get("revision")
        )

    @router.get("/channels/{name}", response_model=ChannelDetail, response_model_exclude_unset=True)
    def channel(name: str, response: Response):
        result = service.read(name)
        response.headers["X-Config-Revision"] = str(result.revision)
        return result

    @router.post(
        "/channels",
        response_model=ChannelSaved,
        status_code=201,
        response_model_exclude_unset=True,
    )
    async def create(body: dict, response: Response, revision: int | None = None):
        result, application, saved = await service.save(
            service.parse_edit(body), revision=revision, create=True
        )
        response.headers["X-Config-Revision"] = str(saved)
        return ChannelSaved.model_validate(
            dict(result.model_dump(exclude_unset=True), saved=True, application=application)
        )

    @router.put("/channels/{name}", response_model=ChannelSaved, response_model_exclude_unset=True)
    async def save(name: str, body: dict, response: Response, revision: int | None = None):
        edit = service.parse_edit(body)
        if edit.channel.id != name:
            raise ApplicationError(400, "Channel ID does not match the URL.")
        service.channel(name)
        result, application, saved = await service.save(edit, revision=revision)
        response.headers["X-Config-Revision"] = str(saved)
        return ChannelSaved.model_validate(
            dict(result.model_dump(exclude_unset=True), saved=True, application=application)
        )

    @router.delete("/channels/{name}")
    async def remove(name: str, revision: int | None = None):
        return await service.remove_provider(name, revision)

    @router.patch("/channels/{name}/credential-label")
    def label(name: str, body: dict):
        if body.keys() != {"label"}:
            raise ApplicationError(400, "Provide a credential label.")
        return service.key_alias(name, body["label"])

    @router.post("/channels/{name}/probe")
    async def probe(name: str):
        return await service.probe(name)

    @router.post("/channels/{name}/completion-test")
    async def completion(name: str, body: dict):
        return await service.complete(name, body)

    router.include_router(subscription_router(service, quota=quota))
    return router
