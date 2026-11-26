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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
