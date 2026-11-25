"""SQLite index and search APIs for parsed Claude sessions.

Search uses plain ``LIKE`` matching with ``%``, ``_`` and the escape character
escaped so user terms match literally. FTS5 is intentionally not used, so the
index works with any SQLite build.
"""
import os
from pathlib import Path
import sqlite3
import urllib.parse

from .parser import claude_root, discover_sessions, parse_session

# Single source of truth for hiding Director worktree sessions. It is applied in
# SQL before LIMIT so hidden rows never use up a result limit. Literal '_' is
# escaped with ESCAPE '\\' and '\\\\' in a pattern matches one literal backslash.
# Patterns match .director_worktrees only as a whole path segment.
_WORKTREE_EXCLUDE_SQL = r"""NOT (
    COALESCE(project_path, '') LIKE '%/.director\_worktrees/%' ESCAPE '\'
    OR COALESCE(project_path, '') LIKE '%/.director\_worktrees' ESCAPE '\'
    OR COALESCE(project_path, '') LIKE '%\\.director\_worktrees\\%' ESCAPE '\'
    OR COALESCE(project_path, '') LIKE '%\\.director\_worktrees' ESCAPE '\'
    OR COALESCE(project_path, '') LIKE '.director\_worktrees/%' ESCAPE '\'
    OR COALESCE(project_path, '') LIKE '.director\_worktrees\\%' ESCAPE '\'
    OR COALESCE(project_path, '') = '.director_worktrees'
    OR COALESCE(project_dir, '') = '--director-worktrees'
    OR COALESCE(project_dir, '') LIKE '%--director-worktrees' ESCAPE '\'
    OR COALESCE(project_dir, '') LIKE '%--director-worktrees-%' ESCAPE '\'
    OR COALESCE(project_dir, '') LIKE '%-.director\_worktrees' ESCAPE '\'
    OR COALESCE(project_dir, '') LIKE '%-.director\_worktrees-%' ESCAPE '\'
    OR COALESCE(project_dir, '') LIKE '%--director\_worktrees' ESCAPE '\'
    OR COALESCE(project_dir, '') LIKE '%--director\_worktrees-%' ESCAPE '\'
    OR COALESCE(file_path, '') LIKE '%/.director\_worktrees/%' ESCAPE '\'
    OR COALESCE(file_path, '') LIKE '%/.director\_worktrees' ESCAPE '\'
    OR COALESCE(file_path, '') LIKE '%\\.director\_worktrees\\%' ESCAPE '\'
    OR COALESCE(file_path, '') LIKE '%\\.director\_worktrees' ESCAPE '\'
    OR COALESCE(file_path, '') LIKE '.director\_worktrees/%' ESCAPE '\'
    OR COALESCE(file_path, '') LIKE '.director\_worktrees\\%' ESCAPE '\'
    OR COALESCE(file_path, '') = '.director_worktrees'
)"""


def default_db_path(path=None):
    if path is not None:
        return Path(path).expanduser()
    return Path(os.environ.get("SHM_DB_PATH", "~/.claude/session-history.sqlite3")).expanduser()


def _like_escape(term):
    """Escape LIKE wildcards so ``term`` matches literally with ``ESCAPE '\\'``."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SessionIndex:
    def __init__(self, db_path=None, *, read_only=False):
        self.db_path = default_db_path(db_path)
        if read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(
                    "Database not found at {}; run `python -m shm index`.".format(self.db_path))
            uri = "file:{}?mode=ro".format(urllib.parse.quote(str(self.db_path)))
            self.connection = sqlite3.connect(uri, uri=True)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        if not read_only:
            self._setup()

    def _setup(self):
        # Older schemas keyed rows by session_id, which collides when two projects
        # share a session file stem. Drop such a table; it is rebuilt keyed by file_path
        # and repopulated by the next index run.
        info = list(self.connection.execute("PRAGMA table_info(sessions)"))
        if any(row["name"] == "session_id" and row["pk"] for row in info):
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
        """Parse session files under ``root`` and store their metadata.

        With ``incremental`` set, files whose mtime and size are unchanged are
        skipped. Rows for files that disappeared from ``root`` are removed. Source
        files are only read. Returns counts of indexed, skipped and removed files.
        """
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

    def list_sessions(self, project=None, limit=None, *, exclude_worktrees=False):
        """Return sessions newest first, optionally filtered by project.

        ``project`` matches project_dir or project_path. ``exclude_worktrees``
        hides Director worktree sessions in SQL before ``limit`` is applied.
        """
        clauses, args = [], []
        if project is not None:
            clauses.append("(project_dir=? OR project_path=?)")
            args.extend((project, project))
        if exclude_worktrees:
            clauses.append(_WORKTREE_EXCLUDE_SQL)
        sql = "SELECT * FROM sessions" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY last_timestamp DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(row) for row in self.connection.execute(sql, args)]

    def find_sessions(self, prefix, *, exclude_worktrees=False):
        """Return sessions whose id starts with ``prefix``; an exact id match wins.

        The prefix is compared with substr, so LIKE wildcards are not special.
        ``exclude_worktrees`` hides Director worktree sessions in SQL.
        """
        where = "substr(session_id, 1, ?) = ?"
        args = [len(prefix), prefix]
        if exclude_worktrees:
            where += " AND " + _WORKTREE_EXCLUDE_SQL
        rows = [dict(row) for row in self.connection.execute(
            "SELECT * FROM sessions WHERE " + where + " ORDER BY last_timestamp DESC", args)]
        exact = [row for row in rows if row["session_id"] == prefix]
        return exact or rows

    def search(self, query, project=None, limit=100, *, exclude_worktrees=False):
        """Return sessions whose title or user text contains every query term.

        Terms are whitespace-separated and matched literally with LIKE (no FTS5),
        newest first. ``project`` matches project_dir or project_path, and
        ``exclude_worktrees`` hides Director worktree sessions before the LIMIT.
        """
        terms = [part for part in query.split() if part]
        if not terms:
            return []
        where = " AND ".join("(title LIKE ? ESCAPE '\\' OR user_text LIKE ? ESCAPE '\\')" for _ in terms)
        args = []
        for term in terms:
            pattern = "%" + _like_escape(term) + "%"
            args.extend((pattern, pattern))
        clauses = ["(" + where + ")"]
        if project is not None:
            clauses.append("(project_dir=? OR project_path=?)")
            args.extend((project, project))
        if exclude_worktrees:
            clauses.append(_WORKTREE_EXCLUDE_SQL)
        args.append(int(limit))
        sql = "SELECT * FROM sessions WHERE " + " AND ".join(clauses) + " ORDER BY last_timestamp DESC LIMIT ?"
        return [dict(row) for row in self.connection.execute(sql, args)]


def index_sessions(root=None, db_path=None, incremental=False):
    with SessionIndex(db_path) as index:
        return index.index(root, incremental)
