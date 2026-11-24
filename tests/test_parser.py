import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shm.parser import parse_session, parse_sessions


def write_session(root, project, session_id, records):
    directory = Path(root) / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (session_id + ".jsonl")
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


class ParserTests(unittest.TestCase):
    def test_parses_session_metadata_and_block_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_session(root, "-work-demo", "session-1", [
                {"type": "user", "sessionId": "ignored", "cwd": "/work/demo", "timestamp": "2024-01-02T10:00:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "  Build "}, {"type": "tool_result", "content": "x"}, {"type": "text", "text": "the index  "}]}},
                {"type": "assistant", "timestamp": "2024-01-02T10:01:00Z", "message": {"role": "assistant", "content": "Done"}},
                {"type": "user", "timestamp": "2024-01-02T10:02:00Z", "message": {"role": "user", "content": "Another request"}},
            ])
            stat = path.stat()
            meta = parse_session(path)
            self.assertEqual((meta.session_id, meta.project_dir, meta.project_path), ("session-1", "-work-demo", "/work/demo"))
            self.assertEqual(meta.file_path, str(path))
            self.assertEqual((meta.first_timestamp, meta.last_timestamp), ("2024-01-02T10:00:00+00:00", "2024-01-02T10:02:00+00:00"))
            self.assertEqual((meta.message_count, meta.user_message_count), (3, 2))
            self.assertEqual(meta.title, "Build the index")
            # Text blocks are joined with a single space, then stripped.
            self.assertEqual(meta.user_text, "Build  the index\nAnother request")
            self.assertEqual((meta.file_mtime, meta.file_size), (stat.st_mtime, stat.st_size))

    def test_skips_blank_and_malformed_lines_and_ignores_unknown_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = write_session(temp, "project", "s", [])
            path.write_text('\nnot json\n{"type":"summary"}\n[]\n{"type":"user"}\n{"type":"weird","message":{}}\n', encoding="utf-8")
            meta = parse_session(path)
            # Only 'not json' counts; the valid non-dict JSON line '[]' is skipped uncounted.
            self.assertEqual(meta.malformed_lines, 1)
            self.assertEqual(meta.message_count, 0)
            self.assertEqual(meta.user_message_count, 0)
            self.assertEqual(meta.title, "")
            self.assertEqual(meta.project_path, None)
            self.assertEqual(meta.first_timestamp, None)

    def test_title_is_first_prompt_truncated_to_200_characters(self):
        with tempfile.TemporaryDirectory() as temp:
            prompt = "x" * 240
            path = write_session(temp, "project", "s", [{"type": "user", "message": {"content": prompt}}])
            self.assertEqual(parse_session(path).title, "x" * 200)

    def test_environment_root_and_explicit_argument_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            env_root = Path(temp) / "env"
            arg_root = Path(temp) / "arg"
            write_session(env_root, "p", "env-session", [{"type": "user", "message": {"content": "env"}}])
            write_session(arg_root, "p", "arg-session", [{"type": "user", "message": {"content": "arg"}}])
            with patch.dict(os.environ, {"SHM_CLAUDE_DIR": str(env_root)}):
                self.assertEqual([m.session_id for m in parse_sessions()], ["env-session"])
                self.assertEqual([m.session_id for m in parse_sessions(arg_root)], ["arg-session"])

    def test_read_only_source_content_and_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            path = write_session(temp, "p", "s", [{"type": "user", "message": {"content": "hello"}}])
            before = (path.read_bytes(), path.stat().st_mtime_ns, set(path.parent.iterdir()))
            parse_session(path)
            parse_sessions(temp)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, set(path.parent.iterdir())), before)


if __name__ == "__main__":
    unittest.main()
