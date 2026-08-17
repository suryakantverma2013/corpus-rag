"""`model_overrides` — the operator's runtime model selection (T-611, R-83).

**Why a table rather than an environment variable.** The six model ids are already one per
job, but every one of them is frozen three ways: `get_settings()` is `@lru_cache`d, the
`ChatClient` and `EmbeddingClient` are process-wide singletons, and `OpenAIChatClient`
copies the ids into instance attributes at construction. So moving the answer model meant
restarting **both** the API and the worker — and those are separate process families, which
is also why an in-process mutable global or a `/reload` endpoint cannot work: the worker
would never see the change. Postgres is the one store both already touch on the synchronous
path, which is R-43(1)'s argument for `processing_locks` over Redis, applied unchanged.

**Env is the default; a row is an override.** An absent row means the value
:class:`~app.config.OpenAISettings` carries, so a deployment that never writes here behaves
exactly as it did before this table existed and `deployment/.env.prod.example` stays the
honest description of a fresh install (R-81(7)). Clearing an override is a `DELETE`, which
is what makes "go back to what the deployment shipped with" expressible at all — a column
holding the current value with no notion of *unset* could only ever be re-typed by hand.

**One row per slot, `slot` as the primary key and a plain string.** `turn_telemetry.outcome`
set the precedent and the reason carries: a CHECK constraint would make adding a slot a
migration on a table nothing else depends on, and there is a known slot coming — embeddings,
once T-608 provides the re-embed path R-83(4) blocks it on. The vocabulary is validated in
:mod:`app.services.model_selection`, where the failure can carry a message naming the legal
values instead of surfacing as an integrity error.

**No foreign key on `updated_by`, and it is text.** The writer is `tools.set_model`, run by
an operator holding database credentials — a principal outside the product's authentication
model entirely, so there is no `users.id` to point at. It records who asked, not who
authenticated, and is a diagnostic rather than the audit trail: NFR-SEC-08's enumerated
event list does not name configuration changes, and R-43(7)'s rule is not to add an
`AuditEventType` member for an event the requirement does not ask for. *Revisit the moment
an authenticated `PUT /api/v1/admin/models` ships — that route has a real administrator
behind it, and an audit row becomes both possible and right.*
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ModelOverride(TimestampMixin, Base):
    __tablename__ = "model_overrides"

    #: One of :class:`~app.services.model_selection.ModelSlot`'s values. Deliberately not an
    #: enum column — see the module docstring.
    slot: Mapped[str] = mapped_column(String(32), primary_key=True)

    #: The provider model id to call for this slot, e.g. `gpt-4o-mini`. Not validated here:
    #: the set of legal ids belongs to the provider and changes without our deploying, so
    #: the check is a live probe at write time (R-83(3)), never a constraint.
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Free text: whoever ran the tool. Diagnostic only — see the module docstring.
    updated_by: Mapped[str | None] = mapped_column(String(255))
