"""Read-only, standard-library web dashboard for the session index.

Only GET and HEAD are served. Routes are resolved before the database is
opened, the database is opened with SQLite ``mode=ro`` and the server never
indexes, so it cannot modify the index or the Claude projects directory.
"""
import html
import json
import socket
import sqlite3
from datetime import date, datetime, time as dt_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from .indexer import SessionIndex, default_db_path

HTML_TYPE = "text/html; charset=utf-8"
JSON_TYPE = "application/json; charset=utf-8"
DB_UNAVAILABLE = "Database unavailable; run `python -m shm index`."
DEFAULT_TIMEZONE = "Asia/Seoul"
KST_FALLBACK = timezone(timedelta(hours=9), "KST")
PREVIEW_LIMIT = 120
# Each period is (label, days before today); None clears the date range.
_PERIODS = (("오늘", 0), ("7일", 6), ("30일", 29), ("전체", None))
_STYLE = (
    "body{font:16px sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem}"
    "li{margin:.5rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
)
_NAV = '<nav><a href="/">Sessions</a> · <a href="/search">Search</a></nav>'
_SIMPLE_ROUTES = {
    "/": "list", "/api/sessions": "list", "/search": "search", "/api/search": "search"
}


def default_timezone(name=DEFAULT_TIMEZONE):
    """Return the requested timezone; use fixed +09:00 KST when tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except (ImportError, KeyError, OSError, ValueError):
        return KST_FALLBACK


def _to_utc(value):
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_local_time(value, now=None, tz=None):
    """Format a stored UTC timestamp for the dashboard.

    Missing values become an empty string and invalid values are shown as-is. Valid
    values use '오늘 HH:MM', '어제 HH:MM', or 'YYYY-MM-DD HH:MM'. Naive values
    are treated as UTC.
    """
    if tz is None:
        tz = default_timezone()
    moment = _to_utc(value)
    if moment is None:
        return "" if value is None else str(value)
    local = moment.astimezone(tz)
    reference = _to_utc(now) or datetime.now(timezone.utc)
    today = reference.astimezone(tz).date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        return "오늘 " + clock
    if local.date() == today - timedelta(days=1):
        return "어제 " + clock
    return local.strftime("%Y-%m-%d %H:%M")


def _local_now(tz):
    """Return local now; tests patch this helper to make date labels stable."""
    return datetime.now(tz)


def preview_text(text, limit=PREVIEW_LIMIT):
    """Collapse whitespace and truncate a session preview to the requested limit."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _segments(path):
    return [part for part in str(path).replace("\\", "/").split("/") if part]


def project_labels(paths):
    """Map paths to distinct short suffixes, expanding colliding labels."""
    distinct = {path for path in paths if path}
    segments_by_path = {path: _segments(path) for path in distinct}
    depths = {path: 1 for path in distinct}
    while True:
        labels = {}
        for path, segments in segments_by_path.items():
            depth = depths[path]
            labels[path] = "/".join(segments[-depth:]) if segments else path
        owners = {}
        for path, label in labels.items():
            owners.setdefault(label, []).append(path)
        collisions = [owners_list for owners_list in owners.values() if len(owners_list) > 1]
        if not collisions:
            return labels
        changed = False
        for owners_list in collisions:
            for path in owners_list:
                if depths[path] < len(segments_by_path[path]):
                    depths[path] += 1
                    changed = True
        if not changed:
            # Preserve already distinct labels and use raw paths for unresolved collisions.
            for owners_list in collisions:
                for path in owners_list:
                    labels[path] = path
            return labels


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _json(data):
    return json.dumps(data, ensure_ascii=False)


def render_page(title, body, page_class="content"):
    """Build the common document frame for every HTML screen."""
    head = '<meta charset="utf-8"><title>{}</title><style>{}</style>'.format(_e(title), _STYLE)
    return ('<!doctype html><html><head>{}</head><body><header class="site-header">{}</header>'
            '<main class="page page-{}"><h1>{}</h1>{}</main></body></html>').format(
                head, _NAV, _e(page_class), _e(title), body)


def _page(title, body):
    return render_page(title, body)


def _param(query, name):
    return query.get(name, [""])[0]


def _project(query):
    return _param(query, "project").strip() or None


def _limit(query):
    try:
        return max(1, min(1000, int(query.get("limit", ["100"])[0])))
    except (TypeError, ValueError):
        return 100


def _parse_day(text):
    try:
        return date.fromisoformat(text.strip())
    except (AttributeError, TypeError, ValueError):
        return None


def _day_text(day):
    return day.isoformat() if day else ""


def _filters(query):
    """Parse project, date, and text filters; invalid dates become None."""
    return {"project": _project(query), "from": _parse_day(_param(query, "from")),
            "to": _parse_day(_param(query, "to")), "q": _param(query, "q").strip()}


def _utc_bound(day, tz, days=0):
    if day is None:
        return None
    try:
        moment = datetime.combine(day + timedelta(days=days), dt_time(0), tzinfo=tz)
        return moment.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError):
        return None


def _filtered_rows(index, filters, limit, tz):
    """Fetch filtered session rows using UTC bounds derived from local dates."""
    options = {"project": filters["project"], "limit": limit, "exclude_worktrees": True,
               "start": _utc_bound(filters["from"], tz),
               "end": _utc_bound(filters["to"], tz, days=1)}
    if filters["q"]:
        return index.search(filters["q"], **options)
    return index.list_sessions(**options)


def _project_label(row):
    return row.get("project_path") or row.get("project_dir") or ""


def _row_item(row, labels, now, tz):
    session_id = str(row["session_id"])
    project = _project_label(row)
    when = format_local_time(row.get("last_timestamp"), now, tz)
    link = ('<a class="session-title" href="/session/{}">'
            '<span class="session-preview">{}</span></a>').format(
                quote(session_id, safe=""), _e(preview_text(row.get("title")) or session_id))
    return ('<li class="session-card">{} · <span class="session-count">메시지 {}개</span> · '
            '<span class="session-time">{}</span> · <span title="{}">{}</span></li>').format(
                link, _e(row.get("message_count") or 0), _e(when), _e(project),
                _e(labels.get(project, project)))


def _rows(rows, labels=None, now=None, tz=None):
    if labels is None:
        labels = project_labels([_project_label(row) for row in rows])
    return '<ul class="session-list">{}</ul>'.format(
        "".join(_row_item(row, labels, now, tz) for row in rows))


def _list_url(filters, start, end):
    pairs = [("project", filters["project"] or ""), ("from", start), ("to", end),
             ("q", filters["q"])]
    pairs = [(key, value) for key, value in pairs if value]
    return "/?" + urlencode(pairs) if pairs else "/"


def _period_links(filters, now):
    today = now.date()
    links = []
    for label, days in _PERIODS:
        start = end = ""
        if days is not None:
            start, end = (today - timedelta(days=days)).isoformat(), today.isoformat()
        links.append('<a href="{}">{}</a>'.format(_e(_list_url(filters, start, end)), label))
    return '<p class="period-links">{}</p>'.format(" · ".join(links))


def _filter_form(filters, projects, labels):
    current = filters["project"] or ""
    options = ['<option value="">전체 프로젝트</option>']
    known = set()
    for item in projects:
        value = item["project"] or ""
        known.add(value)
        options.append('<option value="{0}" title="{0}"{1}>{2} ({3})</option>'.format(
            _e(value), " selected" if value == current else "",
            _e(labels.get(value, value)), int(item["count"])))
    if current and current not in known:
        options.append('<option value="{0}" title="{0}" selected>{0}</option>'.format(_e(current)))
    return ('<form class="filter-bar" method="get" action="/"><label>프로젝트 '
            '<select class="project-select" name="project">{}</select></label> '
            '<label>시작 <input type="date" name="from" value="{}"></label> '
            '<label>끝 <input type="date" name="to" value="{}"></label> '
            '<label>검색 <input type="search" name="q" value="{}"></label> '
            '<button>필터</button></form>').format(
                "".join(options), _day_text(filters["from"]), _day_text(filters["to"]),
                _e(filters["q"]))


def _listing(rows, filters, projects, now, tz):
    """Compatibility wrapper around render_list_page."""
    return render_list_page(rows, filters, projects, now, tz)


def _search_form(term, project, limit):
    fields = ('<input name="q" value="{}"><input name="project" value="{}">'
              '<input name="limit" value="{}">').format(
                  _e(term), _e(project or ""), _e(limit))
    return '<form class="search-form">{}<button>Search</button></form>'.format(fields)


def _candidates_html(matches):
    items = "".join("<li>{} · {}</li>".format(
        _e(row["session_id"]), _e(row["file_path"])) for row in matches)
    return "<p>Several sessions match; use a longer prefix.</p><ul>{}</ul>".format(items)


def _detail_html(row):
    items = "".join("<dt>{}</dt><dd><pre>{}</pre></dd>".format(
        _e(key), _e(value)) for key, value in row.items())
    return '<dl class="session-detail">{}</dl>'.format(items)


def render_empty_result(reset_href):
    """Render the empty state and a link to clear the current search/filter."""
    return ('<div class="empty-state">조건에 맞는 세션이 없습니다 '
            '<a class="filter-reset" href="{}">필터 초기화</a></div>').format(_e(reset_href))


def render_list_page(rows, filters, projects, now, tz):
    labels = project_labels([item["project"] for item in projects] +
                            [_project_label(row) for row in rows])
    # Groups retain the newest-first order of their first row.
    groups = {}
    for row in rows:
        groups.setdefault(_project_label(row), []).append(row)
    sections = "".join(
        '<section class="project-group"><h2 class="project-name" title="{}">'
        '{}</h2>{}</section>'.format(
            _e(name), _e(labels.get(name, name)), _rows(items, labels, now, tz))
        for name, items in groups.items())
    listing = ('<div class="project-list">{}</div>'.format(sections) if sections else
               render_empty_result("/"))
    body = _filter_form(filters, projects, labels) + _period_links(filters, now) + listing
    return render_page("Sessions", body, "list")


def render_search_page(term, project, limit, rows, now, tz):
    if rows:
        results = _rows(rows, None, now, tz)
    elif term.strip():
        results = render_empty_result("/search")
    else:
        results = "<ul></ul>"
    return render_page("Search", _search_form(term, project, limit) + results, "search")


def render_detail_page(row):
    return render_page("Session " + str(row["session_id"]), _detail_html(row), "detail")


def render_candidates_page(matches):
    return render_page("Ambiguous session", _candidates_html(matches), "candidates")


def render_error_page(title, message):
    return render_page(title, "<p>{}</p>".format(_e(message)), "error")


def render_db_unavailable_page(message):
    return render_page(
        "Database unavailable", '<div class="db-unavailable">{}</div>'.format(_e(message)),
        "db-unavailable")


def _error(code, message, api):
    if api:
        return code, _json({"error": message}), JSON_TYPE
    return code, render_error_page("Error", message), HTML_TYPE


def _db_unavailable(api):
    if api:
        return 503, _json({"error": DB_UNAVAILABLE}), JSON_TYPE
    return 503, render_db_unavailable_page(DB_UNAVAILABLE), HTML_TYPE


def _route(path):
    """Resolve a request path before opening the database."""
    api = path.startswith("/api/")
    if path in _SIMPLE_ROUTES:
        return _SIMPLE_ROUTES[path], None, api
    # Nested session paths deliberately fall through to 404.
    for prefix in ("/api/session/", "/session/"):
        if path.startswith(prefix):
            rest = path[len(prefix):]
            if rest and "/" not in rest:
                return "detail", rest, api
            break
    return None, None, api


def _list_response(index, query, api):
    tz = default_timezone()
    now = _local_now(tz)
    filters = _filters(query)
    rows = _filtered_rows(index, filters, _limit(query), tz)
    if api:
        for row in rows:
            row["preview"] = preview_text(row.get("title"))
            row["last_activity_local"] = format_local_time(row.get("last_timestamp"), now, tz)
        return 200, _json(rows), JSON_TYPE
    projects = index.project_counts(exclude_worktrees=True)
    return 200, render_list_page(rows, filters, projects, now, tz), HTML_TYPE


def _search_response(index, query, api):
    term, project, limit = _param(query, "q"), _project(query), _limit(query)
    rows = (index.search(term, project=project, limit=limit, exclude_worktrees=True)
            if term.strip() else [])
    if api:
        return 200, _json(rows), JSON_TYPE
    tz = default_timezone()
    return 200, render_search_page(term, project, limit, rows, _local_now(tz), tz), HTML_TYPE


def _detail_response(index, prefix, api):
    matches = index.find_sessions(prefix, exclude_worktrees=True)
    if not matches:
        return _error(404, "No session matches: " + prefix, api)
    if len(matches) > 1:
        if api:
            return 409, _json({"error": "Session prefix is ambiguous: " + prefix,
                              "candidates": matches}), JSON_TYPE
        return 409, render_candidates_page(matches), HTML_TYPE
    row = matches[0]
    if api:
        return 200, _json(row), JSON_TYPE
    return 200, render_detail_page(row), HTML_TYPE


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
                result = _db_unavailable(api)
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
