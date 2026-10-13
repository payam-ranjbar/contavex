"""Instagram publishing through "Instagram API with Facebook Login".

The second implementation of the `instagram` platform. It talks to the same
content-publishing endpoints as the Instagram Login variant, on
`graph.facebook.com`, and so reuses that state machine unchanged. What
differs is what an operator has to set up, and what the API then allows:

* The Instagram professional account must be connected to a Facebook Page,
  and the Meta app uses the Facebook Login setup (an app has one setup, so
  this is a different Meta app from an Instagram Login one).
* The credential is a Facebook access token. A Page access token derived
  from a long-lived user token does not expire, so nothing is refreshed here.
* **Published posts can be deleted**, with the `instagram_manage_contents`
  permission. That is why this variant exists in this service: verified live
  on 2026-10-11, the Instagram Login API answers a delete with code 100/33.

Consumers address an account, not an implementation, so moving an account
from one variant to the other changes nothing for them except that the
account's capabilities now say `deletable: true`.
"""

from __future__ import annotations

from content_fleet.config import AccountConfig
from content_fleet.core.capabilities import Capabilities
from content_fleet.core.errors import ErrorCategory, PublishError
from content_fleet.core.models import PublicationResult
from content_fleet.credentials import CredentialState, RefreshedCredential
from content_fleet.providers.base import ProviderContext
from content_fleet.providers.instagram.instagram_login import InstagramLogin


class InstagramFacebookLogin(InstagramLogin):
    implementation = "facebook-login"
    api_base = "https://graph.facebook.com"
    # A Page token does not expire and a user token cannot be renewed without
    # a person, so there is nothing for the service to refresh.
    mutable_credentials = frozenset()

    def capabilities(self, account: AccountConfig) -> Capabilities:
        return super().capabilities(account).model_copy(update={"deletable": True})

    async def refresh_credentials(
        self, ctx: ProviderContext, states: dict[str, CredentialState], now: float
    ) -> list[RefreshedCredential]:
        return []

    async def delete(self, ctx: ProviderContext, result: PublicationResult) -> None:
        if not result.posts:
            raise PublishError(
                ErrorCategory.UNSUPPORTED,
                "Instagram returned no media id for this post, so it cannot be deleted here",
            )
        # A carousel is deleted as a whole through its own id, which is the
        # one post id a publication holds.
        body = await self._request(ctx, "DELETE", f"/{result.posts[0].id}", "delete_media")
        if body.get("success") is not True:
            raise PublishError(
                ErrorCategory.MALFORMED_PROVIDER_RESPONSE,
                "Instagram did not confirm the deletion",
            )
