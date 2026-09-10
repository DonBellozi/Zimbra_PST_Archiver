"""Additive schema: existing SSH jobs and their history are unchanged."""
import uuid
from sqlalchemy import String, Text, Integer, BigInteger, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .models import now


class Migration(Base):
    __tablename__ = 'imap_migrations'
    __table_args__ = (UniqueConstraint('batch', 'account'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    batch: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(320))
    target: Mapped[str] = mapped_column(String(320))
    secret: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    tls: Mapped[str] = mapped_column(String(16))
    storage: Mapped[str] = mapped_column(Text)
    use_nas: Mapped[bool] = mapped_column(Boolean)
    want_tgz: Mapped[bool] = mapped_column(Boolean)
    want_pst: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(40), default='QUEUED', index=True)
    stage: Mapped[str] = mapped_column(Text, default='Ожидает запуска')
    error: Mapped[str | None] = mapped_column(Text)
    cancel: Mapped[bool] = mapped_column(Boolean, default=False)
    scanned: Mapped[bool] = mapped_column(Boolean, default=False)
    scan_generation: Mapped[int] = mapped_column(Integer, default=0)
    export_generation: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[int] = mapped_column(Integer, default=0)
    downloaded: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    downloaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tgz_path: Mapped[str | None] = mapped_column(Text)
    pst_path: Mapped[str | None] = mapped_column(Text)
    tgz_sha256: Mapped[str | None] = mapped_column(String(64))
    pst_sha256: Mapped[str | None] = mapped_column(String(64))
    tgz_status: Mapped[str] = mapped_column(String(40), default='PENDING')
    pst_status: Mapped[str] = mapped_column(String(40), default='PENDING')
    retries: Mapped[int] = mapped_column(Integer, default=0)
    next_try: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    pst_expires: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class MigrationMessage(Base):
    __tablename__ = 'imap_messages'
    __table_args__ = (UniqueConstraint('migration_id', 'folder_key', 'validity', 'uid'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    migration_id: Mapped[str] = mapped_column(ForeignKey('imap_migrations.id', ondelete='CASCADE'), index=True)
    folder_key: Mapped[str] = mapped_column(String(64))
    folder: Mapped[str] = mapped_column(Text)
    target_folder: Mapped[str] = mapped_column(Text)
    validity: Mapped[int] = mapped_column(BigInteger)
    uid: Mapped[int] = mapped_column(BigInteger)
    received_at: Mapped[int] = mapped_column(BigInteger)
    size: Mapped[int] = mapped_column(BigInteger)
    flags: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    relative_path: Mapped[str] = mapped_column(Text)


class MigrationArtifact(Base):
    __tablename__ = 'imap_artifacts'
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    migration_id: Mapped[str] = mapped_column(ForeignKey('imap_migrations.id', ondelete='CASCADE'), index=True)
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))
