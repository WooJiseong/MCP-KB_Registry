PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    next_number INTEGER NOT NULL DEFAULT 1 CHECK(next_number > 0)
);
CREATE TABLE IF NOT EXISTS category_aliases (
    code TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id)
);
CREATE TABLE IF NOT EXISTS id_reservations (
    kb_id TEXT PRIMARY KEY,
    number INTEGER NOT NULL UNIQUE CHECK(number > 0)
);
CREATE TABLE IF NOT EXISTS documents (
    kb_id TEXT PRIMARY KEY REFERENCES id_reservations(kb_id),
    path TEXT NOT NULL UNIQUE,
    category_id INTEGER REFERENCES categories(id),
    category_number TEXT UNIQUE REFERENCES category_reservations(display_id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'committed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK ((category_id IS NULL) = (category_number IS NULL))
);
CREATE TABLE IF NOT EXISTS category_reservations (
    display_id TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    number INTEGER NOT NULL CHECK(number > 0),
    kb_id TEXT NOT NULL REFERENCES id_reservations(kb_id)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    kb_id TEXT NOT NULL REFERENCES documents(kb_id),
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS youtrack_links (
    kb_id TEXT NOT NULL REFERENCES documents(kb_id),
    issue_id TEXT NOT NULL,
    PRIMARY KEY (kb_id, issue_id)
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id TEXT REFERENCES documents(kb_id),
    event TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
PRAGMA user_version = 1;
