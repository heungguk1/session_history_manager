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
from datetime import datetime, timedelta, timezone
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
    # Projects show their folder name with the full path in a title attribute.
    alpha = body.index('<h2 class="project-name" title="/work/alpha">alpha</h2>')
    beta = body.index('<h2 class="project-name" title="/work/beta">beta</h2>')
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
    assert code == 200 and body.count('<li class="session-card">') == 1
    assert '<a class="session-title" href="/session/alpha-new">' in body
    assert "hidden" not in body
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
        assert 'class="db-unavailable"' in body
        code, headers, body = fetch(base + "/")
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        code, data = get_json(base + "/api/sessions")
        assert code == 503 and "python -m shm index" in data["error"]
        assert get(base + "/unknown")[0] == 404
        code, headers, _ = fetch(base + "/", method="POST")
        assert code == 405 and headers["Allow"] == "GET, HEAD"
    assert not missing.exists() and not missing.parent.exists()


def test_html_screens_share_frame_and_have_screen_classes(dashboard):
    base = dashboard[0]
    pages = (("/", "page-list", 200), ("/search", "page-search", 200),
             ("/session/alpha-new", "page-detail", 200),
             ("/session/alpha-", "page-candidates", 409), ("/missing", "page-error", 404))
    for path, page_class, expected_code in pages:
        code, body = get(base + path)
        assert code == expected_code
        assert 'class="site-header"' in body
        assert body.count("<style") == 1
        assert '<main class="page {}">'.format(page_class) in body
    missing = dashboard[1].parent / "missing.sqlite3"
    with running(missing) as unavailable_base:
        code, body = get(unavailable_base + "/")
    assert code == 503 and 'class="page page-db-unavailable"' in body
    assert 'class="site-header"' in body
    assert body.count("<style") == 1
    assert 'class="db-unavailable"' in body


def test_list_search_detail_semantic_classes(dashboard):
    base = dashboard[0]
    list_code, list_body = get(base + "/")
    search_code, search_body = get(base + "/search?q=exact")
    detail_code, detail_body = get(base + "/session/alpha-new")
    assert (list_code, search_code, detail_code) == (200, 200, 200)
    for class_name in ("filter-bar", "period-links", "project-select", "project-list",
                       "project-group", "project-name", "session-list", "session-card",
                       "session-title", "session-preview", "session-count", "session-time"):
        assert 'class="{}"'.format(class_name) in list_body
    assert 'class="search-form"' in search_body
    assert 'class="session-detail"' in detail_body


def test_empty_result_list_and_search(dashboard):
    base = dashboard[0]
    for path in ("/?q=nonexistent-term-xyz", "/?project=%2Fno%2Fsuch%2Fproject"):
        code, body = get(base + path)
        assert code == 200
        assert 'class="empty-state"' in body
        assert "조건에 맞는 세션이 없습니다" in body
        assert '<a class="filter-reset" href="/">필터 초기화</a>' in body
        assert 'class="filter-bar"' in body
    code, body = get(base + "/search?q=nonexistent-term-xyz")
    assert code == 200
    assert 'class="empty-state"' in body
    assert "조건에 맞는 세션이 없습니다" in body
    assert '<a class="filter-reset" href="/search">필터 초기화</a>' in body
    assert 'class="search-form"' in body


def test_render_empty_result_direct():
    assert web.render_empty_result("/x?a=1&b=2") == (
        '<div class="empty-state">조건에 맞는 세션이 없습니다 '
        '<a class="filter-reset" href="/x?a=1&amp;b=2">필터 초기화</a></div>')


def test_html_pages_have_no_external_references(dashboard):
    base = dashboard[0]
    responses = []
    for path in ("/", "/?q=nonexistent-term-xyz", "/search?q=exact",
                 "/session/alpha-new"):
        code, body = get(base + path)
        assert code == 200
        responses.append(body)
    missing = dashboard[1].parent / "missing.sqlite3"
    with running(missing) as unavailable_base:
        code, body = get(unavailable_base + "/")
        assert code == 503
        responses.append(body)
    assert all("http://" not in body and "https://" not in body for body in responses)


def test_unreadable_db_returns_html_and_api_errors(tmp_path):
    missing = tmp_path / "absent.sqlite3"
    with running(missing) as base:
        # Existing directory at the DB path cannot be opened as a readable SQLite file.
        missing.mkdir()
        code, body = get(base + "/")
        assert code == 503 and 'class="db-unavailable"' in body
        code, data = get_json(base + "/api/sessions")
        assert code == 503 and data["error"] == web.DB_UNAVAILABLE


def test_project_labels_expand_collisions_to_distinct_paths():
    labels = web.project_labels(["/a/x/app", "/b/x/app", "/work/app", "/personal/app"])
    assert labels["/a/x/app"] == "a/x/app"
    assert labels["/b/x/app"] == "b/x/app"
    assert labels["/work/app"] == "work/app"
    assert labels["/personal/app"] == "personal/app"


def test_project_labels_keep_raw_paths_distinct_after_suffixes_are_exhausted():
    labels = web.project_labels(["/x/app", "x/app"])
    assert len(set(labels.values())) == 2


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


KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=KST)
TEMP_IDS = ("tmp-scratch", "tmp-builder", "tmp-var", "tmp-ptmp", "tmp-file")
VISIBLE = ["p-app", "w-app", "w-api"]


def write_records(root, project, session_id, records):
    directory = Path(root) / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (session_id + ".jsonl")
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                    encoding="utf-8")
    return path


def test_format_local_time_today_yesterday_older_midnight_and_kst():
    fmt = web.format_local_time
    assert fmt("2026-11-25T08:32:00Z", NOW, KST) == "오늘 17:32"
    assert fmt("2026-11-24T08:32:00Z", NOW, KST) == "어제 17:32"
    assert fmt("2026-11-23T08:32:00Z", NOW, KST) == "2026-11-23 17:32"
    assert fmt("2026-11-25T08:32:00+00:00", datetime(2026, 12, 1, tzinfo=KST), KST) == "2026-11-25 17:32"
    midnight = datetime(2026, 11, 26, 0, 0, tzinfo=KST)
    assert fmt("2026-11-25T14:59:00Z", midnight, KST) == "어제 23:59"
    assert fmt("2026-11-25T15:00:00Z", midnight, KST) == "오늘 00:00"
    before = datetime(2026, 11, 25, 23, 59, tzinfo=KST)
    assert fmt("2026-11-25T14:59:00Z", before, KST) == "오늘 23:59"
    assert fmt("2026-11-25T15:00:00Z", before, KST) == "2026-11-26 00:00"
    # Default timezone is Asia/Seoul: 08:32Z is 17:32 local.
    assert fmt("2026-11-25T08:32:00Z", datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)) == "오늘 17:32"
    assert fmt(None, NOW, KST) == "" and fmt("garbage", NOW, KST) == "garbage"


def test_default_timezone_falls_back_to_fixed_kst(monkeypatch):
    monkeypatch.setitem(sys.modules, "zoneinfo", None)
    assert web.default_timezone().utcoffset(None) == timedelta(hours=9)
    now = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
    assert web.format_local_time("2026-11-25T08:32:00Z", now) == "오늘 17:32"


@pytest.fixture
def filtered(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "_local_now", lambda tz: NOW.astimezone(tz))
    root = tmp_path / "claude"
    write_session(root, "-home-work-app", "w-app", "deploy pipeline question",
                  "2026-11-20T03:00:00Z", "/home/work/app")
    # 16:00Z on the 24th is 01:00 on the 25th in Seoul.
    write_session(root, "-home-personal-app", "p-app", "deploy blog", "2026-11-24T16:00:00Z",
                  "/home/personal/app")
    cwd = "/home/work/api"
    write_records(root, "-home-work-api", "w-api", [
        {"type": "user", "cwd": cwd, "timestamp": "2026-11-09T23:00:00Z", "message": {"content": "y" * 200}},
        {"type": "assistant", "timestamp": "2026-11-09T23:30:00Z", "message": {"content": "ok"}},
        {"type": "user", "timestamp": "2026-11-10T00:00:00Z", "message": {"content": "zebra details"}},
    ])
    temps = [("-t1", "tmp-scratch", "/tmp/scratch"),
             ("-t2", "tmp-builder", "/private/var/folders/ab/T/director-builder-q"),
             ("-t3", "tmp-var", "/var/folders/x/y"),
             ("-t4", "tmp-ptmp", "/private/tmp/z")]
    for project, ident, path in temps:
        write_session(root, project, ident, "deploy " + ident, "2026-11-25T01:00:00Z", path)
    builder_root = tmp_path / "director-builder-xyz" / "claude"
    write_session(builder_root, "-home-work-ok", "tmp-file", "deploy file",
                  "2026-11-25T02:00:00Z", "/home/work/ok")
    db = tmp_path / "index.sqlite3"
    with SessionIndex(db) as index:
        index.index(root)
        index.index(builder_root)
        assert len(index.list_sessions()) == 8
    with running(db) as base:
        yield base


def test_temp_sessions_hidden_from_list_search_api_and_dropdown(filtered):
    base = filtered
    assert ids(get_json(base + "/api/sessions")[1]) == VISIBLE
    assert ids(get_json(base + "/api/search?q=deploy")[1]) == ["p-app", "w-app"]
    for path in ("/", "/?q=deploy", "/search?q=deploy"):
        code, body = get(base + path)
        assert code == 200 and "/session/p-app" in body
        for text in ("tmp-", "/tmp/", "/var/folders/", "director-builder", "/home/work/ok"):
            assert text not in body
    for ident in TEMP_IDS:
        assert get(base + "/session/" + ident)[0] == 404


def test_each_filter_alone_and_combined(filtered):
    base = filtered

    def api(query):
        code, rows = get_json(base + "/api/sessions?" + query)
        assert code == 200
        return ids(rows)

    assert api("project=%2Fhome%2Fwork%2Fapp") == ["w-app"]
    assert api("project=-home-work-api") == ["w-api"]
    assert api("from=2026-11-25") == ["p-app"]
    assert api("to=2026-11-24") == ["w-app", "w-api"]
    assert api("from=2026-11-10&to=2026-11-10") == ["w-api"]
    assert api("q=deploy") == ["p-app", "w-app"]
    assert api("q=zebra") == ["w-api"]
    assert api("q=deploy&from=2026-11-15&to=2026-11-24") == ["w-app"]
    assert api("q=deploy&project=%2Fhome%2Fpersonal%2Fapp&from=2026-11-25") == ["p-app"]
    assert api("q=deploy&project=%2Fhome%2Fpersonal%2Fapp&to=2026-11-24") == []
    code, body = get(base + "/?q=deploy&from=2026-11-21")
    assert code == 200 and "/session/p-app" in body
    assert "/session/w-app" not in body and "/session/w-api" not in body
    code, body = get(base + "/?project=%2Fhome%2Fwork%2Fapp")
    assert "/session/w-app" in body and "/session/p-app" not in body


def test_invalid_dates_are_ignored(filtered):
    base = filtered
    for query in ("from=abc", "to=2026-13-45", "from=abc&to=2026-13-45", "from=&to="):
        code, rows = get_json(base + "/api/sessions?" + query)
        assert code == 200 and ids(rows) == VISIBLE
        code, body = get(base + "/?" + query)
        assert code == 200 and all("/session/" + i in body for i in VISIBLE)
    body = get(base + "/?from=abc&to=2026-13-45")[1]
    assert '<input type="date" name="from" value="">' in body


def test_filter_form_and_period_links_keep_values(filtered):
    base = filtered
    code, body = get(base + "/?project=%2Fhome%2Fwork%2Fapp&from=2026-11-01&to=2026-11-30&q=deploy")
    assert code == 200 and "/session/w-app" in body
    assert '<input type="date" name="from" value="2026-11-01">' in body
    assert '<input type="date" name="to" value="2026-11-30">' in body
    assert '<input type="search" name="q" value="deploy">' in body
    assert '<option value="/home/work/app" title="/home/work/app" selected>work/app (1)</option>' in body
    keep = "/?project=%2Fhome%2Fwork%2Fapp&amp;"
    for label, start in (("오늘", "2026-11-25"), ("7일", "2026-11-19"), ("30일", "2026-10-27")):
        href = keep + "from={}&amp;to=2026-11-25&amp;q=deploy".format(start)
        assert '<a href="{}">{}</a>'.format(href, label) in body
    assert '<a href="/?project=%2Fhome%2Fwork%2Fapp&amp;q=deploy">전체</a>' in body
    body = get(base + "/")[1]
    assert '<a href="/">전체</a>' in body
    assert '<a href="/?from=2026-11-25&amp;to=2026-11-25">오늘</a>' in body


def test_list_items_project_labels_preview_count_and_time(filtered):
    base = filtered
    code, body = get(base + "/")
    assert code == 200
    assert body.index("/session/p-app") < body.index("/session/w-app") < body.index("/session/w-api")
    assert '<h2 class="project-name" title="/home/work/app">work/app</h2>' in body
    assert '<h2 class="project-name" title="/home/personal/app">personal/app</h2>' in body
    assert '<h2 class="project-name" title="/home/work/api">api</h2>' in body
    assert '<span title="/home/personal/app">personal/app</span>' in body
    assert 'class="session-preview">' + "y" * 119 + "…</span>" in body
    assert "y" * 120 not in body
    assert ('<span class="session-count">메시지 3개</span> · '
            '<span class="session-time">2026-11-10 09:00</span>') in body
    assert ('<span class="session-count">메시지 1개</span> · '
            '<span class="session-time">오늘 01:00</span>') in body and "2026-11-20 12:00" in body
    for value, label in (("/home/work/app", "work/app"), ("/home/personal/app", "personal/app"),
                         ("/home/work/api", "api")):
        assert '<option value="{0}" title="{0}">{1} (1)</option>'.format(value, label) in body
    code, rows = get_json(base + "/api/sessions")
    assert code == 200 and ids(rows) == VISIBLE
    assert rows[0]["last_activity_local"] == "오늘 01:00"
    assert rows[2]["preview"] == "y" * 119 + "…" and len(rows[2]["preview"]) == 120
    assert rows[2]["message_count"] == 3
