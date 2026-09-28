from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, JSON, Index, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SetupState(Base):
    __tablename__ = 'setup_state'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    initialized: Mapped[bool] = mapped_column(Boolean, default=True)


class Organization(Base):
    __tablename__ = 'organizations'
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200), unique=True)


class Department(Base):
    __tablename__ = 'departments'
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('org_id', 'name'),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(200))


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32))
    org_id: Mapped[str | None] = mapped_column(String(100), index=True)
    team_id: Mapped[str | None] = mapped_column(String(100), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Group(Base):
    __tablename__ = 'groups'
    __table_args__ = (Index('uq_groups_active_external', 'provider', 'external_id', unique=True,
                           postgresql_where=text('archived_at IS NULL')),)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[str | None] = mapped_column(String(36))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    org_id: Mapped[str] = mapped_column(String(100), index=True)
    team_id: Mapped[str | None] = mapped_column(String(100))
    member_ids: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(20), default='web')
    external_id: Mapped[str | None] = mapped_column(String(200))


class Resource(Base):
    __tablename__ = 'resources'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(Text, default='')
    org_id: Mapped[str | None] = mapped_column(String(100), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class Binding(Base):
    __tablename__ = 'bindings'
    __table_args__ = (UniqueConstraint('subject_type', 'subject_id', 'resource_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    subject_type: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey('resources.id'), index=True)


class Conversation(Base):
    __tablename__ = 'conversations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    group_id: Mapped[str | None] = mapped_column(ForeignKey('groups.id'), index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = 'messages'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    message_id: Mapped[str] = mapped_column(ForeignKey('messages.id'))
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Identity(Base):
    __tablename__ = 'identities'
    __table_args__ = (UniqueConstraint('provider', 'external_user_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(20))
    external_user_id: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)


class SessionToken(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class IMSettings(Base):
    __tablename__ = 'im_settings'
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    encrypted: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IMConnection(Base):
    __tablename__ = 'im_connections'
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    transport: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IMDiscovery(Base):
    __tablename__ = 'im_discoveries'
    __table_args__ = (UniqueConstraint('provider', 'app_scope', 'sender_id', 'chat_id', 'chat_type'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(20))
    app_scope: Mapped[str] = mapped_column(String(64), index=True)
    sender_id: Mapped[str] = mapped_column(String(200))
    chat_id: Mapped[str] = mapped_column(String(200))
    chat_type: Mapped[str] = mapped_column(String(10))
    nickname: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(40))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class IMScopeBinding(Base):
    """Pins existing mapping IDs without rewriting legacy identities or groups."""
    __tablename__ = 'im_scope_bindings'
    __table_args__ = (UniqueConstraint('subject_type', 'subject_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    subject_type: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[str] = mapped_column(String(36))
    app_scope: Mapped[str] = mapped_column(String(64))


class IMReaction(Base):
    """Durable outbox for new authorized Feishu messages; no message body/secrets."""
    __tablename__ = 'im_reactions'
    event_id: Mapped[str] = mapped_column(ForeignKey('im_events.id'), primary_key=True)
    message_id: Mapped[str | None] = mapped_column(String(256))
    app_scope: Mapped[str] = mapped_column(String(64))
    reaction_id: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(32), default='pending', index=True)
    error: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IMEvent(Base):
    __tablename__ = 'im_events'
    __table_args__ = (UniqueConstraint('provider', 'event_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(256))
    run_id: Mapped[str | None] = mapped_column(ForeignKey('runs.id'), index=True)
    reply_target: Mapped[dict] = mapped_column(JSON, default=dict)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
