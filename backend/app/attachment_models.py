"""Private attachment metadata, durable leases, and run/message associations."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, uid, now


class Attachment(Base):
    __tablename__ = 'attachments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('conversations.id'), index=True)
    uploader_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey('messages.id'), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey('runs.id'), index=True)
    filename: Mapped[str] = mapped_column(String(240))
    mime: Mapped[str] = mapped_column(String(100), default='application/octet-stream')
    size: Mapped[int] = mapped_column(default=0)
    checksum: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='received', index=True)
    error: Mapped[str | None] = mapped_column(String(300))
    provider: Mapped[str] = mapped_column(String(20), default='web')
    encrypted_reference: Mapped[str | None] = mapped_column(Text)
    app_scope: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class AttachmentJob(Base):
    __tablename__ = 'attachment_jobs'
    attachment_id: Mapped[str] = mapped_column(ForeignKey('attachments.id'), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AttachmentArtifact(Base):
    __tablename__ = 'attachment_artifacts'
    attachment_id: Mapped[str] = mapped_column(ForeignKey('attachments.id'), primary_key=True)
    format_version: Mapped[int] = mapped_column(default=1)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
