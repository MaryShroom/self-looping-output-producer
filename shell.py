"""The bash tool: run a shell command after the user approves it.

Every command needs a "y" from the user. On top of that, commands can run inside a
bubblewrap (bwrap) sandbox:

  - the system folders (/usr, ...) are visible read-only
  - the project folder is the only place a command can write
  - your home folder is NOT visible (no ~/.ssh, no other projects)
  - .env files in the project are replaced by empty files
  - .git/hooks and .git/config are read-only
  - no network, unless you allow it
  - the command and everything it started die with it (timeout, Ctrl-C)

Sandbox mode (env SLOP_SANDBOX, or /sandbox at the prompt):
  auto  use bwrap when it works; otherwise run UNSANDBOXED and say so  (default)
  on    require bwrap; refuse to run commands if it is not working
  off   never use it

Without the sandbox, an approved command runs with your user's permissions and can
read, change or delete anything you can; the approval prompt is then the only
safety boundary. bubblewrap is not a VM: it shares your kernel.

Other guard rails (with or without the sandbox):
  - risky-looking commands get a warning in the prompt (a hint, not a guarantee)
  - API keys and other secret-looking environment variables are removed
  - stdin is closed, so interactive programs fail instead of hanging
  - a timeout kills the command and everything it started
  - long output is shortened before it goes back to the model
Linux and macOS only for the shell itself; bwrap is Linux only.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

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


# --- sandbox settings ----------------------------------------------------------------------

MODES = ("off", "auto", "on")
_MODE_ALIASES = {
    "bwrap": "on", "true": "on", "yes": "on", "1": "on",
    "false": "off", "no": "off", "none": "off", "0": "off",
}

# Set by the /sandbox command; None means "read the environment variable".
sandbox_mode = None
sandbox_network = None
_status_cache = None  # result of the last bwrap self-test


def current_mode() -> str:
    value = (sandbox_mode or os.environ.get("SLOP_SANDBOX") or "auto").strip().lower()
    value = _MODE_ALIASES.get(value, value)
    # An unrecognised value (a typo) fails closed rather than silently running unsandboxed.
    return value if value in MODES else "on"


def network_allowed() -> bool:
    if sandbox_network is not None:
        return sandbox_network
    value = (os.environ.get("SLOP_SANDBOX_NETWORK") or "off").strip().lower()
    return value in ("on", "1", "true", "yes")


def _extra_ro_paths() -> list[str]:
    raw = os.environ.get("SLOP_SANDBOX_RO", "")
    return [os.path.expanduser(p.strip()) for p in raw.split(":") if p.strip()]


# --- building the bwrap command line --------------------------------------------------------------

SYSTEM_DIRS = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")
ETC_FILES = (
    "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "alternatives", "ssl", "ca-certificates",
    "ca-certificates.conf", "pki", "resolv.conf", "hosts", "nsswitch.conf", "passwd",
    "group", "localtime", "os-release", "gitconfig", "terminfo",
)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
SECRET_SCAN_DEPTH = 3


def secret_files(root) -> list[str]:
    """.env files in the project (not .env.example) to hide from the sandbox."""
    root = Path(root)
    base = len(root.parts)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS] if depth < SECRET_SCAN_DEPTH else []
        for name in filenames:
            if name.startswith(".env") and name != ".env.example":
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def bwrap_argv(command: str, cwd, network: bool) -> list[str]:
    cwd = str(Path(cwd).resolve())
    a = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    if network:
        a.append("--share-net")
    a += ["--ro-bind", "/usr", "/usr"]
    for name in SYSTEM_DIRS:
        p = Path("/") / name
        if p.is_symlink():
            a += ["--symlink", os.readlink(p), f"/{name}"]
        elif p.is_dir():
            a += ["--ro-bind", str(p), f"/{name}"]
    for name in ETC_FILES:
        a += ["--ro-bind-try", f"/etc/{name}", f"/etc/{name}"]
    if network:  # resolv.conf is often a link into here
        a += ["--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve"]
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
    for extra in _extra_ro_paths():
        a += ["--ro-bind-try", extra, extra]
    a += ["--bind", cwd, cwd]  # the project: the only writable place
    for protected in (f"{cwd}/.git/hooks", f"{cwd}/.git/config"):
        a += ["--ro-bind-try", protected, protected]
    for secret in secret_files(cwd):
        a += ["--ro-bind", "/dev/null", secret]
    a += ["--setenv", "HOME", "/tmp/home", "--setenv", "TMPDIR", "/tmp", "--chdir", cwd]
    a += ["--", "bash", "-c", command]
    return a


def _userns_restricted() -> bool:
    try:
        return Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip() == "1"
    except OSError:
        return False


def _probe() -> tuple[bool, str]:
    if not shutil.which("bwrap"):
        return False, (
            "bubblewrap is not installed "
            "(Ubuntu: sudo apt install bubblewrap; Arch/CachyOS: sudo pacman -S bubblewrap)"
        )
    try:
        r = subprocess.run(
            bwrap_argv("true", os.getcwd(), network=False),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"bubblewrap could not be started: {e}"
    if r.returncode == 0:
        return True, "ok"
    lines = r.stderr.decode("utf-8", "replace").strip().splitlines()
    first = lines[-1] if lines else f"exit code {r.returncode}"
    hint = ""
    if _userns_restricted():
        hint = (" This Ubuntu restricts unprivileged user namespaces; "
                "add an AppArmor profile for bwrap (see SANDBOX.md).")
    return False, f"bubblewrap self-test failed: {first}.{hint}"


def bwrap_status(refresh: bool = False) -> tuple[bool, str]:
    """(works, detail). Cached; refresh=True runs the self-test again."""
    global _status_cache
    if _status_cache is None or refresh:
        _status_cache = _probe()
    return _status_cache


def _plan() -> tuple[bool, str, str | None]:
    """How will the next command run? -> (use_bwrap, label for the prompt, error or None)."""
    mode = current_mode()
    if mode == "off":
        return False, "UNSANDBOXED (sandbox off)", None
    ok, detail = bwrap_status()
    if ok:
        return True, f"sandbox: bwrap, network {'on' if network_allowed() else 'off'}", None
    if mode == "on":
        return False, "", (
            "error: the sandbox is required (SLOP_SANDBOX=on) but is not working, so the "
            f"command was not run. {detail}"
        )
    return False, "UNSANDBOXED (bubblewrap not working, see /sandbox)", None


# --- /sandbox command -----------------------------------------------------------------------------

USAGE = (
    "usage:\n"
    "  /sandbox            show the current settings\n"
    "  /sandbox test       run the bubblewrap self-test again\n"
    "  /sandbox on|auto|off   on = require, auto = use if it works, off = never\n"
    "  /sandbox net on|off    allow network inside the sandbox"
)


def status_text() -> str:
    ok, detail = bwrap_status()
    use, label, error = _plan()
    lines = [
        f"sandbox mode: {current_mode()}",
        f"bubblewrap: {'working' if ok else 'NOT working - ' + detail}",
        f"network inside sandbox: {'on' if network_allowed() else 'off'}",
    ]
    extra = _extra_ro_paths()
    if extra:
        lines.append("extra read-only folders: " + ", ".join(extra))
    if error:
        lines.append("commands will be REFUSED until the sandbox works (or: /sandbox auto|off)")
    else:
        lines.append(f"commands will run: {label}")
    return "\n".join(lines)


def handle_slash(line: str):
    """Handle '/sandbox ...' typed by the user. Returns text to print, or None if not ours."""
    global sandbox_mode, sandbox_network
    parts = line.strip().split()
    if not parts or parts[0] != "/sandbox":
        return None
    args = [p.lower() for p in parts[1:]]
    if not args:
        return status_text()
    if args == ["test"]:
        bwrap_status(refresh=True)
        return status_text()
    if len(args) == 1 and args[0] in MODES:
        sandbox_mode = args[0]
        if args[0] != "off":
            bwrap_status(refresh=True)  # e.g. just installed bwrap or added the profile
        return status_text()
    if len(args) == 2 and args[0] in ("net", "network") and args[1] in ("on", "off"):
        sandbox_network = args[1] == "on"
        return status_text()
    return USAGE


# --- approval ---------------------------------------------------------------------------------------

def _style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def _visible(text: str) -> str:
    """Show control characters (escape sequences, bidi overrides, ...) as \\x1b-style text,
    so a command cannot redraw the terminal to disguise what you are approving."""
    out = []
    for c in text:
        if c.isprintable() or c == "\t":
            out.append(c)
        else:
            out.append(f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}")
    return "".join(out)


def _describe(command: str, timeout: int, cwd, warnings: list[str], label: str = "") -> str:
    tag = ""
    if label:
        tag = " " + _style(f"[{label}]", "33" if label.startswith("UNSANDBOXED") else "32")
    lines = [f"Run command{tag} (timeout {timeout}s, in {_visible(str(cwd or '.'))}):"]
    lines += [f"  $ {_visible(line)}" for line in command.splitlines()]
    lines += [_style(f"  ! warning: {w}", "33") for w in warnings]
    return "\n".join(lines)


def _prompt_user(command: str, timeout: int, cwd, warnings: list[str], label: str = "") -> bool:
    print(_describe(command, timeout, cwd, warnings, label))
    try:
        answer = input("Run this command? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


confirm_fn = _prompt_user  # replaceable (tests, or a different UI later)


def _approve(command: str, timeout: int, cwd, warnings: list[str], label: str = "") -> bool:
    if auto_approve:
        print(_describe(command, timeout, cwd, warnings, label))
        return True
    return confirm_fn(command, timeout, cwd, warnings, label)


# --- running ----------------------------------------------------------------------------------------------

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

    use_bwrap, label, error = _plan()
    if error:
        return error
    if not _approve(command, timeout, cwd, warnings_for(command), label):
        return DECLINED

    if use_bwrap:
        argv = bwrap_argv(command, cwd or os.getcwd(), network_allowed())
    else:
        argv = [shutil.which("bash") or "/bin/sh", "-c", command]
    proc = subprocess.Popen(
        argv,
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
