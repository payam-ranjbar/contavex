"""Which implementation serves which (platform, implementation) pair.

This table is the only place that knows the concrete provider classes. An
account picks its pair in configuration; consumers only ever name the
account, so switching implementation never touches them.
"""

from __future__ import annotations

from collections.abc import Iterable

from content_fleet.providers.base import Provider
from content_fleet.providers.instagram.instagram_login import InstagramLogin
from content_fleet.providers.linkedin.rest import LinkedInRest
from content_fleet.providers.telegram.bot_api import TelegramBotApi


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider] = ()) -> None:
        self._providers: dict[tuple[str, str], Provider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: Provider) -> None:
        key = (provider.platform, provider.implementation)
        if key in self._providers:
            raise ValueError(f"provider {key} is already registered")
        self._providers[key] = provider

    def get(self, platform: str, implementation: str) -> Provider | None:
        return self._providers.get((platform, implementation))

    def keys(self) -> list[tuple[str, str]]:
        return sorted(self._providers)


def default_registry(*, staging_available: bool = False) -> ProviderRegistry:
    # To add a platform or a second implementation of one: write the
    # Provider subclass and list it here.
    return ProviderRegistry(
        [
            TelegramBotApi(),
            InstagramLogin(staging_available=staging_available),
            LinkedInRest(),
        ]
    )
