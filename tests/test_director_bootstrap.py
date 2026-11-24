"""Initial syntax smoke test; NOT product behavior acceptance."""
import ast
from pathlib import Path

def test_existing_python_source_parses():
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob('*.py'):
        if any(part in {'.git', '.agents', '.venv', 'venv', '.director_worktrees', '__pycache__'} for part in path.relative_to(root).parts):
            continue
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
