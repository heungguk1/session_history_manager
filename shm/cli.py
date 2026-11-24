"""Command-line interface for the session history manager."""
import argparse
import json
import sys

from .indexer import SessionIndex


def _parser():
    parser = argparse.ArgumentParser(prog="shm", description="Index and search Claude Code session history.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="SQLite database path (default: $SHM_DB_PATH or ~/.claude/session-history.sqlite3)")
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", parents=[common], help="build or update the session index")
    index.add_argument("--full", action="store_true", help="reindex every session file")
    index.add_argument("--root", help="Claude projects directory (default: $SHM_CLAUDE_DIR or ~/.claude/projects)")
    for name, help_text in (("list", "list indexed sessions"), ("search", "search indexed sessions")):
        command = commands.add_parser(name, parents=[common], help=help_text)
        if name == "search":
            command.add_argument("query", nargs="+", help="keyword search terms")
        command.add_argument("--project", help="filter by project directory or path")
        command.add_argument("--limit", type=int, default=100, help="maximum sessions to show")
        command.add_argument("--json", action="store_true", dest="as_json", help="print a JSON array")
    show = commands.add_parser("show", parents=[common], help="show metadata for one session")
    show.add_argument("session", help="session id or unique prefix")
    show.add_argument("--json", action="store_true", dest="as_json", help="print JSON")
    return parser


def _short_id(value):
    return value[:8]


def _display(rows):
    for row in rows:
        print("\t".join((str(_short_id(row["session_id"])), str(row.get("last_timestamp") or ""), str(row.get("project_path") or row["project_dir"]), str(row.get("title") or ""))))


def _show(index, args):
    matches = index.find_sessions(args.session)
    if not matches:
        print("No session matches prefix: " + args.session, file=sys.stderr)
        return 2
    if len(matches) > 1:
        print("Session prefix is ambiguous: {} ({} matches)".format(args.session, len(matches)), file=sys.stderr)
        for row in matches:
            print("  " + row["file_path"], file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(matches[0], ensure_ascii=False))
    else:
        for key, value in matches[0].items():
            print("{}: {}".format(key, "" if value is None else value))
    return 0


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        with SessionIndex(args.db) as index:
            if args.command == "index":
                counts = index.index(args.root, incremental=not args.full)
                print("indexed: {indexed}, skipped: {skipped}, removed: {removed}".format(**counts))
                return 0
            if args.command == "show":
                return _show(index, args)
            if args.command == "list":
                rows = index.list_sessions(project=args.project, limit=max(0, args.limit))
            else:
                rows = index.search(" ".join(args.query), project=args.project, limit=max(0, args.limit))
        if args.as_json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            _display(rows)
        return 0
    except (OSError, ValueError, sqlite3_error()) as exc:
        print("shm: {}".format(exc), file=sys.stderr)
        return 2


def sqlite3_error():
    import sqlite3
    return sqlite3.Error


if __name__ == "__main__":
    sys.exit(main())
