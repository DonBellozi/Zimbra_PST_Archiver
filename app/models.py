import enum
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, BigInteger, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

def now(): return datetime.now(timezone.utc)

class JobStatus(str, enum.Enum):
    QUEUED="QUEUED"; PREPARING_TGZ="PREPARING_TGZ"; TGZ_READY="TGZ_READY"
    CHECKING_SPACE="CHECKING_SPACE"; DOWNLOADING="DOWNLOADING"; CONVERTING="CONVERTING"
    VERIFYING="VERIFYING"; COMPLETED="COMPLETED"; WAITING_FOR_SPACE="WAITING_FOR_SPACE"
    STORED_ON_NAS="STORED_ON_NAS"; FAILED="FAILED"; CANCELLED="CANCELLED"; EXPIRED="EXPIRED"

class User(Base):
    __tablename__="users"
    id: Mapped[int]=mapped_column(primary_key=True)
    username: Mapped[str]=mapped_column(String(80), unique=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)

class Setting(Base):
    __tablename__="settings"
    key: Mapped[str]=mapped_column(String(120), primary_key=True)
    value: Mapped[str]=mapped_column(Text, default="")
    encrypted: Mapped[bool]=mapped_column(Boolean, default=False)

class Job(Base):
    __tablename__="jobs"
    id: Mapped[int]=mapped_column(primary_key=True)
    account: Mapped[str]=mapped_column(String(320), index=True)
    status: Mapped[JobStatus]=mapped_column(Enum(JobStatus), default=JobStatus.QUEUED, index=True)
    resume_status: Mapped[str|None]=mapped_column(String(40))
    progress: Mapped[int]=mapped_column(Integer, default=0)
    mailbox_bytes: Mapped[int|None]=mapped_column(BigInteger)
    tgz_bytes: Mapped[int|None]=mapped_column(BigInteger)
    pst_bytes: Mapped[int|None]=mapped_column(BigInteger)
    message_count: Mapped[int|None]=mapped_column(BigInteger)
    remote_tgz: Mapped[str|None]=mapped_column(Text)
    local_tgz: Mapped[str|None]=mapped_column(Text)
    nas_tgz: Mapped[str|None]=mapped_column(Text)
    pst_path: Mapped[str|None]=mapped_column(Text)
    error: Mapped[str|None]=mapped_column(Text)
    cancel_requested: Mapped[bool]=mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    completed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), index=True)

class JobEvent(Base):
    __tablename__="job_events"
    id: Mapped[int]=mapped_column(primary_key=True)
    job_id: Mapped[int]=mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    status: Mapped[str]=mapped_column(String(40))
    message: Mapped[str]=mapped_column(Text, default="")
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=now)
