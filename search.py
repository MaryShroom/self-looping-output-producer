"""Read-only search tools: grep (search file contents) and glob (find files by name).

Plain Python, nothing to install. Guard rails:
  - never searches inside .env files (except .env.example)
  - skips .git, virtual environments, node_modules and caches
  - skips binary files and files over 1 MB (and says how many it skipped)
  - skips symlinks that point outside the project
  - caps the number of results and the length of each line shown
  - gives up after a time budget and says the results may be incomplete

Limit: the regular expression runs inside SLOP, so a pathological pattern (for example
nested quantifiers like "(a+)+$") can be slow on long lines. Lines are cut to 2000
characters before matching, which keeps this in check.
"""
import os
import re
import time
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
}
MAX_FILE_BYTES = 1_000_000
MAX_LINE_SCAN = 2000        # characters of a line that are searched
MAX_LINE_SHOWN = 200        # characters of a matching line that are shown
DEFAULT_GREP_RESULTS, MAX_GREP_RESULTS = 100, 500
DEFAULT_GLOB_RESULTS, MAX_GLOB_RESULTS = 200, 1000
TIME_BUDGET = 10.0          # seconds
MAX_FILES_VISITED = 200_000
MAX_BRACE_EXPANSIONS = 50


def _is_secret_name(name: str) -> bool:
    return name.startswith(".env") and name != ".env.example"


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "on")
    return bool(value)


# --- glob patterns --------------------------------------------------------------------------------

_BRACES = re.compile(r"\{([^{}]*)\}")


def expand_braces(pattern: str) -> list[str]:
    """'*.{py,js}' -> ['*.py', '*.js'] (nesting works too)."""
    m = _BRACES.search(pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out += expand_braces(pattern[: m.start()] + alt + pattern[m.end():])
        if len(out) > MAX_BRACE_EXPANSIONS:
            raise ValueError("too many {a,b} alternatives in the pattern")
    return list(dict.fromkeys(out))        # nested braces can produce repeats


def _translate(glob: str) -> str:
    """One glob (no braces) -> regular expression source."""
    i, n, out = 0, len(glob), []
    while i < n:
        c = glob[i]
        if c == "*":
            if glob[i:i + 2] == "**":
                j = i + 2
                if glob[j:j + 1] == "/":
                    out.append("(?:.*/)?")     # '**/' = zero or more folders
                    i = j + 1
                else:
                    out.append(".*")
                    i = j
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and glob[j] in "!^":
                j += 1
            if j < n and glob[j] == "]":
                j += 1
            while j < n and glob[j] != "]":
                j += 1
            if j >= n:                         # no closing bracket: a literal '['
                out.append(re.escape(c))
                i += 1
            else:
                body = glob[i + 1:j].replace("\\", "\\\\")
                if body and body[0] in "!^":
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def compile_glob(pattern: str):
    """Returns match(relative_posix_path) -> bool.

    A pattern without '/' (like '*.py') matches file names at any depth; a pattern with
    '/' (like 'tests/test_*.py' or '**/*.py') matches the path from the search folder.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    if pattern.startswith(("/", "~")):
        raise ValueError("pattern must be relative to the project folder")
    if ".." in pattern.replace("\\", "/").split("/"):
        raise ValueError("pattern must not contain '..'")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    compiled = []
    for alt in expand_braces(pattern):
        compiled.append(("/" not in alt, re.compile(_translate(alt))))

    def match(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        return any(rx.fullmatch(name if anywhere else rel) for anywhere, rx in compiled)

    return match


# --- walking ------------------------------------------------------------------------------------------

def iter_files(base: Path):
    """Files under base in a stable order, skipping SKIP_DIRS. Never follows folder symlinks."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


def _count(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word}" + ("es" if word.endswith(("ch", "s", "x")) else "s")


def _notes(*notes) -> str:
    notes = [n for n in notes if n]
    return "(" + "; ".join(notes) + ")" if notes else ""


# --- glob ------------------------------------------------------------------------------------------------

def find_files(root: Path, base: Path, pattern: str, max_results=DEFAULT_GLOB_RESULTS) -> str:
    match = compile_glob(pattern)
    limit = _clamp(max_results, 1, MAX_GLOB_RESULTS, DEFAULT_GLOB_RESULTS)
    if not base.is_dir():
        raise ValueError("path is not a folder")
    deadline = time.monotonic() + TIME_BUDGET
    found, visited, timed_out = [], 0, False
    for p in iter_files(base):
        visited += 1
        if visited > MAX_FILES_VISITED or time.monotonic() > deadline:
            timed_out = True
            break
        if match(p.relative_to(base).as_posix()):
            found.append(p.relative_to(root).as_posix())
    shown = found[:limit]
    more = f"{len(found) - limit} more not shown, narrow the pattern or path" if len(found) > limit else ""
    stopped = f"search stopped after {TIME_BUDGET:g}s, results may be incomplete" if timed_out else ""
    if not shown:
        return "\n".join(["no files match", _notes(stopped)]).strip()
    return "\n".join(shown + [n for n in (_notes(more, stopped),) if n])


# --- grep --------------------------------------------------------------------------------------------------

def grep_files(root: Path, base: Path, pattern: str, include=None, ignore_case=False,
               literal=False, max_results=DEFAULT_GREP_RESULTS) -> str:
    if not isinstance(pattern, str) or pattern == "":
        raise ValueError("pattern must not be empty")
    flags = re.IGNORECASE if _as_bool(ignore_case) else 0
    try:
        rx = re.compile(re.escape(pattern) if _as_bool(literal) else pattern, flags)
    except re.error as e:
        raise ValueError(f"invalid regular expression: {e}. Use literal=true to search for plain text.")
    include_match = compile_glob(include) if include else None
    limit = _clamp(max_results, 1, MAX_GREP_RESULTS, DEFAULT_GREP_RESULTS)

    if base.is_file():
        files, single = [base], True
    else:
        files, single = iter_files(base), False
    deadline = time.monotonic() + TIME_BUDGET
    results, matched_files, too_big, timed_out, truncated, visited = [], set(), 0, False, False, 0

    for p in files:
        visited += 1
        if visited > MAX_FILES_VISITED or time.monotonic() > deadline:
            timed_out = True
            break
        if _is_secret_name(p.name):
            continue
        rel_base = p.name if single else p.relative_to(base).as_posix()
        if include_match and not include_match(rel_base):
            continue
        if not _inside(p, root):                 # a symlink pointing out of the project
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                too_big += 1
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:                 # binary file
            continue
        rel = p.relative_to(root).as_posix()
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line[:MAX_LINE_SCAN]):
                text = line.strip()
                if len(text) > MAX_LINE_SHOWN:
                    text = text[:MAX_LINE_SHOWN] + "…"
                results.append(f"{rel}:{number}: {text}")
                matched_files.add(rel)
                if len(results) >= limit:
                    truncated = True
                    break
        if truncated:
            break

    big = f"skipped {_count(too_big, 'file')} larger than {MAX_FILE_BYTES // 1_000_000} MB" if too_big else ""
    stopped = f"search stopped after {TIME_BUDGET:g}s, results may be incomplete" if timed_out else ""
    if truncated:
        summary = _notes(
            f"stopped after {limit} matches; narrow it with `include` or a more specific path",
            big, stopped)
    elif results:
        summary = _notes(
            f"{_count(len(results), 'match')} in {_count(len(matched_files), 'file')}", big, stopped)
    else:
        return "\n".join(["no matches", _notes(big, stopped)]).strip()
    return "\n".join(results + [summary])
