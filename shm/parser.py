"""Read-only parsing of Claude Code session JSONL files."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class SessionMeta:
    session_id: str
    project_dir: str
    project_path: Optional[str]
    file_path: str
    first_timestamp: Optional[str]
    last_timestamp: Optional[str]
    message_count: int
    user_message_count: int
    title: str
    file_mtime: float
    file_size: int
    user_text: str = ""
    malformed_lines: int = 0


def claude_root(root=None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    return Path(os.environ.get("SHM_CLAUDE_DIR", "~/.claude/projects")).expanduser()


def discover_sessions(root=None) -> Iterator[Path]:
    """Yield session JSONL files under encoded project directories."""
    base = claude_root(root)
    if not base.is_dir():
        return
    for project in sorted(base.iterdir()):
        if project.is_dir():
            yield from sorted(project.glob("*.jsonl"))


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return value


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return " ".join(parts)
    return ""


def parse_session(file_path) -> SessionMeta:
    path = Path(file_path)
    stat = path.stat()
    session_id = path.stem
    project_dir = path.parent.name
    project_path = None
    timestamps = []
    message_count = user_count = malformed = 0
    user_parts = []
    first_prompt = ""
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                malformed += 1
                continue
            if not isinstance(record, dict):
                continue
            cwd = record.get("cwd")
            if not project_path and isinstance(cwd, str) and cwd:
                project_path = cwd
            stamp = _timestamp(record.get("timestamp"))
            if stamp:
                timestamps.append(stamp)
            kind = record.get("type")
            message = record.get("message")
            if kind in ("user", "assistant") and isinstance(message, dict):
                message_count += 1
                if kind == "user":
                    text = _content_text(message.get("content")).strip()
                    if text:
                        user_count += 1
                        user_parts.append(text)
                        if not first_prompt:
                            first_prompt = text
    title = " ".join(first_prompt.split())[:200]
    return SessionMeta(
        session_id=session_id, project_dir=project_dir, project_path=project_path,
        file_path=str(path), first_timestamp=min(timestamps) if timestamps else None,
        last_timestamp=max(timestamps) if timestamps else None,
        message_count=message_count, user_message_count=user_count, title=title,
        file_mtime=stat.st_mtime, file_size=stat.st_size,
        user_text="\n".join(user_parts), malformed_lines=malformed,
    )


def parse_sessions(root=None):
    return [parse_session(path) for path in discover_sessions(root)]


def read_messages(file_path, limit=400, max_chars=20000):
    """Return the conversation of one session file as ([{role, text, timestamp}], truncated).

    Read-only. Only user/assistant records with visible text are kept (tool calls and
    tool results carry no text block and are skipped). ``truncated`` is True when more
    than ``limit`` such messages exist.
    """
    messages, truncated = [], False
    with Path(file_path).open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            text = _content_text(message.get("content")).strip()
            if not text:
                continue
            if len(messages) >= limit:
                truncated = True
                break
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…"
            messages.append({"role": record["type"], "text": text,
                             "timestamp": _timestamp(record.get("timestamp"))})
    return messages, truncated
