"""Tools the model can call: read_file, list_files, grep, glob, write_file, edit_file, bash.

Every change to a file goes through an approval step that shows a diff first;
every shell command needs approval too (see shell.py).
"""
import difflib
import os
import sys
from pathlib import Path

import search
import shell

ROOT = Path.cwd().resolve()  # the agent only touches files under where you launch it
MAX_CHARS = 20_000          # most text a single read_file call returns
DEFAULT_READ_LINES = 500    # lines per read_file call unless `limit` says otherwise
MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000       # longer lines are clipped (minified files, data blobs)
MAX_READ_SCAN = 50_000_000  # characters read past when skipping ahead to `offset`
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


def _visible(text: str) -> str:
    """Show control characters (escape sequences, bidi overrides, ...) as \\x1b-style text.
    Otherwise file contents could redraw the terminal and disguise what you are approving."""
    out = []
    for c in text:
        if c.isprintable() or c == "\t":
            out.append(c)
        else:
            out.append(f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}")
    return "".join(out)


def visible_text(text: str) -> str:
    """Like _visible, for multi-line text such as the model's replies: line breaks and tabs
    stay, everything else that could control the terminal is shown as \\x1b-style text."""
    return "\n".join(_visible(line) for line in text.split("\n"))


def _describe_new(rel: str, text: str) -> str:
    lines = text.splitlines()
    body = _clip(["+" + _visible(line) for line in lines])
    return "\n".join([f"Create {_visible(rel)} ({len(lines)} lines)"] + [_color(x) for x in body])


def _describe_change(rel: str, old: str, new: str) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm=""
        )
    )
    return "\n".join([f"Modify {_visible(rel)}"] + [_color(_visible(x)) for x in _clip(diff)])


# --- showing tool results in the terminal ----------------------------------------------

SHOW_LINES = 8
SHOW_WIDTH = 120


def format_preview(result: str, max_lines: int = SHOW_LINES, width: int = SHOW_WIDTH) -> str:
    """A short excerpt of a tool result for the terminal, so you can see what the model was
    actually told (not just its summary). Set SLOP_SHOW_OUTPUT=off to hide it."""
    if os.environ.get("SLOP_SHOW_OUTPUT", "on").strip().lower() in ("off", "0", "false", "no"):
        return ""
    lines = result.splitlines() or ["(no output)"]
    shown = []
    for line in lines[:max_lines]:
        line = _visible(line)
        shown.append(line if len(line) <= width else line[: width - 1] + "…")
    if len(lines) > max_lines:
        shown.append(f"... ({len(lines) - max_lines} more lines)")
    text = "\n".join("    | " + line for line in shown)
    return f"\033[2m{text}\033[0m" if sys.stdout.isatty() else text


class FailureStreak:
    """Counts consecutive failed tool calls so a model stuck in a loop can be stopped.

    A failure is a result starting with "error:" (bad arguments, missing file, ...).
    A command that runs and exits non-zero is not a failure, and neither is the user
    declining a change. Any other result resets the count.
    """

    def __init__(self, limit: int = 4):
        self.limit = limit
        self.count = 0

    def record(self, result: str) -> bool:
        """Record one tool result. Returns True when the limit has been reached."""
        if result.startswith("error:") and "the user declined" not in result[:60]:
            self.count += 1
        else:
            self.count = 0
        return self.count >= self.limit


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

def read_file(path: str, offset=1, limit=DEFAULT_READ_LINES) -> str:
    """Read part of a text file, never more than MAX_CHARS, without loading all of it."""
    target = _safe(path)
    if _is_secret(target):
        raise ValueError("refusing to read a secrets file")
    if not target.exists():
        raise ValueError(f"file not found: {path}")
    if target.is_dir():
        raise ValueError("path is a folder; use list_files")
    if not target.is_file():
        raise ValueError("not a regular file")       # pipes and devices could block forever
    offset, limit = int(offset), int(limit)
    if offset < 1:
        raise ValueError("offset must be 1 or more (line numbers start at 1)")
    limit = max(1, min(limit, MAX_READ_LINES))

    with target.open("rb") as raw:
        if b"\0" in raw.read(8192):
            raise ValueError("binary file, not text")

    out, chars, scanned, line_no, next_line = [], 0, 0, 0, None
    with target.open(encoding="utf-8", errors="replace", newline="") as f:
        while True:
            chunk = f.readline(MAX_LINE_CHARS + 1)       # a huge single line cannot blow up memory
            if not chunk:
                break
            line_no += 1
            scanned += len(chunk)
            clipped = len(chunk) > MAX_LINE_CHARS and not chunk.endswith(("\n", "\r"))
            while clipped:                                # throw away the rest of that line
                rest = f.readline(65536)
                scanned += len(rest)
                if not rest or rest.endswith(("\n", "\r")) or scanned > MAX_READ_SCAN:
                    break
            if scanned > MAX_READ_SCAN:
                raise ValueError("file is too large to jump to a line number; use grep to find it")
            if line_no < offset:
                continue
            text = chunk[:MAX_LINE_CHARS] + "…[line clipped]\n" if clipped else chunk
            if len(out) >= limit or (out and chars + len(text) > MAX_CHARS):
                next_line = line_no
                break
            out.append(text)
            chars += len(text)

    if not out:
        if line_no == 0:
            return "(empty file)"
        raise ValueError(f"offset {offset} is past the end of the file ({line_no} lines)")
    body = "".join(out)
    if body[-1] not in "\r\n" and (next_line or offset > 1):
        body += "\n"
    last = offset + len(out) - 1
    if next_line:
        body += f"[lines {offset}-{last} shown, more follow: call read_file again with offset={next_line}]"
    elif offset > 1:
        body += f"[lines {offset}-{last}, end of file]"
    return body


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


def grep_tool(pattern: str, path: str = ".", include=None, ignore_case=False, literal=False,
              max_results=search.DEFAULT_GREP_RESULTS) -> str:
    target = _safe(path)
    if _is_secret(target):
        raise ValueError("refusing to search a secrets file")
    return search.grep_files(ROOT, target, pattern, include, ignore_case, literal, max_results)


def glob_tool(pattern: str, path: str = ".", max_results=search.DEFAULT_GLOB_RESULTS) -> str:
    return search.find_files(ROOT, _safe(path), pattern, max_results)


def bash(command: str, timeout: int = shell.DEFAULT_TIMEOUT) -> str:
    return shell.run_command(command, timeout, cwd=ROOT)


_IMPLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "edit_file": edit_file,
    "grep": grep_tool,
    "glob": glob_tool,
    "bash": bash,
}

# Neutral JSON-schema tool descriptions (also valid for most other providers).
TOOL_SPECS = [
    {
        "name": "read_file",
        "description": (
            "Read a text file. Returns up to 500 lines per call (at most 20000 characters); "
            "when more follow, the result ends with the offset to use next."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "offset": {"type": "integer", "description": "Line number to start at (first line is 1). Default 1."},
                "limit": {"type": "integer", "description": "Maximum number of lines to return (default 500, max 2000)."},
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
            "package managers. Do not use it to read, search or edit files (use read_file, "
            "list_files, grep, glob and edit_file) or to look for secrets. There is no "
            "interactive input, so "
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
    {
        "name": "grep",
        "description": (
            "Search the contents of files for a regular expression (Python syntax). Returns "
            "matching lines as 'path:line: text'. Skips .git, virtual environments, "
            "node_modules, binary files, files over 1 MB and .env files. Use `include` to limit "
            "which files are searched (for example '*.py'); use read_file to see a file in full."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "path": {
                    "type": "string",
                    "description": "Folder or file to search, relative to the project root. Defaults to '.'.",
                },
                "include": {
                    "type": "string",
                    "description": "Only search files matching this glob, e.g. '*.py' or 'src/**/*.ts'.",
                },
                "ignore_case": {"type": "boolean", "description": "Case-insensitive. Default false."},
                "literal": {
                    "type": "boolean",
                    "description": "Treat pattern as plain text instead of a regular expression. Default false.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum matching lines to return (default 100, max 500).",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob",
        "description": (
            "Find files by name pattern. Examples: '**/*.py' (all Python files), "
            "'tests/test_*.py', '*.{md,txt}'. A pattern without '/' (like '*.py') matches file "
            "names at any depth. Returns paths relative to the project root; skips .git, "
            "virtual environments and node_modules."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'."},
                "path": {
                    "type": "string",
                    "description": "Folder to search in, relative to the project root. Defaults to '.'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum paths to return (default 200, max 1000).",
                },
            },
            "required": ["pattern"],
        },
    },
]


_SPEC_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}


def _wrong_type(value, expected) -> bool:
    """Does `value` not fit the JSON-schema type? Lenient where providers differ: Gemini sends
    whole numbers as floats (30.0), and some models send numbers as strings ("30")."""
    if expected == "string":
        return not isinstance(value, str)
    if expected == "boolean":
        return not (isinstance(value, bool) or (isinstance(value, str) and value.lower() in ("true", "false")))
    if expected == "integer":
        if isinstance(value, bool):
            return True
        if isinstance(value, int):
            return False
        if isinstance(value, float):
            return not value.is_integer()
        if isinstance(value, str):
            try:
                int(value)
                return False
            except ValueError:
                return True
        return True
    return False


def _check_args(name: str, args: dict):
    """Clear error for wrong or missing arguments (models, small ones especially, invent them)."""
    params = _SPEC_BY_NAME[name]["parameters"]
    valid = sorted(params.get("properties", {}))
    required = params.get("required", [])
    unknown = sorted(set(args) - set(valid))
    missing = [r for r in required if r not in args]
    if not unknown and not missing:
        for key, value in args.items():
            expected = params["properties"][key].get("type")
            if _wrong_type(value, expected):
                hint = (" Pass each argument directly, not nested inside another object."
                        if isinstance(value, dict) else "")
                return (f"error: {name}: argument '{key}' must be of type {expected} "
                        f"but got {type(value).__name__}.{hint}")
        return None
    problems = []
    if unknown:
        problems.append(f"unknown argument(s) {unknown}")
    if missing:
        problems.append(f"missing required argument(s) {missing}")
    return (
        f"error: {name}: " + " and ".join(problems)
        + f". Valid arguments: {valid} (required: {required or 'none'})"
    )


def run_tool(name: str, args: dict) -> str:
    fn = _IMPLS.get(name)
    if fn is None:
        return f"error: unknown tool {name}"
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return f"error: {name}: arguments must be an object, got {type(args).__name__}"
    args = {k: v for k, v in args.items() if v is not None}   # null means "not given"
    problem = _check_args(name, args)
    if problem:
        return problem
    try:
        return fn(**args)
    except Exception as e:  # report failures to the model instead of crashing
        return f"error: {e}"
