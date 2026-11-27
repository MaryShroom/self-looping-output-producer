"""Tests for the bash tool. These run REAL (harmless) shell commands in a temp folder.

Run:  python tests/test_shell.py     (or: pytest)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shell  # noqa: E402
import tools  # noqa: E402


class Env:
    def __init__(self, approve=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        tools.ROOT = self.root
        shell.auto_approve = False
        self.asked = []
        self.approve = approve

        def confirm(command, timeout, cwd, warnings):
            self.asked.append((command, timeout, warnings))
            return self.approve

        shell.confirm_fn = confirm


def run(command, **kw):
    return tools.run_tool("bash", {"command": command, **kw})


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
    e = Env()
    started = time.monotonic()
    out = run("sleep 47.31 & sleep 47.32", timeout=1)   # a background child too
    elapsed = time.monotonic() - started
    assert "timed out after 1s" in out, out
    assert elapsed < 8, elapsed
    # nothing from this test is still running (dead zombies awaiting reaping don't count)
    time.sleep(0.3)
    # Match this test's own unique durations, so unrelated `sleep` processes on the
    # machine (desktop scripts, timers) can't cause a false failure.
    ps = os.popen("ps -eo stat=,args=").read().splitlines()
    live = [ln for ln in ps if "sleep 47.3" in ln and not ln.lstrip().startswith("Z")]
    assert live == [], live


def test_timeout_is_clamped():
    e = Env()
    run("true", timeout=9999)
    run("true", timeout=-5)
    run("true", timeout="abc")
    assert [a[1] for a in e.asked] == [shell.MAX_TIMEOUT, 1, shell.DEFAULT_TIMEOUT]


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
        out = run("echo auto")
    finally:
        shell.auto_approve = False
    assert "auto" in out and e.asked == []


def test_empty_command_is_an_error():
    Env()
    assert run("   ").startswith("error:")


def test_real_prompt_only_accepts_y():
    import builtins
    import contextlib
    import io
    original = builtins.input
    quiet = contextlib.redirect_stdout(io.StringIO())  # hide the prompts' printed text
    quiet.__enter__()
    try:
        for answer, expected in (("y", True), ("yes", True), ("a", False),
                                 ("n", False), ("", False)):
            builtins.input = lambda _p="", a=answer: a
            assert shell._prompt_user("ls", 30, ".", []) is expected, answer

        def eof(_p=""):
            raise EOFError
        builtins.input = eof
        assert shell._prompt_user("ls", 30, ".", []) is False
    finally:
        builtins.input = original
        quiet.__exit__(None, None, None)


def test_bash_is_registered_with_the_model():
    names = [t["name"] for t in tools.TOOL_SPECS]
    assert "bash" in names
    spec = next(t for t in tools.TOOL_SPECS if t["name"] == "bash")
    assert spec["parameters"]["required"] == ["command"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
