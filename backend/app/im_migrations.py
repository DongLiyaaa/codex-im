"""Additive, idempotent migrations serialized across local services."""
from sqlalchemy import select, func, text
from .models import IMReaction, Organization, Department


def migrate(engine):
    with engine.begin() as conn:
        conn.execute(select(func.pg_advisory_xact_lock(71904)))
        from .models import PlatformSettings, PlatformAuthJob, PlatformAuthRequest
        for model in (PlatformSettings, PlatformAuthJob, PlatformAuthRequest):
            model.__table__.create(conn, checkfirst=True)
        IMReaction.__table__.create(conn, checkfirst=True)
        Organization.__table__.create(conn, checkfirst=True)
        Department.__table__.create(conn, checkfirst=True)
        conn.execute(text('ALTER TABLE conversations ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ'))
        conn.execute(text('ALTER TABLE conversations ADD COLUMN IF NOT EXISTS archived_by VARCHAR(36)'))
        conn.execute(text('ALTER TABLE organizations ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ'))
        conn.execute(text('ALTER TABLE departments ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ'))
        conn.execute(text('ALTER TABLE groups ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ'))
        conn.execute(text('ALTER TABLE groups ADD COLUMN IF NOT EXISTS archived_by VARCHAR(36)'))
        conn.execute(text('ALTER TABLE groups DROP CONSTRAINT IF EXISTS groups_provider_external_id_key'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_groups_active_external '
                          'ON groups (provider, external_id) WHERE archived_at IS NULL'))
