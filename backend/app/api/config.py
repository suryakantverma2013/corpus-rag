"""``/config`` — the deployment settings the GUI renders (T-513, FR-SYS-03 / FR-ANL-02).

**Why this route exists at all.** FR-SYS-03 says the stats panel displays *"the configured
OpenAI model id"*, and until now nothing on the wire carried it: `MeResponse` describes the
caller, and `MessageResponse.model_name` is per *answer* — so a chat that has never been
answered had nothing to read and the GUI fell back to a hand-kept copy of the backend default,
which is the silent-drift shape this codebase rejects everywhere else.

**Why it is not a field on `MeResponse`.** That model is the caller's own profile. A
product-wide operator setting riding on it would be a category error, and the next such value
would have to ride on it too. One route whose whole subject is configuration is the honest
home, and it costs one request behind the same boot probe every surface already waits for.

**Not per-answer provenance.** `MessageResponse.model_name` stays the record of what actually
answered a given turn, and the GUI prefers it where it exists — this is what is *configured
now*, which is the only thing an empty chat can truthfully show.

Authenticated like every other route: this is internal product configuration, not a public
discovery document, and there is no reason for an anonymous caller to learn it. Nothing
secret goes here — a key, an endpoint or a credential would be a defect, not an extension.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth.dependencies import CurrentUser, SettingsDep

router = APIRouter(prefix="/config", tags=["config"])


class ConfigResponse(BaseModel):
    """Deployment configuration the GUI renders. One field today, by design."""

    chat_model: str = Field(
        description=(
            "The configured answer model (`OPENAI_CHAT_MODEL`), for FR-ANL-02's MODEL card. "
            "The *configured* id, not the one that answered any particular turn — that is "
            "`MessageResponse.model_name`, which the GUI prefers where it exists."
        )
    )


@router.get("", response_model=ConfigResponse, summary="Deployment configuration")
async def get_config(user: CurrentUser, settings: SettingsDep) -> ConfigResponse:
    """FR-SYS-03's model id.

    `user` is unused and is the point: it is the dependency that makes this authenticated.
    """
    return ConfigResponse(chat_model=settings.openai.chat_model)
