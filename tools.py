"""Tools the model can call. Read-only for now; write/edit/bash come later."""
from pathlib import Path

ROOT = Path.cwd().resolve()  # the agent only touches files under where you launch it
MAX_CHARS = 20_000


def _safe(path: str) -> Path:
    p = (ROOT / path).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError("path is outside the working directory")
    return p


def read_file(path: str) -> str:
    text = _safe(path).read_text(errors="replace")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n...[truncated]"
    return text


def list_files(path: str = ".") -> str:
    entries = sorted(_safe(path).iterdir())
    return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries) or "(empty)"


_IMPLS = {"read_file": read_file, "list_files": list_files}

# Neutral JSON-schema tool descriptions (also valid for most other providers).
TOOL_SPECS = [
    {
        "name": "read_file",
        "description": "Read a text file and return its contents.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List the files and folders in a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to the project root. Defaults to '.'.",
                }
            },
        },
    },
]


def run_tool(name: str, args: dict) -> str:
    fn = _IMPLS.get(name)
    if fn is None:
        return f"error: unknown tool {name}"
    try:
        return fn(**args)
    except Exception as e:  # report failures to the model instead of crashing
        return f"error: {e}"
