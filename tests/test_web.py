"""Behaviour tests for the read-only web dashboard and the `serve` command."""
import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import shm.web as web
from shm.cli import main
from shm.indexer import SessionIndex
from shm.web import make_server

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TITLE = "<script>alert(1)</script> 안녕"
REPO_ROOT = Path(__file__).resolve().parents[1]
HIDDEN_IDS = {"hidden", "hidden-path", "hidden-encoded"}


def write_session(root, project, session_id, title, timestamp, cwd=None):
    directory = Path(root) / project
    directory.mkdir(parents=True, exist_ok=True)
    # The parser takes project_path from cwd, so "-alpha" maps to "/work/alpha".
    record = {"type": "user", "cwd": cwd or "/work/" + project.lstrip("-"),
              "timestamp": timestamp, "message": {"content": title}}
    path = directory / (session_id + ".jsonl")
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


@contextlib.contextmanager
def running(db):
    server = make_server(db, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    db = tmp_path / "index.sqlite3"
    monkeypatch.setenv("SHM_CLAUDE_DIR", str(root))
    monkeypatch.setenv("SHM_DB_PATH", str(tmp_path / "unused.sqlite3"))
    # alpha's newest session (01-06) beats beta's newest (01-05): alpha group first.
    write_session(root, "-alpha", "alpha-new", TITLE, "2024-01-06T00:00:00Z")
    write_session(root, "-alpha", "alpha-old", "older alpha exact", "2024-01-01T00:00:00Z")
    write_session(root, "-beta", "beta-one", "100% exact", "2024-01-03T00:00:00Z")
    write_session(root, "-beta", "beta-decoy", "100x exact", "2024-01-02T00:00:00Z")
    write_session(root, "-beta", "under-one", "a_b exact", "2024-01-05T00:00:00Z")
    write_session(root, "-beta", "under-decoy", "aXb exact", "2024-01-01T12:00:00Z")
    # Worktree sessions are the newest overall; only the exclusion keeps them out.
    write_session(root, "-Users-x-repo--director-worktrees-RUN-1", "hidden-encoded",
                  "secret needle", "2024-02-01T00:00:00Z", "/work/encoded")
    write_session(root, "-repo", "hidden-path", "secret path exact", "2024-02-02T00:00:00Z",
                  "/work/.director_worktrees/run/repo")
    with SessionIndex(db) as index:
        index.index(root)
    with running(db) as base:
        yield base, db, root


def fetch(url, method="GET"):
    request = urllib.request.Request(url, method=method)
    try:
        response = OPENER.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.headers, exc.read()
        finally:
            exc.close()
    with response:
        return response.status, response.headers, response.read()


def get(url):
    code, _, body = fetch(url)
    return code, body.decode("utf-8")


def get_json(url):
    code, body = get(url)
    return code, json.loads(body)


def ids(rows):
    return [row["session_id"] for row in rows]


def test_list_groups_newest_first_and_escapes(dashboard):
    base = dashboard[0]
    code, body = get(base + "/")
    assert code == 200
    # Groups are newest-first by each group's newest session.
    alpha = body.index("<h2>/work/alpha</h2>")
    beta = body.index("<h2>/work/beta</h2>")
    assert alpha < body.index("alpha-new") < body.index("alpha-old") < beta
    assert body.index("under-one") < body.index("beta-one") < body.index("beta-decoy")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "<script>" not in body
    assert "안녕" in body
    assert "secret" not in body and "hidden" not in body
    code, filtered = get(base + "/?project=%2Fwork%2Falpha")
    assert code == 200 and "alpha-old" in filtered and "beta-one" not in filtered
    assert get(base + "/?limit=abc")[0] == 200


def test_hidden_newer_rows_do_not_consume_limit(dashboard):
    base, db, _ = dashboard
    # The newest sessions overall are worktree ones; they must not use up the limit.
    with SessionIndex(db, read_only=True) as index:
        assert ids(index.list_sessions(limit=1)) == ["hidden-path"]
    code, body = get(base + "/?limit=1")
    assert code == 200 and body.count("<li>") == 1
    assert '<a href="/session/alpha-new">' in body and "hidden" not in body
    code, rows = get_json(base + "/api/sessions?limit=1")
    assert code == 200 and ids(rows) == ["alpha-new"]


def test_api_sessions_excludes_worktrees_and_keeps_utf8(dashboard):
    base = dashboard[0]
    code, headers, raw = fetch(base + "/api/sessions")
    assert code == 200 and headers["Content-Type"] == "application/json; charset=utf-8"
    text = raw.decode("utf-8")
    assert "안녕" in text and "\\u" not in text
    rows = json.loads(text)
    assert ids(rows) == ["alpha-new", "under-one", "beta-one", "beta-decoy",
                         "under-decoy", "alpha-old"]
    assert rows[0]["title"] == TITLE
    code, rows = get_json(base + "/api/search?q=secret")
    assert code == 200 and not HIDDEN_IDS & set(ids(rows))


def test_search_literal_wildcards_and_empty_query(dashboard):
    base = dashboard[0]
    assert ids(get_json(base + "/api/search?q=100%25")[1]) == ["beta-one"]
    assert ids(get_json(base + "/api/search?q=a_b")[1]) == ["under-one"]
    body = get(base + "/search?q=100%25")[1]
    assert "beta-one" in body and "beta-decoy" not in body
    body = get(base + "/search?q=a_b")[1]
    assert "under-one" in body and "under-decoy" not in body
    code, body = get(base + "/search")
    assert code == 200 and "<form" in body and "<ul></ul>" in body
    assert get_json(base + "/api/search") == (200, [])


def test_search_project_parameter_restricts_results(dashboard):
    base = dashboard[0]
    code, rows = get_json(base + "/api/search?q=exact")
    assert code == 200
    assert sorted(ids(rows)) == ["alpha-old", "beta-decoy", "beta-one", "under-decoy", "under-one"]
    assert ids(get_json(base + "/api/search?q=exact&project=%2Fwork%2Falpha")[1]) == ["alpha-old"]
    assert ids(get_json(base + "/api/search?q=exact&project=-alpha")[1]) == ["alpha-old"]
    body = get(base + "/search?q=exact&project=%2Fwork%2Fbeta")[1]
    assert "beta-one" in body and "alpha-old" not in body


def test_search_excludes_worktrees(dashboard):
    base = dashboard[0]
    assert get_json(base + "/api/search?q=secret") == (200, [])
    assert get_json(base + "/api/search?q=needle") == (200, [])
    assert "hidden" not in get(base + "/search?q=secret")[1]


def test_detail_page_and_api(dashboard):
    base = dashboard[0]
    code, body = get(base + "/session/alpha-n")
    assert code == 200
    assert "Session alpha-new" in body and "/work/alpha" in body
    assert "<dt>project_path</dt><dd><pre>/work/alpha</pre></dd>" in body
    assert "<dt>project_dir</dt><dd><pre>-alpha</pre></dd>" in body
    assert "<dt>message_count</dt><dd><pre>1</pre></dd>" in body
    assert "<dt>user_message_count</dt><dd><pre>1</pre></dd>" in body
    assert "&lt;script&gt;" in body and "<script>" not in body
    code, row = get_json(base + "/api/session/alpha-new")
    assert code == 200 and row["session_id"] == "alpha-new"
    assert (row["project_path"], row["project_dir"]) == ("/work/alpha", "-alpha")
    assert row["title"] == TITLE
    assert (row["message_count"], row["user_message_count"]) == (1, 1)


def test_ambiguous_prefix_html_and_api(dashboard):
    base = dashboard[0]
    code, body = get(base + "/session/alpha-")
    assert code == 409 and "alpha-new" in body and "alpha-old" in body
    code, data = get_json(base + "/api/session/alpha-")
    assert code == 409 and "error" in data
    assert sorted(ids(data["candidates"])) == ["alpha-new", "alpha-old"]


def test_detail_missing_and_worktree_exclusion(dashboard):
    base = dashboard[0]
    assert get(base + "/session/missing")[0] == 404
    code, data = get_json(base + "/api/session/missing")
    assert code == 404 and "error" in data
    for ident in sorted(HIDDEN_IDS):
        assert get(base + "/session/" + ident)[0] == 404
        code, data = get_json(base + "/api/session/" + ident)
        assert code == 404 and "error" in data


def test_head_returns_headers_without_body(dashboard):
    base = dashboard[0]
    code, headers, body = fetch(base + "/", method="HEAD")
    assert code == 200 and body == b""
    assert int(headers["Content-Length"]) > 0
    code, _, body = fetch(base + "/api/sessions", method="HEAD")
    assert code == 200 and body == b""


def test_post_is_405_and_unknown_paths_are_404(dashboard):
    base = dashboard[0]
    for path in ("/", "/api/sessions"):
        code, headers, _ = fetch(base + path, method="POST")
        assert code == 405 and headers["Allow"] == "GET, HEAD"
    assert get(base + "/unknown")[0] == 404
    assert get(base + "/session/")[0] == 404
    assert get(base + "/session/a/b")[0] == 404
    code, data = get_json(base + "/api/unknown")
    assert code == 404 and "error" in data


def test_server_never_writes_db_or_claude_dir(dashboard):
    base, db, root = dashboard
    before = (db.read_bytes(), db.stat().st_mtime_ns, sorted(db.parent.iterdir()))
    tree = {path: path.stat().st_mtime_ns for path in root.rglob("*")}
    for path in ("/", "/api/sessions", "/search?q=exact", "/api/session/alpha-new", "/session/alpha-"):
        get(base + path)
    assert (db.read_bytes(), db.stat().st_mtime_ns, sorted(db.parent.iterdir())) == before
    assert {path: path.stat().st_mtime_ns for path in root.rglob("*")} == tree


def test_missing_db_reports_error_without_creating_it(tmp_path):
    missing = tmp_path / "absent" / "index.sqlite3"
    with running(missing) as base:
        code, body = get(base + "/")
        assert code == 503 and "python -m shm index" in body
        code, data = get_json(base + "/api/sessions")
        assert code == 503 and "python -m shm index" in data["error"]
        assert get(base + "/unknown")[0] == 404
        code, headers, _ = fetch(base + "/", method="POST")
        assert code == 405 and headers["Allow"] == "GET, HEAD"
    assert not missing.exists() and not missing.parent.exists()


# Keep the original test entry points alongside the more focused tests below.
# Calling the shared tests preserves their assertions without duplicating setup.
def test_list_grouping_filter_order_and_worktree_limit(dashboard):
    test_list_groups_newest_first_and_escapes(dashboard)
    test_hidden_newer_rows_do_not_consume_limit(dashboard)


def test_api_exclusion_and_utf8(dashboard):
    test_api_sessions_excludes_worktrees_and_keeps_utf8(dashboard)


def test_detail_ambiguous_missing_and_worktree_exclusion(dashboard):
    test_ambiguous_prefix_html_and_api(dashboard)
    test_detail_missing_and_worktree_exclusion(dashboard)


def test_methods_unknown_and_missing_db(tmp_path):
    missing = tmp_path / "absent" / "index.sqlite3"
    with running(missing) as base:
        for path in ("/", "/api/sessions"):
            code, headers, _ = fetch(base + path, method="POST")
            assert code == 405 and headers["Allow"] == "GET, HEAD"
        assert get(base + "/unknown")[0] == 404
        assert get(base + "/session/")[0] == 404
        assert get(base + "/session/a/b")[0] == 404
        code, data = get_json(base + "/api/unknown")
        assert code == 404 and "error" in data
        code, body = get(base + "/")
        assert code == 503 and "python -m shm index" in body
        code, data = get_json(base + "/api/sessions")
        assert code == 503 and "python -m shm index" in data["error"]
    assert not missing.exists() and not missing.parent.exists()


def test_worktree_exclusion_variants_in_sql(tmp_path):
    root = tmp_path / "claude"
    variants = [
        ("keep-plain", "-work-app", "/work/app"),
        ("keep-similar", "-work-app2", "/work/my.director_worktrees_old/app"),
        ("hide-backslash", "-c-repo", "C:\\repo\\.director_worktrees\\run"),
        ("hide-relative", "-rel", ".director_worktrees/run/repo"),
        ("hide-trailing", "-trail", "/work/repo/.director_worktrees"),
        ("hide-dir-dash", "-Users-x-repo--director-worktrees-RUN-1", "/work/y"),
        ("hide-dir-underscore", "-x--director_worktrees-run", "/work/x"),
        ("keep-dir-underscore", "-x-director_worktrees-run", "/x/director_worktrees/run"),
        ("hide-dir-dot", "-x-.director_worktrees-run", "/work/z"),
        ("hide-dir-end", "-x--director-worktrees", "/work/w"),
        ("keep-dir-wildcard", "-x-.directorXworktrees", "/work/v"),
        ("keep-my-project", "-work-my-director-worktrees", "/work/my-director-worktrees"),
    ]
    for number, (session_id, project, cwd) in enumerate(variants):
        stamp = "2024-03-{:02d}T00:00:00Z".format(number + 1)
        write_session(root, project, session_id, "needle " + session_id, stamp, cwd)
    worktree_root = tmp_path / ".director_worktrees" / "claude"
    write_session(worktree_root, "-plain", "hide-file", "needle file", "2024-04-01T00:00:00Z",
                  "/work/plain")
    with SessionIndex(tmp_path / "index.sqlite3") as index:
        index.index(root)
        index.index(worktree_root)
        assert len(index.list_sessions()) == 13
        expected = ["keep-my-project", "keep-dir-wildcard", "keep-dir-underscore",
                    "keep-similar", "keep-plain"]
        assert ids(index.list_sessions(exclude_worktrees=True)) == expected
        assert ids(index.list_sessions(limit=1, exclude_worktrees=True)) == ["keep-my-project"]
        assert ids(index.search("needle", exclude_worktrees=True)) == expected
        assert index.find_sessions("hide-", exclude_worktrees=True) == []
        assert len(index.find_sessions("hide-")) == 8


def test_similar_names_visible_and_worktree_segments_hidden_over_http(tmp_path):
    root = tmp_path / "claude"
    old_root = tmp_path / ".director_worktrees_old" / "claude"
    worktree_root = tmp_path / ".director_worktrees" / "claude"
    write_session(root, "-work-my-director-worktrees", "vis-mydir", "needle mydir",
                  "2024-06-01T00:00:00Z", "/work/my-director-worktrees")
    write_session(old_root, "-old", "vis-old", "needle old", "2024-06-02T00:00:00Z",
                  "/x/.director_worktrees_old/app")
    hidden = [
        (root, "-s", "hid-slash", "/x/.director_worktrees/run"),
        (root, "-b", "hid-back", "C:\\x\\.director_worktrees\\run"),
        (root, "-t", "hid-trail", "/x/.director_worktrees"),
        (root, "-x--director-worktrees-RUN-1", "hid-enc", "/work/enc"),
        (worktree_root, "-f", "hid-file", "/work/f"),
    ]
    for number, (source, project, ident, cwd) in enumerate(hidden):
        stamp = "2024-07-{:02d}T00:00:00Z".format(number + 1)
        write_session(source, project, ident, "needle " + ident, stamp, cwd)
    db = tmp_path / "index.sqlite3"
    with SessionIndex(db) as index:
        for source in (root, old_root, worktree_root):
            index.index(source)
        assert len(index.list_sessions()) == 7
    visible = ["vis-old", "vis-mydir"]
    with running(db) as base:
        code, rows = get_json(base + "/api/sessions")
        assert code == 200 and ids(rows) == visible
        code, rows = get_json(base + "/api/search?q=needle")
        assert code == 200 and ids(rows) == visible
        for path in ("/", "/search?q=needle"):
            code, body = get(base + path)
            assert code == 200
            assert "/session/vis-mydir" in body and "/session/vis-old" in body
            assert "hid-" not in body
        for _, _, ident, _ in hidden:
            assert get(base + "/session/" + ident)[0] == 404
            code, data = get_json(base + "/api/session/" + ident)
            assert code == 404 and "error" in data


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])
def test_cli_rejects_non_loopback_host(tmp_path, capsys, host):
    db = tmp_path / "db"
    assert main(["serve", "--host", host, "--db", str(db)]) == 2
    assert "loopback" in capsys.readouterr().err
    assert not db.exists()


def test_cli_serve_prints_url_and_closes(tmp_path, capsys, monkeypatch):
    closed = []

    class FakeServer:
        server_address = ("127.0.0.1", 4321)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            closed.append(True)

    monkeypatch.setattr(web, "make_server", lambda *args: FakeServer())
    db = tmp_path / "db"
    assert main(["serve", "--port", "0", "--db", str(db)]) == 0
    assert "http://127.0.0.1:4321" in capsys.readouterr().out
    assert closed == [True] and not db.exists()


def test_cli_serve_end_to_end_on_loopback(tmp_path):
    claude = tmp_path / "claude"
    claude.mkdir()
    source = tmp_path / "source"
    write_session(source, "-e2e", "e2e-session", "end to end", "2024-05-01T00:00:00Z")
    db = tmp_path / "index.sqlite3"
    with SessionIndex(db) as index:
        index.index(source)
    before = db.read_bytes()
    unused = tmp_path / "unused.sqlite3"
    env = dict(os.environ, SHM_CLAUDE_DIR=str(claude), SHM_DB_PATH=str(unused))
    proc = subprocess.Popen([sys.executable, "-m", "shm", "serve", "--port", "0", "--db", str(db)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, cwd=str(REPO_ROOT))
    lines = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        url = None
        deadline = time.monotonic() + 10
        while url is None and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            if line.startswith("http://"):
                url = line.strip()
        assert url is not None, "serve did not print a URL"
        parts = urllib.parse.urlsplit(url)
        assert parts.hostname == "127.0.0.1" and parts.port
        code, _, body = fetch(url + "/")
        assert code == 200 and b"e2e-session" in body
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    assert proc.returncode is not None
    assert list(claude.iterdir()) == []
    assert db.read_bytes() == before
    assert not unused.exists()
