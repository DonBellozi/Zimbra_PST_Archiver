PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=30000;

CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(80) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL);
CREATE TABLE settings (key VARCHAR(120) PRIMARY KEY, value TEXT NOT NULL DEFAULT '', encrypted BOOLEAN NOT NULL DEFAULT 0);
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account VARCHAR(320) NOT NULL,
  status VARCHAR(18) NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','PREPARING_TGZ','TGZ_READY','CHECKING_SPACE','DOWNLOADING','CONVERTING','VERIFYING','COMPLETED','WAITING_FOR_SPACE','STORED_ON_NAS','FAILED','CANCELLED','EXPIRED')),
  resume_status VARCHAR(40), progress INTEGER NOT NULL DEFAULT 0,
  mailbox_bytes BIGINT, tgz_bytes BIGINT, pst_bytes BIGINT, message_count BIGINT,
  remote_tgz TEXT, local_tgz TEXT, nas_tgz TEXT, pst_path TEXT, error TEXT,
  cancel_requested BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL, completed_at DATETIME, expires_at DATETIME
);
CREATE INDEX ix_jobs_status_created ON jobs(status,created_at);
CREATE INDEX ix_jobs_account ON jobs(account);
CREATE INDEX ix_jobs_expires ON jobs(expires_at);
CREATE TABLE job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, status VARCHAR(40) NOT NULL, message TEXT NOT NULL DEFAULT '', created_at DATETIME NOT NULL);
CREATE INDEX ix_job_events_job_id ON job_events(job_id);
