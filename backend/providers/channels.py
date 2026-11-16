"""Channel configuration operations, independent of HTTP and model discovery."""

from collections.abc import Callable
from uuid import uuid4

from backend.auth.access import is_chatgpt_subscription
from backend.catalog.selection import reconcile_discovery
from backend.configuration.models import Channel, CredentialRecord
from backend.configuration.store import ConfigurationState, ConfigurationStore


class ChannelService:
    def __init__(self, store: ConfigurationStore):
        self.store = store

    def get(self, channel_id: str) -> Channel:
        return self.find(self.store.read(), channel_id)

    @staticmethod
    def find(state: ConfigurationState, channel_id: str) -> Channel:
        for channel in state.configuration.channels:
            if channel.id == channel_id:
                return channel
        raise ValueError("Channel not found.")

    def save(
        self,
        channel: Channel,
        *,
        credentials: Callable[[CredentialRecord], CredentialRecord] | None = None,
        expected_revision: int | None = None,
    ) -> ConfigurationState:
        def change(state):
            channels = list(state.configuration.channels)
            position = next((i for i, item in enumerate(channels) if item.id == channel.id), None)
            replacement = channel.model_copy(deep=True)
            if replacement.disabled and state.configuration.default_channel_id == channel.id:
                raise ValueError("Select another default provider first.")
            if position is None:
                replacement.models.metadata.initial_selection = {
                    "version": 1,
                    "registrationId": str(uuid4()),
                    "status": "ready" if replacement.auth_mode == "oauth" else "pending",
                }
                replacement.models = replacement.models.model_copy(
                    update={"metadata": replacement.models.metadata}
                )
                channels.append(replacement)
            else:
                channels[position] = replacement
            state.configuration.channels = channels
            if not state.configuration.default_channel_id:
                state.configuration.default_channel_id = channel.id
            if state.configuration.default_channel_id == channel.id:
                state.configuration.default_model_id = channel.models.default
            if credentials:
                previous = state.credentials.get(channel.id, CredentialRecord())
                state.credentials[channel.id] = credentials(previous)
            if not replacement.models.live:
                state.configuration = reconcile_discovery(
                    state.configuration, channel.id, replacement.models.static
                )

        state, _ = self.store.update(change, expected_revision=expected_revision)
        return state

    def remove(
        self, channel_id: str, *, expected_revision: int | None = None, allow_empty: bool = False
    ) -> ConfigurationState:
        def change(state):
            self.find(state, channel_id)
            config = state.configuration
            if len(config.channels) == 1 and not allow_empty:
                raise ValueError("Cannot remove the last provider.")
            config.channels = [channel for channel in config.channels if channel.id != channel_id]
            if config.default_channel_id == channel_id:
                default = next(
                    (
                        channel
                        for channel in config.channels
                        if not channel.disabled
                        and (channel.auth_mode != "oauth" or is_chatgpt_subscription(channel))
                    ),
                    None,
                )
                if default is None and not allow_empty:
                    raise ValueError("Enable another provider before removing the default.")
                config.default_channel_id = default.id if default else None
                config.default_model_id = default.models.default if default else None
            config.models = config.models.model_copy(
                update={
                    "custom": [
                        model for model in config.models.custom if model.provider != channel_id
                    ],
                    "disabled": [
                        model
                        for model in config.models.disabled
                        if not model.startswith(channel_id + "/")
                    ],
                    "legacy_disabled": [
                        model
                        for model in config.models.legacy_disabled
                        if not model.startswith(channel_id + "/")
                    ],
                }
            )
            state.credentials.pop(channel_id, None)

        state, _ = self.store.update(change, expected_revision=expected_revision)
        return state

    def set_default(
        self, channel_id: str, *, model_id: str | None = None, expected_revision: int | None = None
    ) -> ConfigurationState:
        def change(state):
            channel = self.find(state, channel_id)
            if channel.disabled:
                raise ValueError("The default provider cannot be disabled.")
            if channel.auth_mode == "oauth" and not is_chatgpt_subscription(channel):
                raise ValueError("This subscription is no longer supported.")
            state.configuration.default_channel_id = channel_id
            state.configuration.default_model_id = model_id or channel.models.default

        state, _ = self.store.update(change, expected_revision=expected_revision)
        return state
