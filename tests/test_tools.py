"""Tests for the file tools (no network, no API keys).

Run:  python tests/test_tools.py     (or: pytest)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402


class Env:
    """Fresh temp project + scripted approval answers for each test."""

    def __init__(self, approve=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        tools.ROOT = self.root
        tools.auto_approve = False
        self.asked = []
        self.approve = approve

        def confirm(summary):
            self.asked.append(summary)
            return self.approve

        tools.confirm_fn = confirm

    def write(self, name, text, newline=""):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline=newline) as f:
            f.write(text)
        return p

    def read(self, name):
        with (self.root / name).open(encoding="utf-8", newline="") as f:
            return f.read()


def test_write_new_file_approved():
    e = Env()
    out = tools.run_tool("write_file", {"path": "pkg/new.py", "content": "print('hi')\n"})
    assert out.startswith("wrote pkg/new.py"), out
    assert e.read("pkg/new.py") == "print('hi')\n"     # parent dir was created
    assert "Create pkg/new.py" in e.asked[0] and "+print('hi')" in e.asked[0]


def test_write_declined_changes_nothing():
    e = Env(approve=False)
    out = tools.run_tool("write_file", {"path": "x.txt", "content": "data"})
    assert out == tools.DECLINED
    assert not (e.root / "x.txt").exists()


def test_overwrite_shows_diff():
    e = Env()
    e.write("a.txt", "one\ntwo\n")
    out = tools.run_tool("write_file", {"path": "a.txt", "content": "one\nTWO\n"})
    assert out.startswith("wrote")
    assert "-two" in e.asked[0] and "+TWO" in e.asked[0]
    assert e.read("a.txt") == "one\nTWO\n"


def test_write_identical_content_asks_nothing():
    e = Env()
    e.write("a.txt", "same")
    out = tools.run_tool("write_file", {"path": "a.txt", "content": "same"})
    assert out.startswith("no change") and e.asked == []


def test_edit_replaces_unique_text():
    e = Env()
    e.write("m.py", "x = 1\ny = 2\n")
    out = tools.run_tool("edit_file", {"path": "m.py", "old_text": "y = 2", "new_text": "y = 3"})
    assert out == "edited m.py"
    assert e.read("m.py") == "x = 1\ny = 3\n"
    assert "-y = 2" in e.asked[0] and "+y = 3" in e.asked[0]


def test_edit_declined_changes_nothing():
    e = Env(approve=False)
    e.write("m.py", "x = 1\n")
    out = tools.run_tool("edit_file", {"path": "m.py", "old_text": "1", "new_text": "2"})
    assert out == tools.DECLINED and e.read("m.py") == "x = 1\n"


def test_edit_not_found():
    e = Env()
    e.write("m.py", "x = 1\n")
    out = tools.run_tool("edit_file", {"path": "m.py", "old_text": "zzz", "new_text": "q"})
    assert out.startswith("error: old_text not found") and e.asked == []


def test_edit_ambiguous_match_is_rejected():
    e = Env()
    e.write("m.py", "a = 1\na = 1\n")
    out = tools.run_tool("edit_file", {"path": "m.py", "old_text": "a = 1", "new_text": "a = 2"})
    assert "appears 2 times" in out
    assert e.read("m.py") == "a = 1\na = 1\n" and e.asked == []


def test_edit_input_validation():
    e = Env()
    e.write("m.py", "x = 1\n")
    assert "must not be empty" in tools.run_tool(
        "edit_file", {"path": "m.py", "old_text": "", "new_text": "q"})
    assert "identical" in tools.run_tool(
        "edit_file", {"path": "m.py", "old_text": "x", "new_text": "x"})
    assert "file not found" in tools.run_tool(
        "edit_file", {"path": "nope.py", "old_text": "a", "new_text": "b"})


def test_edit_preserves_crlf():
    e = Env()
    e.write("w.txt", "one\r\ntwo\r\nthree\r\n")
    # the model sends \n line endings; the file uses \r\n
    out = tools.run_tool(
        "edit_file", {"path": "w.txt", "old_text": "one\ntwo", "new_text": "one\nTWO"})
    assert out == "edited w.txt"
    assert e.read("w.txt") == "one\r\nTWO\r\nthree\r\n"


def test_non_utf8_file_is_refused():
    e = Env()
    (e.root / "b.bin").write_bytes(b"\xff\xfe\x00bad")
    out = tools.run_tool("edit_file", {"path": "b.bin", "old_text": "a", "new_text": "b"})
    assert "not a UTF-8 text file" in out


def test_paths_outside_project_are_blocked():
    e = Env()
    for name in ("write_file", "edit_file"):
        args = {"path": "../evil.txt", "content": "x"} if name == "write_file" else {
            "path": "../evil.txt", "old_text": "a", "new_text": "b"}
        assert "outside the working directory" in tools.run_tool(name, args)
    assert e.asked == []


def test_protected_paths_are_blocked():
    e = Env()
    e.write(".env", "KEY=1\n")
    e.write(".git/config", "[core]\n")
    for path in (".env", ".env.local", ".git/config", ".git/hooks/pre-commit"):
        out = tools.run_tool("write_file", {"path": path, "content": "x"})
        assert "protected path" in out, (path, out)
    out = tools.run_tool("edit_file", {"path": ".env", "old_text": "1", "new_text": "2"})
    assert "protected path" in out
    assert e.read(".env") == "KEY=1\n" and e.asked == []


def test_env_example_is_writable():
    e = Env()
    out = tools.run_tool("write_file", {"path": ".env.example", "content": "KEY=\n"})
    assert out.startswith("wrote")


def test_writing_to_a_directory_fails():
    e = Env()
    (e.root / "d").mkdir()
    assert "directory" in tools.run_tool("write_file", {"path": "d", "content": "x"})


def test_auto_approve_skips_prompt_but_applies_change():
    e = Env(approve=False)          # would decline if it were asked
    tools.auto_approve = True
    out = tools.run_tool("write_file", {"path": "ok.txt", "content": "yes"})
    assert out.startswith("wrote") and e.asked == []
    assert e.read("ok.txt") == "yes"


def test_prompt_answers(monkeypatch=None):
    """The real prompt: y approves once, a approves and sets auto_approve, anything else declines."""
    import builtins
    original = builtins.input
    try:
        for answer, expected, auto in (("y", True, False), ("a", True, True),
                                       ("n", False, False), ("", False, False)):
            tools.auto_approve = False
            builtins.input = lambda _prompt="", a=answer: a
            assert tools._prompt_user("summary") is expected, answer
            assert tools.auto_approve is auto, answer
        def eof(_prompt=""):
            raise EOFError
        builtins.input = eof
        tools.auto_approve = False
        assert tools._prompt_user("summary") is False
    finally:
        builtins.input = original
        tools.auto_approve = False


def test_long_previews_are_clipped():
    e = Env()
    content = "\n".join(f"line {i}" for i in range(200))
    tools.run_tool("write_file", {"path": "big.txt", "content": content})
    assert "more lines)" in e.asked[0]
    assert len(e.asked[0].splitlines()) < 70


def test_read_and_list_still_work():
    e = Env()
    e.write("a.txt", "hello")
    assert tools.run_tool("read_file", {"path": "a.txt"}) == "hello"
    assert "a.txt" in tools.run_tool("list_files", {})
    e.write(".env", "SECRET=1")
    assert "refusing to read" in tools.run_tool("read_file", {"path": ".env"})


def test_unknown_argument_error_lists_valid_ones():
    Env()
    out = tools.run_tool("read_file", {"path": "a.txt", "toolbench_rust_version": "1.04.0"})
    assert out.startswith("error: read_file:") and "unknown argument(s) ['toolbench_rust_version']" in out
    assert "Valid arguments: ['limit', 'offset', 'path']" in out


def test_missing_required_argument_error():
    Env()
    out = tools.run_tool("edit_file", {"path": "a.txt"})
    assert "missing required argument(s) ['old_text', 'new_text']" in out
    assert tools.run_tool("read_file", None).startswith("error: read_file: missing required")
    assert "must be an object" in tools.run_tool("read_file", "a.txt")


def test_unknown_tool_is_still_reported():
    assert tools.run_tool("nope", {}) == "error: unknown tool nope"


def test_preview_clips_lines_and_width_and_hides_control_characters():
    os_env = tools.os.environ
    os_env.pop("SLOP_SHOW_OUTPUT", None)
    text = "\n".join(f"line {i}" for i in range(20))
    out = tools.format_preview(text)
    assert out.count("\n") == 8 and "... (12 more lines)" in out      # 8 lines + the "more" line
    assert tools.format_preview("x" * 500).endswith("…") and len(tools.format_preview("x" * 500)) < 140
    assert tools.format_preview("") == "    | (no output)"
    evil = tools.format_preview("ok\x1b[2J\x1b]0;pwned\x07\rhidden")
    assert "\x1b" not in evil and "\x07" not in evil and "\r" not in evil
    assert "\\x1b" in evil


def test_preview_can_be_switched_off():
    tools.os.environ["SLOP_SHOW_OUTPUT"] = "off"
    try:
        assert tools.format_preview("hello") == ""
    finally:
        tools.os.environ.pop("SLOP_SHOW_OUTPUT", None)


def test_failure_streak_counts_only_consecutive_tool_errors():
    s = tools.FailureStreak(limit=4)
    assert [s.record("error: nope") for _ in range(3)] == [False, False, False]
    assert s.record("wrote a.txt") is False          # success resets the count
    assert [s.record("error: nope") for _ in range(3)] == [False, False, False]
    assert s.record("error: again") is True          # 4th in a row
    s2 = tools.FailureStreak(limit=2)
    s2.record("error: x")
    assert s2.record("exit code: 1\ntests failed") is False    # a command failing is not a tool failure
    s2.record("error: x")
    assert s2.record(tools.DECLINED) is False and s2.count == 0   # declining is not a failure


def test_approval_previews_escape_control_characters():
    e = Env()
    tools.run_tool("write_file", {"path": "evil\x1b[31m.txt", "content": "a\x1b[2K\rb\n"})
    summary = e.asked[0]
    assert "\x1b" not in summary and "\r" not in summary
    assert "\\x1b" in summary


def test_visible_text_keeps_layout_but_escapes_control_characters():
    assert tools.visible_text("a\nb\tc") == "a\nb\tc"
    out = tools.visible_text("x\x1b[2Jy\rz\x07\n‮!")
    assert out == "x\\x1b[2Jy\\x0dz\\x07\n\\u202e!"
    assert tools.visible_text("héllo 你好 🙂") == "héllo 你好 🙂"      # normal text untouched


def test_wrong_argument_types_get_a_clear_error():
    # what qwen2.5:0.5b actually sent: the whole argument object nested inside `command`
    out = tools.run_tool("bash", {"command": {"command": "cat evil.txt", "timeout": 30}})
    assert out.startswith("error: bash: argument 'command' must be of type string but got dict.")
    assert "not nested inside another object" in out
    assert "must be of type string but got list" in tools.run_tool("read_file", {"path": ["a", "b"]})
    assert "must be of type string but got int" in tools.run_tool("read_file", {"path": 5})
    assert "must be of type integer but got str" in tools.run_tool("bash", {"command": "x", "timeout": "abc"})
    assert "must be of type integer but got bool" in tools.run_tool("bash", {"command": "x", "timeout": True})
    assert "must be of type integer but got float" in tools.run_tool("bash", {"command": "x", "timeout": 2.5})


def test_numbers_are_accepted_the_way_each_provider_sends_them():
    for timeout in (30, 30.0, "30"):       # int, Gemini's float, a model's string
        assert tools._check_args("bash", {"command": "ls", "timeout": timeout}) is None, timeout


def numbered(n):
    return "".join(f"line {i}\n" for i in range(1, n + 1))


def test_read_file_returns_small_files_unchanged():
    e = Env()
    e.write("s.txt", "a\nb")
    assert tools.run_tool("read_file", {"path": "s.txt"}) == "a\nb"        # no footer, no changes
    e.write("crlf.txt", "a\r\nb\r\n", newline="")
    assert tools.run_tool("read_file", {"path": "crlf.txt"}) == "a\r\nb\r\n"
    e.write("empty.txt", "")
    assert tools.run_tool("read_file", {"path": "empty.txt"}) == "(empty file)"


def test_read_file_pages_through_a_long_file():
    e = Env()
    e.write("long.txt", numbered(1200))
    first = tools.run_tool("read_file", {"path": "long.txt"})
    assert first.startswith("line 1\n") and "line 500\n" in first and "line 501" not in first
    assert first.endswith("[lines 1-500 shown, more follow: call read_file again with offset=501]")
    second = tools.run_tool("read_file", {"path": "long.txt", "offset": 501})
    assert second.startswith("line 501\n") and "offset=1001" in second
    last = tools.run_tool("read_file", {"path": "long.txt", "offset": 1001})
    assert last.startswith("line 1001\n") and last.endswith("[lines 1001-1200, end of file]")


def test_read_file_limit_and_clamping():
    e = Env()
    e.write("t.txt", numbered(50))
    out = tools.run_tool("read_file", {"path": "t.txt", "offset": 10, "limit": 3})
    assert out == "line 10\nline 11\nline 12\n[lines 10-12 shown, more follow: call read_file again with offset=13]"
    assert tools.run_tool("read_file", {"path": "t.txt", "limit": "2"}).count("\nline") == 1   # "2" as a string
    e.write("big.txt", numbered(5000))
    assert "lines 1-2000 shown" in tools.run_tool("read_file", {"path": "big.txt", "limit": 99999})
    assert "offset must be 1 or more" in tools.run_tool("read_file", {"path": "t.txt", "offset": 0})
    assert "past the end of the file (50 lines)" in tools.run_tool("read_file", {"path": "t.txt", "offset": 51})
    assert "must be of type integer" in tools.run_tool("read_file", {"path": "t.txt", "limit": "lots"})


def test_read_file_output_is_capped_in_characters():
    e = Env()
    e.write("wide.txt", ("x" * 1500 + "\n") * 100)           # 150 KB in 100 lines
    out = tools.run_tool("read_file", {"path": "wide.txt", "limit": 2000})
    assert len(out) <= tools.MAX_CHARS + 200
    assert "more follow: call read_file again with offset=" in out


def test_read_file_clips_one_enormous_line_without_loading_it():
    e = Env()
    e.write("min.js", "start\n" + "y" * 5_000_000 + "\nend\n")  # a 5 MB line
    out = tools.run_tool("read_file", {"path": "min.js"})
    assert out.startswith("start\n") and "…[line clipped]" in out and out.rstrip().endswith("end")
    assert len(out) < 5000


def test_read_file_refuses_binary_folders_pipes_and_secrets():
    import os
    e = Env()
    (e.root / "b.bin").write_bytes(b"abc\0def")
    assert "binary file" in tools.run_tool("read_file", {"path": "b.bin"})
    (e.root / "d").mkdir()
    assert "use list_files" in tools.run_tool("read_file", {"path": "d"})
    assert "file not found" in tools.run_tool("read_file", {"path": "nope.txt"})
    os.mkfifo(e.root / "pipe")                                  # would block forever if opened for reading
    assert "not a regular file" in tools.run_tool("read_file", {"path": "pipe"})
    e.write(".env", "KEY=1\n")
    assert "refusing to read" in tools.run_tool("read_file", {"path": ".env", "offset": 1})


def test_read_file_tolerates_bad_utf8():
    e = Env()
    (e.root / "latin.txt").write_bytes(b"caf\xe9\n")
    assert tools.run_tool("read_file", {"path": "latin.txt"}) == "caf�\n"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
