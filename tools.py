"""Tools the model can call: read_file, list_files, write_file, edit_file.

Every change to a file goes through an approval step that shows a diff first.
"""
import difflib
import sys
from pathlib import Path

import shell

ROOT =Path.cwd().resolve()  # the agent only touches files under where you launch it
MAX_CHARS = 20_000
MAX_PREVIEW_LINES = 60

# When True, changes are still printed but applied without asking. Answering "a"
# at a prompt sets it for the rest of the session; a startup switch can set it too.
auto_approve = False

DECLINED = (
    "error: the user declined this change. Do not retry it; "
    "ask the user what they would like instead."
)


# --- path safety -----------------------------------------------------------

def _safe(path: str) -> Path:
    p = (ROOT / path).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError("path is outside the working directory")
    return p


def _is_secret(p: Path) -> bool:
    # Never let the model read API keys: .env, .env.local, ... (.env.example is fine)
    return p.name.startswith(".env") and p.name != ".env.example"


def _is_protected(p: Path) -> bool:
    """Files the model may never write: secrets and anything inside .git/."""
    return _is_secret(p) or ".git" in p.relative_to(ROOT).parts


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


# --- file helpers (keep line endings exactly as they are) -----------------------

def _read_text(p: Path) -> str:
    try:
        with p.open(encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError:
        raise ValueError("not a UTF-8 text file")


def _write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


# --- previews and approval ----------------------------------------------------------

def _color(line: str) -> str:
    if not sys.stdout.isatty():
        return line
    if line.startswith("+") and not line.startswith("+++"):
        return f"\033[32m{line}\033[0m"
    if line.startswith("-") and not line.startswith("---"):
        return f"\033[31m{line}\033[0m"
    return line


def _clip(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_PREVIEW_LINES:
        return lines
    return lines[:MAX_PREVIEW_LINES] + [f"... ({len(lines) - MAX_PREVIEW_LINES} more lines)"]


def _describe_new(rel: str, text: str) -> str:
    lines = text.splitlines()
    body = _clip(["+" + line for line in lines])
    return "\n".join([f"Create {rel} ({len(lines)} lines)"] + [_color(x) for x in body])


def _describe_change(rel: str, old: str, new: str) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm=""
        )
    )
    return "\n".join([f"Modify {rel}"] + [_color(x) for x in _clip(diff)])


def _prompt_user(summary: str) -> bool:
    global auto_approve
    print(summary)
    try:
        answer = input("Apply this change? [y/N/a = yes to all for this session] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer == "a":
        auto_approve = True
        return True
    return answer in ("y", "yes")


confirm_fn = _prompt_user  # replaceable (tests, or a different UI later)


def _approve(summary: str) -> bool:
    if auto_approve:
        print(summary)
        return True
    return confirm_fn(summary)


# --- tools -----------------------------------------------------------------

def read_file(path: str) -> str:
    target = _safe(path)
    if _is_secret(target):
        raise ValueError("refusing to read a secrets file")
    text = target.read_text(errors="replace")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n...[truncated]"
    return text


def list_files(path: str = ".") -> str:
    entries = sorted(_safe(path).iterdir())
    return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries) or "(empty)"


def write_file(path: str, content: str) -> str:
    target = _safe(path)
    if _is_protected(target):
        raise ValueError("refusing to write to a protected path (.env files and .git are off limits)")
    if target.is_dir():
        raise ValueError("path is a directory")
    rel = _rel(target)
    if target.exists():
        old = _read_text(target)
        if old == content:
            return f"no change: {rel} already has this content"
        summary = _describe_change(rel, old, content)
    else:
        summary = _describe_new(rel, content)
    if not _approve(summary):
        return DECLINED
    _write_text(target, content)
    return f"wrote {rel} ({len(content.encode('utf-8'))} bytes)"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    target = _safe(path)
    if _is_protected(target):
        raise ValueError("refusing to edit a protected path (.env files and .git are off limits)")
    if not target.is_file():
        raise ValueError("file not found (use write_file to create a new file)")
    if not old_text:
        raise ValueError("old_text must not be empty")
    if old_text == new_text:
        raise ValueError("old_text and new_text are identical")
    text = _read_text(target)
    if "\r\n" in text:  # file uses Windows line endings: match them
        old_text = old_text.replace("\r\n", "\n").replace("\n", "\r\n")
        new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
    count = text.count(old_text)
    if count == 0:
        raise ValueError(
            "old_text not found. It must match the file exactly, including whitespace; "
            "read the file again and copy the text precisely."
        )
    if count > 1:
        raise ValueError(
            f"old_text appears {count} times; include more surrounding lines so it is unique"
        )
    new = text.replace(old_text, new_text, 1)
    rel = _rel(target)
    if not _approve(_describe_change(rel, text, new)):
        return DECLINED
    _write_text(target, new)
    return f"edited {rel}"


def bash(command: str, timeout: int = shell.DEFAULT_TIMEOUT) -> str:
    return shell.run_command(command, timeout, cwd=ROOT)


_IMPLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "edit_file": edit_file,
    "bash": bash,
}

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
    {
        "name": "edit_file",
        "description": (
            "Change an existing file by replacing one exact piece of text. old_text must match "
            "the file exactly (including whitespace and indentation) and must appear exactly "
            "once; include a few surrounding lines to make it unique. Read the file first. "
            "Prefer this over write_file for changes to existing files. The user is asked to "
            "approve the change."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "old_text": {"type": "string", "description": "Exact text to replace (must be unique in the file)."},
                "new_text": {"type": "string", "description": "Text to put in its place."},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create a new file, or completely overwrite an existing one, with the given content. "
            "For small changes to an existing file use edit_file instead. The user is asked to "
            "approve the change."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "content": {"type": "string", "description": "The full new content of the file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "bash",
        "description": (
            "Run a shell command in the project folder and return its exit code and output "
            "(stdout and stderr combined). Use it for things like running tests, git, or "
            "package managers. Do not use it to read or edit files (use read_file, list_files "
            "and edit_file) or to search for secrets. There is no interactive input, so "
            "commands that prompt will fail; use non-interactive flags. The user must approve "
            "every command."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the command is killed (default 30, max 120).",
                },
            },
            "required": ["command"],
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
