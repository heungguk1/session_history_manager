"""SQLite index and search APIs for parsed Claude sessions.

Search uses SQL LIKE only (no FTS5 dependency), so it works on any SQLite build.
"""
import os
from pathlib import Path
import sqlite3

from .parser import claude_root, discover_sessions, parse_session


def default_db_path(path=None):
    if path is not None:
        return Path(path).expanduser()
    return Path(os.environ.get("SHM_DB_PATH", "~/.claude/session-history.sqlite3")).expanduser()


class SessionIndex:
    def __init__(self, db_path=None):
        self.db_path = default_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        self._setup()

    def _setup(self):
        info = list(self.connection.execute("PRAGMA table_info(sessions)"))
        if any(row["name"] == "session_id" and row["pk"] for row in info):
            # Older schema keyed on session_id; the index is a rebuildable cache.
            self.connection.execute("DROP TABLE sessions")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT NOT NULL, project_dir TEXT NOT NULL, project_path TEXT,
            file_path TEXT PRIMARY KEY, first_timestamp TEXT, last_timestamp TEXT,
            message_count INTEGER NOT NULL, user_message_count INTEGER NOT NULL,
            title TEXT NOT NULL, user_text TEXT NOT NULL, file_mtime REAL NOT NULL,
            file_size INTEGER NOT NULL, malformed_lines INTEGER NOT NULL DEFAULT 0
        )""")
        self.connection.execute("CREATE INDEX IF NOT EXISTS sessions_last ON sessions(last_timestamp)")
        self.connection.commit()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _store(self, meta):
        self.connection.execute("""INSERT INTO sessions (
            session_id, project_dir, project_path, file_path, first_timestamp,
            last_timestamp, message_count, user_message_count, title, user_text,
            file_mtime, file_size, malformed_lines) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(file_path) DO UPDATE SET
            session_id=excluded.session_id, project_dir=excluded.project_dir,
            project_path=excluded.project_path, first_timestamp=excluded.first_timestamp,
            last_timestamp=excluded.last_timestamp, message_count=excluded.message_count,
            user_message_count=excluded.user_message_count, title=excluded.title,
            user_text=excluded.user_text, file_mtime=excluded.file_mtime,
            file_size=excluded.file_size, malformed_lines=excluded.malformed_lines
        """, (meta.session_id, meta.project_dir, meta.project_path, meta.file_path,
              meta.first_timestamp, meta.last_timestamp, meta.message_count,
              meta.user_message_count, meta.title, meta.user_text, meta.file_mtime,
              meta.file_size, meta.malformed_lines))

    def index(self, root=None, incremental=False):
        """Index files. Incremental mode skips unchanged size/mtime pairs."""
        paths = list(discover_sessions(root))
        existing = {row["file_path"]: (row["file_mtime"], row["file_size"])
                    for row in self.connection.execute("SELECT file_path,file_mtime,file_size FROM sessions")}
        seen = set()
        indexed = skipped = removed = 0
        for path in paths:
            file_path = str(path)
            seen.add(file_path)
            stat = path.stat()
            if incremental and existing.get(file_path) == (stat.st_mtime, stat.st_size):
                skipped += 1
                continue
            self._store(parse_session(path))
            indexed += 1
        roots = {str(claude_root(root))}
        for file_path in existing:
            if file_path not in seen and str(Path(file_path).parent.parent) in roots:
                self.connection.execute("DELETE FROM sessions WHERE file_path=?", (file_path,))
                removed += 1
        self.connection.commit()
        return {"indexed": indexed, "skipped": skipped, "removed": removed}

    def list_sessions(self, project=None):
        sql = "SELECT * FROM sessions"
        args = ()
        if project is not None:
            sql += " WHERE project_dir=? OR project_path=?"
            args = (project, project)
        sql += " ORDER BY last_timestamp DESC"
        return [dict(row) for row in self.connection.execute(sql, args)]

    def search(self, query, project=None, limit=100):
        """Keyword search (all terms must match) over title and user text via LIKE."""
        terms = [part for part in query.split() if part]
        if not terms:
            return []
        where = " AND ".join("(title LIKE ? OR user_text LIKE ?)" for _ in terms)
        args = []
        for term in terms:
            pattern = "%" + term + "%"
            args.extend((pattern, pattern))
        if project is not None:
            where = "(" + where + ") AND (project_dir=? OR project_path=?)"
            args.extend((project, project))
        args.append(int(limit))
        sql = "SELECT * FROM sessions WHERE " + where + " ORDER BY last_timestamp DESC LIMIT ?"
        return [dict(row) for row in self.connection.execute(sql, args)]


def index_sessions(root=None, db_path=None, incremental=False):
    with SessionIndex(db_path) as index:
        return index.index(root, incremental)
