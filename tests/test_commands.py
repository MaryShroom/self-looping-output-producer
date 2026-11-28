"""Tests for the /auto, /help and /sandbox slash commands and for how auto-approve behaves.

Run:  python tests/test_commands.py     (or: pytest)
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands  # noqa: E402
import shell  # noqa: E402
import tools  # noqa: E402


class Env:
    def __init__(self, answers=(), sandbox_ok=True):
        self.tmp = tempfile.TemporaryDirectory()
        tools.ROOT = Path(self.tmp.name).resolve()
        tools.auto_approve = False
        shell.auto_approve = False
        self.asked = []
        self.answers = list(answers)
        self.questions = []
        commands.ask = self._ask
        label = "sandbox: bwrap, network off" if sandbox_ok else "UNSANDBOXED (sandbox off)"
        shell._plan = lambda: (False, label, None)       # plan only; the command itself runs plainly
        tools.confirm_fn = lambda summary: self.asked.append(summary) or False
        shell.confirm_fn = lambda c, t, cwd, w, label="": self.asked.append((c, w, label)) or False

    def _ask(self, prompt=""):
        self.questions.append(prompt)
        return self.answers.pop(0) if self.answers else ""


_ORIGINALS = (shell._plan, commands.ask, tools.confirm_fn, shell.confirm_fn)


def teardown_function(_fn=None):
    """Put everything back (matters when pytest runs all test files in one process)."""
    shell._plan, commands.ask, tools.confirm_fn, shell.confirm_fn = _ORIGINALS
    tools.auto_approve = False
    shell.auto_approve = False


def cmd(line):
    with contextlib.redirect_stdout(io.StringIO()):
        return commands.handle_slash(line)


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def test_defaults_are_off_and_status_says_so():
    Env()
    out = cmd("/auto")
    assert "files: off" in out and "shell: off" in out


def test_files_switch():
    Env()
    assert "files: ON" in cmd("/auto files on") and tools.auto_approve is True
    assert shell.auto_approve is False                     # shell is separate
    assert "files: off" in cmd("/auto files off") and tools.auto_approve is False
    assert cmd("  /AUTO   Files   ON ") and tools.auto_approve is True     # case and spaces


def test_shell_switch_needs_a_typed_yes():
    e = Env(answers=["yes"])
    out = cmd("/auto shell on")
    assert shell.auto_approve is True and "shell: ON" in out and "sandboxed commands run" in out
    assert "yes" in e.questions[0]
    e = Env(answers=["y"])                                  # "y" is not enough
    assert "not enabled" in cmd("/auto shell on") and shell.auto_approve is False
    e = Env(answers=[""])
    cmd("/auto shell on")
    assert shell.auto_approve is False
    Env(answers=["yes"])
    cmd("/auto shell on")
    assert "shell: off" in cmd("/auto shell off") and shell.auto_approve is False


def test_shell_switch_warns_when_the_sandbox_is_not_in_use():
    Env(answers=["yes"], sandbox_ok=False)
    out = cmd("/auto shell on")
    assert "every command still asks" in out


def test_auto_off_turns_both_off():
    Env(answers=["yes"])
    cmd("/auto files on")
    cmd("/auto shell on")
    out = cmd("/auto off")
    assert tools.auto_approve is False and shell.auto_approve is False
    assert "files: off" in out and "shell: off" in out


def test_bad_usage_changes_nothing():
    Env()
    for bad in ("/auto on", "/auto files", "/auto files maybe", "/auto shell", "/auto all on"):
        assert cmd(bad).startswith("usage:"), bad
    assert tools.auto_approve is False and shell.auto_approve is False


def test_routing_and_unknown_commands():
    Env()
    assert "sandbox mode" in cmd("/sandbox")                # still handled by shell.py
    assert "/auto shell on|off" in cmd("/help")
    assert commands.handle_slash("/nope") is None
    assert commands.handle_slash("hello") is None
    assert commands.handle_slash("") is None


def test_shell_auto_approve_runs_safe_sandboxed_commands_without_asking():
    e = Env()
    shell.auto_approve = True
    out = quiet(tools.run_tool, "bash", {"command": "echo hello_auto"})
    assert "hello_auto" in out and e.asked == []


def test_shell_auto_approve_still_asks_for_risky_commands():
    e = Env()
    shell.auto_approve = True
    out = quiet(tools.run_tool, "bash", {"command": "rm -rf nothing_here"})
    assert out == shell.DECLINED and len(e.asked) == 1          # our fake prompt said no
    assert e.asked[0][1]                                         # and it carried the warnings


def test_shell_auto_approve_still_asks_when_unsandboxed():
    e = Env(sandbox_ok=False)
    shell.auto_approve = True
    out = quiet(tools.run_tool, "bash", {"command": "echo hi"})
    assert out == shell.DECLINED and e.asked[0][2].startswith("UNSANDBOXED")


def test_answering_a_at_the_file_prompt_does_not_enable_shell_auto_approve():
    import builtins
    Env()
    original = builtins.input
    try:
        builtins.input = lambda _p="": "a"
        quiet(tools._prompt_user, "summary")
    finally:
        builtins.input = original
    assert tools.auto_approve is True and shell.auto_approve is False


def test_the_model_cannot_reach_slash_commands():
    Env()
    # a tool result or a model reply is never passed to handle_slash; and bash cannot flip the flag
    quiet(tools.run_tool, "bash", {"command": "echo /auto shell on"})
    assert shell.auto_approve is False
    assert "auto_approve" not in {p for t in tools.TOOL_SPECS for p in t["parameters"]["properties"]}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
