"""Read-only, standard-library web dashboard for the session index.

Only GET and HEAD are served. Routes are resolved before the database is
opened, the database is opened with SQLite ``mode=ro`` and the server never
indexes, so it cannot modify the index or the Claude projects directory.
"""
import html
import json
import socket
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .indexer import SessionIndex, default_db_path

HTML_TYPE = "text/html; charset=utf-8"
JSON_TYPE = "application/json; charset=utf-8"
DB_UNAVAILABLE = "Database unavailable; run `python -m shm index`."

_STYLE = (
    "body{font:16px sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem}"
    "li{margin:.5rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
)
_NAV = '<nav><a href="/">Sessions</a> · <a href="/search">Search</a></nav>'
_SIMPLE_ROUTES = {
    "/": "list",
    "/api/sessions": "list",
    "/search": "search",
    "/api/search": "search",
}


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _json(data):
    return json.dumps(data, ensure_ascii=False)


def _page(title, body):
    head = '<meta charset="utf-8"><title>{}</title><style>{}</style>'.format(_e(title), _STYLE)
    return "<!doctype html><html><head>{}</head><body>{}<h1>{}</h1>{}</body></html>".format(
        head, _NAV, _e(title), body)


def _param(query, name):
    return query.get(name, [""])[0]


def _project(query):
    return _param(query, "project").strip() or None


def _limit(query):
    try:
        return max(1, min(1000, int(query.get("limit", ["100"])[0])))
    except (TypeError, ValueError):
        return 100


def _project_label(row):
    return row.get("project_path") or row.get("project_dir") or ""


def _row_item(row):
    session_id = str(row["session_id"])
    link = '<a href="/session/{}">{}</a>'.format(
        quote(session_id, safe=""), _e(row.get("title") or session_id))
    return "<li>{} · {} · {}</li>".format(
        link, _e(row.get("last_timestamp")), _e(_project_label(row)))


def _rows(rows):
    return "<ul>{}</ul>".format("".join(_row_item(row) for row in rows))


def _listing(rows, project):
    # Rows arrive newest-first, so insertion order sorts groups by their newest session.
    groups = {}
    for row in rows:
        groups.setdefault(_project_label(row), []).append(row)
    form = ('<form><label>Project <input name="project" value="{}"></label>'
            '<button>Filter</button></form>').format(_e(project or ""))
    sections = "".join("<h2>{}</h2>{}".format(_e(name), _rows(items))
                       for name, items in groups.items())
    return _page("Sessions", form + sections)


def _search_form(term, project, limit):
    fields = ('<input name="q" value="{}"><input name="project" value="{}">'
              '<input name="limit" value="{}">').format(_e(term), _e(project or ""), _e(limit))
    return "<form>{}<button>Search</button></form>".format(fields)


def _candidates_html(matches):
    items = "".join("<li>{} · {}</li>".format(_e(row["session_id"]), _e(row["file_path"]))
                    for row in matches)
    return "<p>Several sessions match; use a longer prefix.</p><ul>{}</ul>".format(items)


def _detail_html(row):
    items = "".join("<dt>{}</dt><dd><pre>{}</pre></dd>".format(_e(key), _e(value))
                    for key, value in row.items())
    return "<dl>{}</dl>".format(items)


def _error(code, message, api):
    if api:
        return code, _json({"error": message}), JSON_TYPE
    return code, _page("Error", "<p>{}</p>".format(_e(message))), HTML_TYPE


def _route(path):
    """Map a URL path to (kind, session prefix, is_api) without touching the DB."""
    api = path.startswith("/api/")
    if path in _SIMPLE_ROUTES:
        return _SIMPLE_ROUTES[path], None, api
    for prefix in ("/api/session/", "/session/"):
        if path.startswith(prefix):
            rest = path[len(prefix):]
            if rest and "/" not in rest:
                return "detail", rest, api
            # Empty or nested session paths deliberately fall through to 404.
            break
    return None, None, api


def _list_response(index, query, api):
    project = _project(query)
    rows = index.list_sessions(project=project, limit=_limit(query), exclude_worktrees=True)
    if api:
        return 200, _json(rows), JSON_TYPE
    return 200, _listing(rows, project), HTML_TYPE


def _search_response(index, query, api):
    term, project, limit = _param(query, "q"), _project(query), _limit(query)
    rows = []
    if term.strip():
        rows = index.search(term, project=project, limit=limit, exclude_worktrees=True)
    if api:
        return 200, _json(rows), JSON_TYPE
    return 200, _page("Search", _search_form(term, project, limit) + _rows(rows)), HTML_TYPE


def _detail_response(index, prefix, api):
    matches = index.find_sessions(prefix, exclude_worktrees=True)
    if not matches:
        return _error(404, "No session matches: " + prefix, api)
    if len(matches) > 1:
        if api:
            payload = {"error": "Session prefix is ambiguous: " + prefix, "candidates": matches}
            return 409, _json(payload), JSON_TYPE
        return 409, _page("Ambiguous session", _candidates_html(matches)), HTML_TYPE
    row = matches[0]
    if api:
        return 200, _json(row), JSON_TYPE
    return 200, _page("Session " + str(row["session_id"]), _detail_html(row)), HTML_TYPE


def _respond(index, kind, ident, query, api):
    if kind == "list":
        return _list_response(index, query, api)
    if kind == "search":
        return _search_response(index, query, api)
    return _detail_response(index, ident, api)


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type=HTML_TYPE, head=False):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _reply(self, result, head):
            code, body, content_type = result
            self._send(code, body, content_type, head=head)

        def _error(self, code, message, head, as_json):
            self._reply(_error(code, message, as_json), head)

        def do_HEAD(self):
            self._get(True)

        def do_GET(self):
            self._get(False)

        def send_error(self, code, message=None, explain=None):
            if code == 501:
                self._method_not_allowed()
            else:
                super().send_error(code, message, explain)

        def do_OPTIONS(self):
            self._method_not_allowed()

        def do_TRACE(self):
            self._method_not_allowed()

        def do_POST(self):
            self._method_not_allowed()

        do_PUT = do_DELETE = do_PATCH = do_CONNECT = do_POST

        def _method_not_allowed(self):
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _get(self, head=False):
            parts = urlsplit(self.path)
            kind, ident, api = _route(unquote(parts.path))
            if kind is None:
                self._reply(_error(404, "Not found", api), head)
                return
            query = parse_qs(parts.query, keep_blank_values=True)
            try:
                with SessionIndex(db_path, read_only=True) as index:
                    result = _respond(index, kind, ident, query, api)
            except (OSError, ValueError, sqlite3.Error):
                result = _error(503, DB_UNAVAILABLE, api)
            self._reply(result, head)

        def log_message(self, *_args):
            pass

    return Handler


def make_server(db_path=None, host="127.0.0.1", port=8765):
    server_class = ThreadingHTTPServer
    if host == "::1":
        class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_class = IPv6ThreadingHTTPServer
    return server_class((host, port), make_handler(default_db_path(db_path)))


def serve(db_path=None, host="127.0.0.1", port=8765):
    server = make_server(db_path, host, port)
    shown = "[::1]" if host == "::1" else host
    print("http://{}:{}".format(shown, server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
