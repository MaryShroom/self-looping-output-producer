"""Tests for the bash tool and its bubblewrap sandbox.

Run:  python tests/test_shell.py     (or: pytest)

Four kinds of test:
  1. real harmless shell commands with the sandbox OFF (the original tests)
  2. pure logic: modes, settings, the generated bwrap command line, /sandbox
  3. plumbing with a FAKE bwrap script (checks how SLOP calls bwrap; no isolation)
  4. integration with the REAL bwrap; skipped when bwrap is missing or not working
"""
import contextlib
import io
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shell  # noqa: E402
import tools  # noqa: E402

_ORIGINAL_PATH = os.environ.get("PATH", "")


class Env:
    """Fresh temp project, sandbox OFF, scripted approval answers."""

    def __init__(self, approve=True, mode="off"):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        tools.ROOT = self.root
        shell.auto_approve = False
        shell.sandbox_mode = mode
        shell.sandbox_network = None
        shell._status_cache = None
        os.environ["PATH"] = _ORIGINAL_PATH
        for var in ("SLOP_SANDBOX", "SLOP_SANDBOX_NETWORK", "SLOP_SANDBOX_RO", "FAKE_BWRAP_LOG"):
            os.environ.pop(var, None)
        self.asked = []
        self.approve = approve

        def confirm(command, timeout, cwd, warnings, label=""):
            self.asked.append((command, timeout, warnings, label))
            return self.approve

        shell.confirm_fn = confirm

    def make(self, rel, text=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


def run(command, **kw):
    return tools.run_tool("bash", {"command": command, **kw})


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


# ===== 1. original behaviour, sandbox off ==========================================================

def test_runs_in_project_folder_and_reports_exit_code():
    e = Env()
    out = run("pwd")
    assert out.startswith("exit code: 0"), out
    assert str(e.root) in out


def test_nonzero_exit_and_stderr_are_reported():
    Env()
    out = run("echo oops >&2; exit 3")
    assert out.startswith("exit code: 3") and "oops" in out


def test_declined_command_does_not_run():
    e = Env(approve=False)
    out = run("touch should_not_exist")
    assert out == shell.DECLINED
    assert not (e.root / "should_not_exist").exists()


def test_approved_command_runs_and_asks_once():
    e = Env()
    run("touch made_it")
    assert (e.root / "made_it").exists()
    assert len(e.asked) == 1 and e.asked[0][0] == "touch made_it"


def test_timeout_kills_command_and_its_children():
    Env()
    started = time.monotonic()
    out = run("sleep 47.31 & sleep 47.32", timeout=1)   # a background child too
    elapsed = time.monotonic() - started
    assert "timed out after 1s" in out, out
    assert elapsed < 8, elapsed
    time.sleep(0.3)
    # Match this test's own unique durations, so unrelated `sleep` processes on the
    # machine can't cause a false failure (dead zombies awaiting reaping don't count).
    ps = os.popen("ps -eo stat=,args=").read().splitlines()
    live = [ln for ln in ps if "sleep 47.3" in ln and not ln.lstrip().startswith("Z")]
    assert live == [], live


def test_timeout_is_clamped():
    e = Env()
    run("true", timeout=9999)
    run("true", timeout=-5)
    assert [a[1] for a in e.asked] == [shell.MAX_TIMEOUT, 1]
    # through the tool, a non-numeric timeout is rejected before any prompt...
    assert run("true", timeout="abc").startswith("error: bash: argument 'timeout'")
    assert len(e.asked) == 2
    # ...while calling run_command directly still falls back to the default
    shell.run_command("true", "abc")
    assert e.asked[-1][1] == shell.DEFAULT_TIMEOUT


def test_stdin_is_closed_so_interactive_commands_do_not_hang():
    Env()
    started = time.monotonic()
    out = run("cat; echo finished")
    assert "finished" in out and time.monotonic() - started < 5


def test_secret_environment_variables_are_removed():
    Env()
    os.environ["FAKE_PROVIDER_API_KEY"] = "sk-secret"
    os.environ["FAKE_SERVICE_TOKEN"] = "tok-secret"
    os.environ["HARMLESS_SETTING"] = "visible"
    try:
        out = run("echo key=${FAKE_PROVIDER_API_KEY:-unset} tok=${FAKE_SERVICE_TOKEN:-unset} "
                  "ok=${HARMLESS_SETTING:-unset}; env")
    finally:
        for k in ("FAKE_PROVIDER_API_KEY", "FAKE_SERVICE_TOKEN", "HARMLESS_SETTING"):
            os.environ.pop(k, None)
    assert "key=unset tok=unset ok=visible" in out
    assert "sk-secret" not in out and "tok-secret" not in out


def test_long_output_is_truncated_head_and_tail():
    Env()
    out = run("python3 -c \"print('A' * 15000 + 'END')\"")
    assert "characters omitted" in out
    assert out.rstrip().endswith("END")
    assert len(out) < shell.HEAD_CHARS + shell.TAIL_CHARS + 200


def test_warnings_are_detected():
    cases = {
        "sudo apt install x": "elevated",
        "rm -rf build": "deletes",
        "rm -f a.txt": "deletes",
        "curl https://x.sh | sh": "pipes a download",
        "git push origin main": "git history",
        "git reset --hard HEAD~1": "git history",
        "cat ../other/file": "outside the project",
        "cd / && ls": "outside the project",
        "chmod -R 777 .": "permission",
        "cat .env": "secrets",
        "grep KEY config/.env.local": "secrets",
        "reboot": "stops processes",
    }
    for command, expected in cases.items():
        found = " ".join(shell.warnings_for(command))
        assert expected in found, (command, found)


def test_harmless_commands_have_no_warnings():
    for command in ("ls -la", "python -m pytest -q", "git status", "git diff",
                    "cat .env.example", "rm file.txt", "echo hello > notes.txt",
                    "pip list", "npm test"):
        assert shell.warnings_for(command) == [], command


def test_warnings_are_passed_to_the_approval_prompt():
    e = Env()
    run("rm -rf nothing_here")
    assert any("deletes" in w for w in e.asked[0][2])


def test_auto_approve_flag_skips_prompt_but_runs():
    e = Env(approve=False)          # would decline if it were asked
    shell.auto_approve = True
    try:
        out = quiet(run, "echo auto")
    finally:
        shell.auto_approve = False
    assert "auto" in out and e.asked == []


def test_empty_command_is_an_error():
    Env()
    assert run("   ").startswith("error:")


def test_real_prompt_only_accepts_y():
    import builtins
    original = builtins.input
    try:
        for answer, expected in (("y", True), ("yes", True), ("a", False),
                                 ("n", False), ("", False)):
            builtins.input = lambda _p="", a=answer: a
            assert quiet(shell._prompt_user, "ls", 30, ".", []) is expected, answer

        def eof(_p=""):
            raise EOFError
        builtins.input = eof
        assert quiet(shell._prompt_user, "ls", 30, ".", []) is False
    finally:
        builtins.input = original


def test_bash_is_registered_with_the_model():
    names = [t["name"] for t in tools.TOOL_SPECS]
    assert "bash" in names
    spec = next(t for t in tools.TOOL_SPECS if t["name"] == "bash")
    assert spec["parameters"]["required"] == ["command"]


# ===== 2. settings, bwrap command line, /sandbox ==================================================

def test_mode_parsing_and_defaults():
    Env(mode=None)
    assert shell.current_mode() == "auto"                    # default
    for value, expected in (("off", "off"), ("OFF", "off"), ("auto", "auto"), ("on", "on"),
                            ("bwrap", "on"), ("false", "off"), ("no", "off"),
                            ("of", "on"), ("garbage", "on")):   # unknown -> fail closed
        os.environ["SLOP_SANDBOX"] = value
        assert shell.current_mode() == expected, value
    shell.sandbox_mode = "off"                               # runtime setting wins
    assert shell.current_mode() == "off"


def test_network_setting():
    Env()
    assert shell.network_allowed() is False                  # default off
    os.environ["SLOP_SANDBOX_NETWORK"] = "on"
    assert shell.network_allowed() is True
    shell.sandbox_network = False                            # runtime setting wins
    assert shell.network_allowed() is False


def _triple(argv, flag, a, b):
    return any(argv[i:i + 3] == [flag, a, b] for i in range(len(argv) - 2))


def test_bwrap_argv_shape_and_protections():
    e = Env()
    e.make(".env", "SECRET=1")
    e.make("sub/.env.local", "SECRET=2")
    e.make(".env.example", "KEY=")
    e.make(".venv/.env", "pruned")
    e.make("a/b/c/d/.env", "too deep")
    e.make(".git/config", "[core]")
    (e.root / ".git" / "hooks").mkdir()
    root = str(e.root)
    argv = shell.bwrap_argv("echo hi", e.root, network=False)

    assert argv[0] == "bwrap"
    for flag in ("--die-with-parent", "--new-session", "--unshare-all"):
        assert flag in argv, flag
    assert "--share-net" not in argv
    assert argv[-4:] == ["--", "bash", "-c", "echo hi"]
    assert _triple(argv, "--ro-bind", "/usr", "/usr")
    assert _triple(argv, "--bind", root, root)                         # project is writable
    assert argv[argv.index("--chdir") + 1] == root
    # .env files are covered by an unreadable placeholder; .env.example, pruned and too-deep ones are not
    assert _triple(argv, "--ro-bind", "/dev/null", f"{root}/.env")
    assert _triple(argv, "--ro-bind", "/dev/null", f"{root}/sub/.env.local")
    assert f"{root}/.env.example" not in argv
    assert f"{root}/.venv/.env" not in argv
    assert f"{root}/a/b/c/d/.env" not in argv
    # git hooks/config are read-only, and the mounts come AFTER the writable project mount
    for p in (f"{root}/.git/hooks", f"{root}/.git/config"):
        assert _triple(argv, "--ro-bind-try", p, p), p
        assert argv.index(p) > argv.index("--bind")
    # the home folder is never mounted
    home = os.path.expanduser("~")
    assert home not in argv and "/home" not in argv
    assert shell.bwrap_argv("x", e.root, network=True).count("--share-net") == 1


def test_extra_read_only_paths_from_env():
    e = Env()
    os.environ["SLOP_SANDBOX_RO"] = "~/.cache/ms-playwright: /opt/tools ::"
    argv = shell.bwrap_argv("true", e.root, network=False)
    pw = os.path.expanduser("~/.cache/ms-playwright")
    assert _triple(argv, "--ro-bind-try", pw, pw)
    assert _triple(argv, "--ro-bind-try", "/opt/tools", "/opt/tools")


def test_slash_command():
    Env(mode=None)
    shell._status_cache = (True, "ok")
    assert shell.handle_slash("/other") is None
    assert shell.handle_slash("hello") is None
    text = shell.handle_slash("/sandbox")
    assert "sandbox mode: auto" in text and "sandbox: bwrap" in text
    assert "off" in shell.handle_slash("/sandbox off") and shell.current_mode() == "off"
    assert "UNSANDBOXED" in shell.handle_slash("/sandbox")
    shell._status_cache = (True, "ok")
    shell.handle_slash("/sandbox ON")
    assert shell.current_mode() == "on"
    assert "network inside sandbox: on" in shell.handle_slash("/sandbox net on")
    assert shell.network_allowed() is True
    assert shell.handle_slash("/sandbox network off") and shell.network_allowed() is False
    assert shell.handle_slash("/sandbox bogus").startswith("usage:")
    assert shell.handle_slash("/sandbox net maybe").startswith("usage:")


def test_userns_hint_in_failure_detail():
    Env()
    fake_dir = _fake_bwrap("broken")
    os.environ["PATH"] = fake_dir + ":" + _ORIGINAL_PATH
    original = shell._userns_restricted
    try:
        shell._userns_restricted = lambda: True
        ok, detail = shell.bwrap_status(refresh=True)
    finally:
        shell._userns_restricted = original
    assert ok is False
    assert "RTM_NEWADDR" in detail and "AppArmor" in detail and "SANDBOX.md" in detail


# ===== 3. plumbing with a fake bwrap =================================================================

FAKE_OK = """#!/bin/sh
# Fake bubblewrap for plumbing tests. Records its arguments, skips all options up to
# "--", then simply runs the command. It provides NO isolation.
[ -n "$FAKE_BWRAP_LOG" ] && printf '%s\\n' "$@" >> "$FAKE_BWRAP_LOG"
chdir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --chdir) chdir="$2"; shift 2 ;;
    --) shift; break ;;
    *) shift ;;
  esac
done
[ -n "$chdir" ] && cd "$chdir"
exec "$@"
"""

FAKE_BROKEN = """#!/bin/sh
echo 'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted' >&2
exit 1
"""


def _fake_bwrap(kind="ok"):
    d = tempfile.mkdtemp()
    p = Path(d) / "bwrap"
    p.write_text(FAKE_OK if kind == "ok" else FAKE_BROKEN)
    p.chmod(0o755)
    return d


def _use(fake_dir):
    os.environ["PATH"] = fake_dir + ":" + _ORIGINAL_PATH


def _no_bwrap_on_path():
    os.environ["PATH"] = tempfile.mkdtemp()   # empty dir: bwrap (and bash) not found


def test_sandbox_on_runs_through_bwrap_with_label():
    e = Env(mode="on")
    _use(_fake_bwrap("ok"))
    log = e.root.parent / (e.root.name + ".log")
    os.environ["FAKE_BWRAP_LOG"] = str(log)
    out = run("pwd")
    assert out.startswith("exit code: 0") and str(e.root) in out, out
    assert e.asked[0][3] == "sandbox: bwrap, network off", e.asked
    args = log.read_text().splitlines()
    assert "--unshare-all" in args and args[-3:] == ["bash", "-c", "pwd"]
    assert "--share-net" not in args


def test_network_on_changes_label_and_flags():
    e = Env(mode="on")
    _use(_fake_bwrap("ok"))
    log = e.root.parent / (e.root.name + ".log")
    os.environ["FAKE_BWRAP_LOG"] = str(log)
    shell.sandbox_network = True
    run("true")
    assert e.asked[0][3] == "sandbox: bwrap, network on"
    assert "--share-net" in log.read_text().splitlines()


def test_mode_off_never_uses_bwrap():
    e = Env(mode="off")
    _use(_fake_bwrap("ok"))
    log = e.root.parent / (e.root.name + ".log")
    os.environ["FAKE_BWRAP_LOG"] = str(log)
    out = run("echo plain")
    assert "plain" in out and e.asked[0][3].startswith("UNSANDBOXED")
    assert not log.exists()


def test_mode_on_without_bwrap_refuses_and_does_not_ask():
    e = Env(mode="on")
    _no_bwrap_on_path()
    out = run("touch nope")
    assert out.startswith("error: the sandbox is required"), out
    assert "not installed" in out and e.asked == []
    assert not (e.root / "nope").exists()


def test_mode_on_with_broken_bwrap_refuses():
    e = Env(mode="on")
    _use(_fake_bwrap("broken"))
    out = run("touch nope")
    assert out.startswith("error: the sandbox is required") and "RTM_NEWADDR" in out, out
    assert e.asked == [] and not (e.root / "nope").exists()


def test_mode_auto_falls_back_to_unsandboxed_with_loud_label():
    e = Env(mode="auto")
    _use(_fake_bwrap("broken"))
    out = run("echo still works")
    assert "still works" in out
    assert e.asked[0][3].startswith("UNSANDBOXED"), e.asked


def test_mode_auto_uses_bwrap_when_it_works():
    e = Env(mode="auto")
    _use(_fake_bwrap("ok"))
    run("true")
    assert e.asked[0][3].startswith("sandbox: bwrap")


def test_declined_command_is_not_run_even_in_sandbox_mode():
    e = Env(mode="on", approve=False)
    _use(_fake_bwrap("ok"))
    log = e.root.parent / (e.root.name + ".log")
    os.environ["FAKE_BWRAP_LOG"] = str(log)
    assert run("touch x") == shell.DECLINED
    assert log.read_text().count("bash") == 1      # only the self-test ran bwrap, not the command


def test_timeout_still_kills_everything_through_bwrap_plumbing():
    Env(mode="on")
    _use(_fake_bwrap("ok"))
    started = time.monotonic()
    out = run("sleep 47.33 & sleep 47.34", timeout=1)
    assert "timed out after 1s" in out and time.monotonic() - started < 8
    time.sleep(0.3)
    ps = os.popen("ps -eo stat=,args=").read().splitlines()
    live = [ln for ln in ps if "sleep 47.3" in ln and not ln.lstrip().startswith("Z")]
    assert live == [], live


# ===== 4. integration with the real bwrap (skipped when unavailable) ==================================

def _real_bwrap_ok():
    os.environ["PATH"] = _ORIGINAL_PATH
    shell._status_cache = None
    return shell.bwrap_status(refresh=True)


def _skip(name, detail):
    print(f"  (skipped {name}: {detail.splitlines()[0][:100]})")


def test_real_bwrap_hides_home_and_secrets_and_blocks_writes():
    e = Env(mode="on")
    ok, detail = _real_bwrap_ok()
    if not ok:
        return _skip("real bwrap", detail)
    e.make(".env", "TOP_SECRET_VALUE")
    e.make("ok.txt", "visible")
    e.make(".git/config", "[core]")
    out = run("echo works; cat ok.txt; cat .env; ls /home 2>&1; ls ~ 2>&1 | head -1")
    assert "works" in out and "visible" in out, out
    assert "TOP_SECRET_VALUE" not in out, out                 # .env is unreadable in there (EACCES)
    assert os.path.expanduser("~") not in out, out            # the real home path never shows up
    assert "exit code: 0" in run("touch created_inside")      # project is writable
    assert (e.root / "created_inside").exists()
    assert "exit code: 0" not in run("touch /usr/should_fail")  # system is read-only
    run("echo x >> .git/config")                              # .git/config is read-only
    assert (e.root / ".git" / "config").read_text() == "[core]"
    assert "exit code: 0" not in run("echo x >> .env")        # cannot write secrets either


def test_real_bwrap_network_off_and_on():
    e = Env(mode="on")
    ok, detail = _real_bwrap_ok()
    if not ok:
        return _skip("real bwrap network", detail)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    probe = f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2)\""
    try:
        shell.sandbox_network = False
        assert "exit code: 0" not in run(probe)               # private network: cannot reach it
        shell.sandbox_network = True
        assert run(probe).startswith("exit code: 0")          # shared network: can
    finally:
        server.close()


def test_real_bwrap_timeout_kills_everything():
    Env(mode="on")
    ok, detail = _real_bwrap_ok()
    if not ok:
        return _skip("real bwrap timeout", detail)
    started = time.monotonic()
    out = run("sleep 47.35 & sleep 47.36", timeout=1)
    assert "timed out after 1s" in out and time.monotonic() - started < 8, out
    time.sleep(0.5)
    ps = os.popen("ps -eo stat=,args=").read().splitlines()
    live = [ln for ln in ps if "sleep 47.3" in ln and not ln.lstrip().startswith("Z")]
    assert live == [], live


def test_control_characters_are_made_visible_in_the_command_prompt():
    text = shell._describe("echo hi\x1b[2K\rrm -rf ~‮", 30, ".", [], "")
    assert "\x1b" not in text and "‮" not in text and "\r" not in text
    assert "\\x1b" in text and "\\u202e" in text
    assert "rm -rf ~" in text          # the real command is still shown, not hidden


def test_run_command_rejects_non_string_commands_directly():
    Env()
    for bad in ({"command": "ls"}, ["ls"], 5):
        try:
            shell.run_command(bad)
        except ValueError as e:
            assert "must be a string" in str(e)
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
