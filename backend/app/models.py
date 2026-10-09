from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, JSON, Index, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


# IM-only members are created at approval time without a password. "!" is not a salt:digest pair, so no input can
# ever verify against it; the reserved .invalid domain keeps their placeholder email from ever being deliverable.
LOCKED_PASSWORD = '!'
IM_ONLY_EMAIL_DOMAIN = 'im.invalid'


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
    preferred_agent: Mapped[str | None] = mapped_column(String(20))

    @property
    def login_enabled(self) -> bool:
        return self.password_hash != LOCKED_PASSWORD


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
    # Narrows an organization's resource to one of its departments (kept in its own table so existing databases only
    # need a new table, never an ALTER). Read and written through `team_id`.
    department: Mapped['ResourceDepartment | None'] = relationship(lazy='joined', uselist=False, cascade='all, delete-orphan')

    @property
    def team_id(self):
        return self.department.team_id if self.department else None

    @team_id.setter
    def team_id(self, value):
        self.department = ResourceDepartment(team_id=value) if value else None


class ResourceDepartment(Base):
    __tablename__ = 'resource_departments'
    resource_id: Mapped[str] = mapped_column(ForeignKey('resources.id'), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(100), index=True)


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
    agent: Mapped[str] = mapped_column(String(20), default='codex', server_default='codex')
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
    agent: Mapped[str] = mapped_column(String(20), default='codex', server_default='codex')
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
    # DingTalk staff ids are only unique inside one organization: the robot's corpId, recorded from the person's own
    # internal messages, qualifies the id when a personal authorization names its account. Unused for Feishu.
    corp_id: Mapped[str | None] = mapped_column(String(200))


class SessionToken(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PlatformConnection(Base):
    __tablename__ = 'platform_connections'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default='disconnected')
    encrypted: Mapped[str] = mapped_column(Text, default='')
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlatformAuthJob(Base):
    __tablename__ = 'platform_auth_jobs'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    generation: Mapped[str] = mapped_column(String(36), default=uid)
    phase: Mapped[str] = mapped_column(String(32), default='begin', index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    identity_id: Mapped[str | None] = mapped_column(String(36))
    source_run_id: Mapped[str | None] = mapped_column(String(36))
    im_fingerprint: Mapped[str | None] = mapped_column(String(64))
    app_scope: Mapped[str | None] = mapped_column(String(64))
    delivery: Mapped[str] = mapped_column(String(32), default='web')
    notification: Mapped[str] = mapped_column(String(32), default='pending')
    error: Mapped[str | None] = mapped_column(String(64))


class PlatformAuthRequest(Base):
    __tablename__ = 'platform_auth_requests'
    __table_args__ = (UniqueConstraint('run_id', 'provider'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str] = mapped_column(ForeignKey('messages.id'), index=True)


class PlatformSettings(Base):
    __tablename__ = 'platform_settings'
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    encrypted: Mapped[str] = mapped_column(Text)


class PlatformAccess(Base):
    """Which Hub users may authorize a platform in person. Kept in Hub because DingTalk's own CLI 可用人员 list has no
    API; with the platform open to everyone, this is the list that decides. No row means everyone."""
    __tablename__ = 'platform_access'
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    user_scope: Mapped[str] = mapped_column(String(20), default='all')
    user_ids: Mapped[list] = mapped_column(JSON, default=list)
    updated_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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


class IMOutbox(Base):
    """Durable replies to IM commands; sent by the API outbox thread and never replayed once ambiguous."""
    __tablename__ = 'im_outbox'
    event_id: Mapped[str] = mapped_column(ForeignKey('im_events.id'), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlatformApproval(Base):
    """A risky platform action that only the requester's own chat message can release.

    The whole request is kept server-side, so executing it never depends on the model repeating it
    exactly, and the model cannot alter it after the user approved. Terminal states clear the payload.
    """
    __tablename__ = 'platform_approvals'
    __table_args__ = (UniqueConstraint('user_id', 'code'), Index('ix_platform_approvals_user_state', 'user_id', 'state'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(12))
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    operation: Mapped[str] = mapped_column(String(20))
    digest: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(16), default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IMOnboardingPolicy(Base):
    """Opt-in rule: verified same-organization private senders become members without an administrator click.

    Always off until an administrator saves it; the role is fixed to member and never stored here.
    """
    __tablename__ = 'im_onboarding_policies'
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    org_id: Mapped[str | None] = mapped_column(String(100))
    team_id: Mapped[str | None] = mapped_column(String(100))
    daily_cap: Mapped[int] = mapped_column(default=20)
    updated_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IMChatName(Base):
    """Display-only names of external group chats; never an authorization source."""
    __tablename__ = 'im_chat_names'
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    app_scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


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
