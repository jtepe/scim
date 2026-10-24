
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS settings (
 id INTEGER PRIMARY KEY CHECK(id=1), base_url TEXT NOT NULL DEFAULT '',
 token TEXT NOT NULL DEFAULT '',
 user_extension TEXT NOT NULL DEFAULT 'urn:example:scim:schemas:extension:company:2.0:User',
 group_extension TEXT NOT NULL DEFAULT 'urn:example:scim:schemas:extension:company:2.0:Group'
);
INSERT OR IGNORE INTO settings(id) VALUES(1);
CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 username TEXT NOT NULL UNIQUE, given_name TEXT NOT NULL, family_name TEXT NOT NULL,
 email TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 remote_id TEXT UNIQUE, revision INTEGER NOT NULL DEFAULT 1,
 synced_revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS groups (
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 display_name TEXT NOT NULL, remote_id TEXT UNIQUE,
 revision INTEGER NOT NULL DEFAULT 1, synced_revision INTEGER NOT NULL DEFAULT 0,
 UNIQUE(company_id, display_name)
);
CREATE TABLE IF NOT EXISTS memberships (
 user_id TEXT REFERENCES users(id), group_id TEXT REFERENCES groups(id),
 PRIMARY KEY(user_id,group_id)
);
CREATE TRIGGER IF NOT EXISTS membership_company BEFORE INSERT ON memberships
WHEN (SELECT company_id FROM users WHERE id=NEW.user_id) !=
     (SELECT company_id FROM groups WHERE id=NEW.group_id)
BEGIN SELECT RAISE(ABORT, 'Cross-company membership is forbidden'); END;
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, started TEXT DEFAULT CURRENT_TIMESTAMP,
 mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running', error TEXT
);
CREATE TABLE IF NOT EXISTS journal (
 id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id),
 created TEXT DEFAULT CURRENT_TIMESTAMP, method TEXT, url TEXT,
 request TEXT, response TEXT, status INTEGER, duration_ms INTEGER, attempt INTEGER
);
