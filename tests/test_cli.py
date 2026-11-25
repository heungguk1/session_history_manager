import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from shm.cli import main


def write(project, session_id, prompt, timestamp, cwd="/work/demo"):
    project.mkdir(parents=True, exist_ok=True)
    record = {"type": "user", "cwd": cwd, "timestamp": timestamp, "message": {"content": prompt}}
    (project / (session_id + ".jsonl")).write_text(json.dumps(record) + "\n", encoding="utf-8")


class CliWorktreeFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sessions"
        self.db = Path(self.temp.name) / "index.sqlite3"
        write(self.root / "-work-demo", "abc111normal", "shared needle normal", "2024-01-02T00:00:00Z")
        worktree = self.root / "-work-demo--director-worktrees-RUN1"
        cwd = "/work/demo/.director_worktrees/RUN1"
        write(worktree, "abc222tree", "shared needle tree", "2024-01-03T00:00:00Z", cwd)
        write(worktree, "wt999only", "tree only", "2024-01-04T00:00:00Z", cwd)
        self.call_cli("index", "--root", str(self.root), "--db", str(self.db))

    def call_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_list_hides_worktrees_by_default_and_opt_in_restores_them(self):
        code, out, _ = self.call_cli("list", "--json", "--db", str(self.db))
        self.assertEqual((code, [r["session_id"] for r in json.loads(out)]), (0, ["abc111normal"]))
        code, out, _ = self.call_cli("list", "--json", "--include-worktrees", "--db", str(self.db))
        self.assertEqual((code, {r["session_id"] for r in json.loads(out)}),
                         (0, {"abc111normal", "abc222tree", "wt999only"}))
        code, out, _ = self.call_cli("list", "--db", str(self.db))
        self.assertEqual((code, len(out.strip().splitlines())), (0, 1))
        code, out, _ = self.call_cli("list", "--include-worktrees", "--db", str(self.db))
        self.assertEqual((code, len(out.strip().splitlines())), (0, 3))

    def test_search_hides_worktrees_by_default_and_opt_in_restores_them(self):
        code, out, _ = self.call_cli("search", "needle", "--json", "--db", str(self.db))
        self.assertEqual((code, [r["session_id"] for r in json.loads(out)]), (0, ["abc111normal"]))
        code, out, _ = self.call_cli("search", "needle", "--json", "--include-worktrees", "--db", str(self.db))
        self.assertEqual((code, {r["session_id"] for r in json.loads(out)}),
                         (0, {"abc111normal", "abc222tree"}))
        code, out, _ = self.call_cli("search", "needle", "--db", str(self.db))
        self.assertEqual((code, len(out.strip().splitlines())), (0, 1))
        code, out, _ = self.call_cli("search", "needle", "--include-worktrees", "--db", str(self.db))
        self.assertEqual((code, len(out.strip().splitlines())), (0, 2))

    def test_show_filters_prefix_matches_and_help_describes_opt_in(self):
        code, out, err = self.call_cli("show", "wt999", "--db", str(self.db))
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("No session matches", err)
        code, out, _ = self.call_cli("show", "wt999", "--include-worktrees", "--json", "--db", str(self.db))
        self.assertEqual((code, json.loads(out)["session_id"]), (0, "wt999only"))
        code, out, _ = self.call_cli("show", "abc", "--json", "--db", str(self.db))
        self.assertEqual((code, json.loads(out)["session_id"]), (0, "abc111normal"))
        code, out, err = self.call_cli("show", "abc", "--include-worktrees", "--db", str(self.db))
        self.assertNotEqual(code, 0)
        self.assertIn("ambiguous", err)
        for command in ("list", "search", "show"):
            code, out, _ = self.call_cli(command, "--help")
            self.assertEqual(code, 0)
            self.assertIn("--include-worktrees", out)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sessions"
        self.db = Path(self.temp.name) / "index.sqlite3"
        self.project = self.root / "-demo"
        write(self.project, "abcdef012345", "Find 100% literal", "2024-01-02T10:00:00Z")

    def call_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_help_lists_subcommands(self):
        code, out, _ = self.call_cli("--help")
        self.assertEqual(code, 0)
        for name in ("index", "list", "search", "show"):
            self.assertIn(name, out)

    def test_index_list_search_and_show(self):
        tree = set(self.root.rglob("*"))
        code, out, err = self.call_cli("index", "--root", str(self.root), "--db", str(self.db))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("indexed: 1", out)
        self.assertEqual(set(self.root.rglob("*")), tree)
        code, out, _ = self.call_cli("list", "--json", "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertEqual([r["session_id"] for r in json.loads(out)], ["abcdef012345"])
        code, out, _ = self.call_cli("list", "--db", str(self.db))
        lines = out.strip().splitlines()
        self.assertEqual((code, len(lines)), (0, 1))
        self.assertEqual(lines[0].split("\t"), ["abcdef01", "2024-01-02T10:00:00+00:00", "/work/demo", "Find 100% literal"])
        code, out, _ = self.call_cli("search", "100%", "--db", str(self.db), "--json")
        self.assertEqual((code, [r["session_id"] for r in json.loads(out)]), (0, ["abcdef012345"]))
        code, out, _ = self.call_cli("search", "1000", "--db", str(self.db), "--json")
        self.assertEqual((code, json.loads(out)), (0, []))
        code, out, _ = self.call_cli("show", "abcdef", "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertIn("session_id: abcdef012345", out)

    def test_incremental_default_and_full_reindex(self):
        args = ("--root", str(self.root), "--db", str(self.db))
        self.assertIn("indexed: 1", self.call_cli("index", *args)[1])
        code, out, _ = self.call_cli("index", *args)
        self.assertEqual(code, 0)
        self.assertIn("indexed: 0, skipped: 1", out)
        code, out, _ = self.call_cli("index", "--full", *args)
        self.assertEqual(code, 0)
        self.assertIn("indexed: 1, skipped: 0", out)

    def test_ambiguous_prefix_fails_and_exact_id_resolves(self):
        write(self.project, "abc111", "one", "2024-01-03T00:00:00Z")
        write(self.project, "abc222", "two", "2024-01-04T00:00:00Z")
        self.call_cli("index", "--root", str(self.root), "--db", str(self.db))
        code, out, err = self.call_cli("show", "abc", "--db", str(self.db))
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("ambiguous", err)
        self.assertIn("abc111.jsonl", err)
        code, out, _ = self.call_cli("show", "abc111", "--json", "--db", str(self.db))
        self.assertEqual((code, json.loads(out)["title"]), (0, "one"))

    def test_environment_database_and_prefix_errors(self):
        with patch.dict("os.environ", {"SHM_DB_PATH": str(self.db), "SHM_CLAUDE_DIR": str(self.root)}):
            self.call_cli("index")
            code, out, _ = self.call_cli("list", "--json")
            self.assertEqual(json.loads(out)[0]["session_id"], "abcdef012345")
            code, out, err = self.call_cli("search", "100%", "--json")
            self.assertEqual((code, [r["session_id"] for r in json.loads(out)]), (0, ["abcdef012345"]))
            code, _, err = self.call_cli("show", "missing")
            self.assertNotEqual(code, 0)
            self.assertIn("No session matches", err)
            code, out, _ = self.call_cli("show", "abcdef", "--json")
            self.assertEqual(json.loads(out)["session_id"], "abcdef012345")


if __name__ == "__main__":
    unittest.main()
