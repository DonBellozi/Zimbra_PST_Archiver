-- SQLite, additive and idempotent. Applied automatically through SQLAlchemy
-- initialize_schema() at startup under BEGIN IMMEDIATE. No changes to old jobs.
CREATE TABLE IF NOT EXISTS imap_migrations (
 id VARCHAR(32) NOT NULL PRIMARY KEY, batch VARCHAR(64) NOT NULL,
 account VARCHAR(320) NOT NULL, target VARCHAR(320) NOT NULL, secret TEXT NOT NULL,
 host VARCHAR(255) NOT NULL, port INTEGER NOT NULL, tls VARCHAR(16) NOT NULL,
 storage TEXT NOT NULL, use_nas BOOLEAN NOT NULL,
 want_tgz BOOLEAN NOT NULL, want_pst BOOLEAN NOT NULL,
 status VARCHAR(40) NOT NULL, stage TEXT NOT NULL, error TEXT,
 cancel BOOLEAN NOT NULL, scanned BOOLEAN NOT NULL,
 scan_generation INTEGER NOT NULL, export_generation INTEGER NOT NULL,
 total INTEGER NOT NULL, downloaded INTEGER NOT NULL,
 total_bytes BIGINT NOT NULL, downloaded_bytes BIGINT NOT NULL,
 tgz_path TEXT, pst_path TEXT, tgz_sha256 VARCHAR(64), pst_sha256 VARCHAR(64),
 tgz_status VARCHAR(40) NOT NULL, pst_status VARCHAR(40) NOT NULL,
 retries INTEGER NOT NULL, next_try DATETIME, created_at DATETIME NOT NULL,
 pst_expires DATETIME, UNIQUE(batch, account)
);
CREATE INDEX IF NOT EXISTS ix_imap_migrations_batch ON imap_migrations(batch);
CREATE INDEX IF NOT EXISTS ix_imap_migrations_status ON imap_migrations(status);
CREATE TABLE IF NOT EXISTS imap_messages (
 id INTEGER NOT NULL PRIMARY KEY, migration_id VARCHAR(32) NOT NULL,
 folder_key VARCHAR(64) NOT NULL, folder TEXT NOT NULL, target_folder TEXT NOT NULL,
 validity BIGINT NOT NULL, uid BIGINT NOT NULL, received_at BIGINT NOT NULL,
 size BIGINT NOT NULL, flags TEXT NOT NULL, generation INTEGER NOT NULL,
 sha256 VARCHAR(64), relative_path TEXT NOT NULL,
 UNIQUE(migration_id, folder_key, validity, uid),
 FOREIGN KEY(migration_id) REFERENCES imap_migrations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_imap_messages_migration_id ON imap_messages(migration_id);
CREATE TABLE IF NOT EXISTS imap_artifacts (
 path TEXT NOT NULL PRIMARY KEY, migration_id VARCHAR(32) NOT NULL,
 expires_at DATETIME NOT NULL,
 FOREIGN KEY(migration_id) REFERENCES imap_migrations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_imap_artifacts_migration_id ON imap_artifacts(migration_id);
