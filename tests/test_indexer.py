import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shm.indexer import SessionIndex, default_db_path


def write_records(root, project, session_id, records):
    directory = Path(root) / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (session_id + ".jsonl")
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def user(prompt, timestamp, project="p"):
    return {"type": "user", "cwd": "/work/" + project, "timestamp": timestamp, "message": {"content": prompt}}


def write_session(root, project, session_id, prompt, timestamp):
    return write_records(root, project, session_id, [user(prompt, timestamp, project)])


def ids(rows):
    return [r["session_id"] for r in rows]


class IndexerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sessions"
        self.db = Path(self.temp.name) / "index.sqlite3"

    def test_full_incremental_update_delete_listing_search_and_read_only(self):
        alpha = write_session(self.root, "alpha", "one", "findable apricot", "2024-01-01T00:00:00Z")
        beta = write_session(self.root, "beta", "two", "other topic", "2024-01-02T00:00:00Z")
        original = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (alpha, beta)}
        tree = set(self.root.rglob("*"))
        with SessionIndex(self.db) as index:
            stats = index.index(self.root)
            self.assertEqual(stats, {"indexed": 2, "skipped": 0, "removed": 0})
            stats = index.index(self.root, incremental=True)
            self.assertEqual(stats, {"indexed": 0, "skipped": 2, "removed": 0})
            # Read-only: sources untouched and no new files in the session tree.
            self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (alpha, beta)}, original)
            self.assertEqual(set(self.root.rglob("*")), tree)
            self.assertEqual(ids(index.list_sessions()), ["two", "one"])
            self.assertEqual(ids(index.list_sessions(project="alpha")), ["one"])
            self.assertEqual(ids(index.list_sessions(project="/work/beta")), ["two"])
            self.assertEqual(ids(index.search("apricot")), ["one"])
            self.assertEqual(ids(index.search("other")), ["two"])
            old_ns = alpha.stat().st_mtime_ns
            alpha.write_text(json.dumps(user("changed keyword, much longer content", "2024-01-03T00:00:00Z", "alpha")) + "\n", encoding="utf-8")
            os.utime(alpha, ns=(old_ns + 5000000000, old_ns + 5000000000))
            stats = index.index(self.root, incremental=True)
            self.assertEqual((stats["indexed"], stats["skipped"]), (1, 1))
            self.assertEqual(ids(index.search("changed")), ["one"])
            self.assertEqual(index.search("apricot"), [])
            self.assertEqual(ids(index.list_sessions()), ["one", "two"])
            beta.unlink()
            stats = index.index(self.root, incremental=True)
            self.assertEqual(stats, {"indexed": 0, "skipped": 1, "removed": 1})
            self.assertEqual(ids(index.list_sessions()), ["one"])

    def test_search_treats_like_wildcards_as_literals(self):
        write_session(self.root, "p", "percent", "exact 100%", "2024-01-01T00:00:00Z")
        write_session(self.root, "q", "thousand", "exact 1000", "2024-01-02T00:00:00Z")
        write_session(self.root, "r", "under", "under_score", "2024-01-03T00:00:00Z")
        write_session(self.root, "s", "underx", "underXscore", "2024-01-04T00:00:00Z")
        write_session(self.root, "t", "slash", "back\\slash", "2024-01-05T00:00:00Z")
        with SessionIndex(self.db) as index:
            index.index(self.root)
            self.assertEqual(ids(index.search("100%")), ["percent"])
            self.assertEqual(ids(index.search("under_score")), ["under"])
            self.assertEqual(ids(index.search("back\\slash")), ["slash"])
            self.assertEqual(ids(index.search("1000")), ["thousand"])

    def test_timestamps_normalize_offsets_and_sort_chronologically(self):
        # 02:00+02:00 is 00:00Z, so it is chronologically before 01:30Z.
        write_session(self.root, "p", "offset_first", "offset", "2024-01-01T02:00:00+02:00")
        write_session(self.root, "q", "utc_second", "utc", "2024-01-01T01:30:00Z")
        with SessionIndex(self.db) as index:
            index.index(self.root)
            self.assertEqual(ids(index.list_sessions()), ["utc_second", "offset_first"])
            self.assertEqual(index.list_sessions()[1]["last_timestamp"], "2024-01-01T00:00:00+00:00")
            self.assertEqual(ids(index.list_sessions(limit=1)), ["utc_second"])

    def test_migrates_session_id_primary_key_schema(self):
        db = sqlite3.connect(str(self.db))
        db.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_dir TEXT, project_path TEXT, file_path TEXT, first_timestamp TEXT, last_timestamp TEXT, message_count INTEGER, user_message_count INTEGER, title TEXT, user_text TEXT, file_mtime REAL, file_size INTEGER)")
        db.execute("INSERT INTO sessions VALUES ('old','p',NULL,'/old.jsonl',NULL,NULL,0,0,'','',0,0)")
        db.commit()
        db.close()
        with SessionIndex(self.db) as index:
            self.assertEqual(index.list_sessions(), [])
            pk = {row["name"]: row["pk"] for row in index.connection.execute("PRAGMA table_info(sessions)")}
            self.assertEqual(pk["session_id"], 0)
            self.assertGreaterEqual(pk["file_path"], 1)
            self.assertIn("malformed_lines", pk)
            write_session(self.root, "a", "dup", "first", "2024-01-01T00:00:00Z")
            write_session(self.root, "b", "dup", "second", "2024-01-02T00:00:00Z")
            self.assertEqual(index.index(self.root)["indexed"], 2)
            self.assertEqual([(r["session_id"], r["project_dir"]) for r in index.list_sessions()], [("dup", "b"), ("dup", "a")])

    def test_same_session_stem_in_two_projects_keeps_both_rows(self):
        write_session(self.root, "a", "dup", "first project", "2024-01-01T00:00:00Z")
        write_session(self.root, "b", "dup", "second project", "2024-01-02T00:00:00Z")
        with SessionIndex(self.db) as index:
            self.assertEqual(index.index(self.root)["indexed"], 2)
            rows = index.list_sessions()
            self.assertEqual([(r["session_id"], r["project_dir"]) for r in rows], [("dup", "b"), ("dup", "a")])
            self.assertEqual(len(index.find_sessions("du")), 2)
            self.assertEqual(index.find_sessions("zz"), [])

    def test_like_search_matches_user_text_and_db_environment_path_is_honored(self):
        # SessionIndex.search has only a LIKE path (no FTS5), so this exercises the fallback.
        env_db = Path(self.temp.name) / "env" / "env.sqlite3"
        with patch.dict(os.environ, {"SHM_DB_PATH": str(env_db)}):
            self.assertEqual(default_db_path(), env_db)
            explicit = Path(self.temp.name) / "explicit.sqlite3"
            self.assertEqual(default_db_path(explicit), explicit)
            index = SessionIndex()
        self.assertEqual(index.db_path, env_db)
        write_session(self.root, "p", "s1", "needle in title", "2024-01-01T00:00:00Z")
        write_records(self.root, "q", "s2", [
            user("generic opening", "2024-01-02T00:00:00Z", "q"),
            user("now about zebrafish genetics", "2024-01-02T00:05:00Z", "q"),
        ])
        try:
            index.index(self.root)
            self.assertEqual(ids(index.search("zebrafish")), ["s2"])
            self.assertEqual(index.list_sessions(project="q")[0]["title"], "generic opening")
            self.assertEqual(ids(index.search("needle")), ["s1"])
            self.assertEqual(ids(index.search("needle", project="p")), ["s1"])
            self.assertEqual(index.search("needle", project="q"), [])
            self.assertEqual(index.search("needle zebrafish"), [])
            self.assertEqual(index.search("absent"), [])
        finally:
            index.close()
        self.assertTrue(env_db.exists())


if __name__ == "__main__":
    unittest.main()
