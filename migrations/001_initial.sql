CREATE TYPE job_status AS ENUM ('QUEUED','PREPARING_TGZ','TGZ_READY','CHECKING_SPACE','DOWNLOADING','CONVERTING','VERIFYING','COMPLETED','WAITING_FOR_SPACE','STORED_ON_NAS','FAILED','CANCELLED','EXPIRED');
CREATE TABLE users (id bigserial PRIMARY KEY, username varchar(80) UNIQUE NOT NULL, password_hash varchar(255) NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE settings (key varchar(120) PRIMARY KEY, value text NOT NULL DEFAULT '', encrypted boolean NOT NULL DEFAULT false);
CREATE TABLE jobs (id bigserial PRIMARY KEY, account varchar(320) NOT NULL, status job_status NOT NULL DEFAULT 'QUEUED', resume_status varchar(40), progress integer NOT NULL DEFAULT 0, mailbox_bytes bigint, tgz_bytes bigint, pst_bytes bigint, message_count bigint, remote_tgz text, local_tgz text, nas_tgz text, pst_path text, error text, cancel_requested boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz, expires_at timestamptz);
CREATE INDEX ix_jobs_status_created ON jobs(status,created_at);
CREATE INDEX ix_jobs_account ON jobs(account);
CREATE INDEX ix_jobs_expires ON jobs(expires_at);
CREATE TABLE job_events (id bigserial PRIMARY KEY, job_id bigint NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, status varchar(40) NOT NULL, message text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_job_events_job_id ON job_events(job_id);

