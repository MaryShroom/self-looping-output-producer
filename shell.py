"""The bash tool: run a shell command after the user approves it.

IMPORTANT: this is NOT a sandbox. An approved command runs with your user's
permissions and can read, change or delete anything you can, anywhere on the
machine (it merely *starts* in the project folder). The approval prompt is the
safety boundary, so read each command before you press y. For real autonomy, run
SLOP inside a container or VM.

Guard rails that do exist:
  - every command needs approval (no "yes to all" answer at the prompt)
  - risky-looking commands get a warning in the prompt (a hint, not a guarantee)
  - API keys and other secret-looking environment variables are removed from
    the command's environment
  - stdin is closed, so interactive programs fail instead of hanging
  - a timeout kills the command and everything it started
  - long output is shortened before it goes back to the model
Linux and macOS only (uses process groups).
"""
import os
import re
import shutil
import signal
import subprocess
import sys

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
HEAD_CHARS = 6000
TAIL_CHARS = 4000

# When True, commands run without asking (still printed). Not settable from the
# prompt on purpose; a future startup switch can set it.
auto_approve = False

DECLINED = (
    "error: the user declined to run this command. Do not retry it; "
    "ask the user what they would like instead."
)

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

# (pattern, warning shown in the approval prompt)
RISKY = [
    (r"\bsudo\b|\bsu\s", "uses elevated privileges"),
    (r"\brm\s+-[a-zA-Z]*[rRf]", "deletes files recursively or without asking"),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", "pipes a download into a shell"),
    (r"\bgit\s+(push|reset\s+--hard|clean|rebase|checkout\s+--)", "changes git history or the remote"),
    (r"\b(dd|mkfs\w*|fdisk|shred)\b", "low-level disk operation"),
    (r"\b(chmod|chown)\s+-R\b", "recursive permission change"),
    (r"(^|[\s;&|])(\.\./|cd\s+[/~])", "reaches outside the project folder"),
    (r"\b(shutdown|reboot|halt|poweroff|kill(all)?)\b", "stops processes or the machine"),
]


def touches_secrets_file(command: str) -> bool:
    """Heuristic: does any word in the command name a .env file (except .env.example)?"""
    for word in re.findall(r"[^\s'\"=<>|;&()]+", command):
        name = word.rsplit("/", 1)[-1]
        if name.startswith(".env") and name != ".env.example":
            return True
    return False


def warnings_for(command: str) -> list[str]:
    found = [msg for pattern, msg in RISKY if re.search(pattern, command)]
    if touches_secrets_file(command):
        found.append("touches a .env secrets file")
    return found


def clean_env() -> dict:
    """Environment for the child: no API keys or other secret-looking variables."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(hint in k.upper() for hint in SECRET_HINTS)
    }
    env.update(PAGER="cat", GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0", TERM="dumb")
    return env


def truncate(text: str) -> str:
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text
    omitted = len(text) - HEAD_CHARS - TAIL_CHARS
    return f"{text[:HEAD_CHARS]}\n[... {omitted} characters omitted ...]\n{text[-TAIL_CHARS:]}"


# --- approval ---------------------------------------------------------------------

def _style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def _describe(command: str, timeout: int, cwd, warnings: list[str]) -> str:
    lines = [f"Run command (timeout {timeout}s, starting in {cwd or '.'}):"]
    lines += [f"  $ {line}" for line in command.splitlines()]
    lines += [_style(f"  ! warning: {w}", "33") for w in warnings]
    return "\n".join(lines)


def _prompt_user(command: str, timeout: int, cwd, warnings: list[str]) -> bool:
    print(_describe(command, timeout, cwd, warnings))
    try:
        answer = input("Run this command? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


confirm_fn = _prompt_user  # replaceable (tests, or a different UI later)


def _approve(command: str, timeout: int, cwd, warnings: list[str]) -> bool:
    if auto_approve:
        print(_describe(command, timeout, cwd, warnings))
        return True
    return confirm_fn(command, timeout, cwd, warnings)


# --- running ------------------------------------------------------------------------------

def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the command and everything it started
    except (ProcessLookupError, PermissionError):
        pass


def _collect(proc: subprocess.Popen) -> str:
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        out = b""
    return out.decode("utf-8", errors="replace")


def run_command(command: str, timeout=DEFAULT_TIMEOUT, cwd=None) -> str:
    command = (command or "").strip()
    if not command:
        raise ValueError("command must not be empty")
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    if not _approve(command, timeout, cwd, warnings_for(command)):
        return DECLINED

    proc = subprocess.Popen(
        [shutil.which("bash") or "/bin/sh", "-c", command],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=clean_env(),
        start_new_session=True,  # own process group, so a timeout can kill it all
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        text = truncate(out.decode("utf-8", errors="replace"))
        return f"exit code: {proc.returncode}\n{text}".rstrip()
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        text = truncate(_collect(proc))
        return f"error: command timed out after {timeout}s and was killed\n{text}".rstrip()
    except KeyboardInterrupt:
        _kill_group(proc)
        _collect(proc)
        return "error: the user interrupted the command (Ctrl-C)"
