"""Dependencies and common read/apply behavior of settings operations."""

from .apply import apply_client_changes
from .errors import ApplicationError


class SettingsContext:
    def __init__(
        self,
        *,
        service,
        catalog,
        apply_changes=None,
    ):
        self.service, self.catalog = service, catalog
        self.apply_changes = apply_changes

    def config(self):
        return self.service.store.read().configuration

    def visible(self):
        return [model.routed_selector for model in self.catalog() if model.visible]

    async def converge(self):
        return await apply_client_changes(self.apply_changes)

    @staticmethod
    def checked(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except ValueError as exc:
            raise ApplicationError(400, str(exc)) from exc
