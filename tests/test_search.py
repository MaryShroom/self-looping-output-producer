"""Tests for the grep and glob tools (no network, no API keys).

Run:  python tests/test_search.py     (or: pytest)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import search  # noqa: E402
import tools  # noqa: E402


class Project:
    """A temp project with a mix of normal, hidden, secret, huge, binary and linked files."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        self.outside = Path(self.tmp.name).resolve() / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        tools.ROOT = self.root
        search.TIME_BUDGET = 10.0
        self.add("a.py", "def run_turn():\n    pass\n")
        self.add("sub/b.py", "x = 1\n# run_turn here\n")
        self.add("sub/deep/c.py", "print('hello')\n")
        self.add("sub/c.txt", "Run_Turn mention\n")
        self.add("notes.md", "# Notes\n")
        self.add(".env", "SECRET=run_turn\n")
        self.add(".env.example", "KEY=run_turn\n")
        self.add(".venv/lib/z.py", "run_turn\n")
        self.add("node_modules/m.js", "run_turn\n")
        self.add(".git/config", "run_turn\n")
        self.add("__pycache__/x.pyc.txt", "run_turn\n")
        (self.root / "bin.dat").write_bytes(b"\0\1\2 run_turn \3")
        (self.root / "big.txt").write_text("run_turn\n" + "x" * 1_100_000)
        (self.outside / "stolen.txt").write_text("run_turn from outside\n")
        os.symlink(self.outside / "stolen.txt", self.root / "link_out.txt")
        os.symlink(self.outside, self.root / "link_dir")

    def add(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def grep(self, pattern, path=".", **kw):
        return tools.run_tool("grep", {"pattern": pattern, "path": path, **kw})

    def glob(self, pattern, path=".", **kw):
        return tools.run_tool("glob", {"pattern": pattern, "path": path, **kw})


def lines(out):
    """Result lines without the trailing '(...)' summary."""
    return [ln for ln in out.splitlines() if not ln.startswith("(")]


# ===== grep ========================================================================================

def test_grep_finds_matches_with_path_line_and_text():
    p = Project()
    out = p.grep("run_turn", ignore_case=False)
    found = lines(out)
    assert "a.py:1: def run_turn():" in found
    assert "sub/b.py:2: # run_turn here" in found
    assert out.splitlines()[-1].startswith("(")


def test_grep_never_reads_secrets_caches_binaries_big_files_or_outside_links():
    p = Project()
    out = p.grep("run_turn", ignore_case=True)
    files = {ln.split(":")[0] for ln in lines(out)}
    assert files == {"a.py", "sub/b.py", "sub/c.txt", ".env.example"}, files
    assert ".env" not in files                       # .env.example is allowed, .env is not
    for forbidden in (".venv", "node_modules", ".git/", "__pycache__", "bin.dat",
                      "big.txt", "link_out", "link_dir", "stolen", "outside"):
        assert not any(f == forbidden or f.startswith(forbidden) for f in files), forbidden
    assert "skipped 1 file larger than 1 MB" in out


def test_grep_order_is_stable_and_sorted():
    p = Project()
    first = p.grep("run_turn", ignore_case=True)
    assert first == p.grep("run_turn", ignore_case=True)
    files = [ln.split(":")[0] for ln in lines(first)]
    assert files == [".env.example", "a.py", "sub/b.py", "sub/c.txt"]


def test_grep_ignore_case_and_literal():
    p = Project()
    assert "sub/c.txt" not in p.grep("run_turn")                  # case-sensitive by default
    assert "sub/c.txt:1: Run_Turn mention" in p.grep("run_turn", ignore_case=True)
    assert "sub/c.txt:1:" in p.grep("run_turn", ignore_case="true")  # a model sending "true"
    assert p.grep("def run_turn(").startswith("error: invalid regular expression")
    assert "a.py:1: def run_turn():" in p.grep("def run_turn(", literal=True)
    assert "a.py:1:" in p.grep("run_t.rn")                         # a real regex
    assert "a.py:" not in p.grep("run_t.rn", literal=True)         # not as plain text


def test_grep_include_filters_by_name_or_path():
    p = Project()
    assert {ln.split(":")[0] for ln in lines(p.grep("run_turn", include="*.py"))} == {"a.py", "sub/b.py"}
    assert {ln.split(":")[0] for ln in lines(p.grep("run_turn", include="sub/*.py"))} == {"sub/b.py"}
    both = p.grep("run_turn", ignore_case=True, include="*.{py,txt}")
    assert {ln.split(":")[0] for ln in lines(both)} == {"a.py", "sub/b.py", "sub/c.txt"}


def test_grep_path_can_be_a_folder_or_a_single_file():
    p = Project()
    assert {ln.split(":")[0] for ln in lines(p.grep("run_turn", path="sub"))} == {"sub/b.py"}
    assert lines(p.grep("run_turn", path="a.py")) == ["a.py:1: def run_turn():"]


def test_grep_refuses_secret_files_and_paths_outside_the_project():
    p = Project()
    assert "refusing to search a secrets file" in p.grep("SECRET", path=".env")
    assert "outside the working directory" in p.grep("x", path="..")
    assert "outside the working directory" in p.grep("x", path="../outside")
    # a symlinked folder is not followed, and the link is not searched as a file either
    assert "stolen" not in p.grep("outside", ignore_case=True)


def test_grep_result_cap_and_long_line_clipping():
    p = Project()
    p.add("many.txt", "\n".join(f"hit {i}" for i in range(300)))
    p.add("long.txt", "needle " + "x" * 1000 + "\n")
    out = p.grep("hit", max_results=10)
    assert len([ln for ln in lines(out) if ln.startswith("many.txt")]) == 10
    assert "stopped after 10 matches" in out
    assert "(stopped after 100 matches" in p.grep("hit")             # default cap
    assert "300 matches in 1 file" in p.grep("hit", max_results=99999)   # above the cap is clamped to 500
    long = [ln for ln in lines(p.grep("needle")) if ln.startswith("long.txt")][0]
    assert long.endswith("…") and len(long) < 230


def test_grep_no_matches_and_bad_input():
    p = Project()
    assert p.grep("zzz_not_here").startswith("no matches")
    assert "must not be empty" in p.grep("")
    assert p.grep("x", include="../*").startswith("error: pattern must not contain")
    assert p.grep("x", max_results="many").startswith("error: grep: argument 'max_results'")
    assert p.grep("x", ignore_case="maybe").startswith("error: grep: argument 'ignore_case'")


def test_grep_no_match_says_how_many_files_were_searched_and_hints_about_include():
    p = Project()
    out = p.grep("zzz_not_here", include="*.py")
    assert out.startswith("no matches (searched 3 files)"), out         # a.py, sub/b.py, sub/deep/c.py
    assert "no file matched" not in out
    out = p.grep("run_turn", include="src/**/*.ts")                    # what the 0.5B model tried
    assert out.startswith("no matches (searched 0 files)")
    assert "no file matched include='src/**/*.ts'" in out and "without `include`" in out
    assert p.grep("zzz", path="a.py").startswith("no matches (searched 1 file)")


def test_glob_no_match_says_how_many_files_were_checked():
    p = Project()
    out = p.glob("*.nothing")
    assert out.startswith("no files match (checked ") and "Patterns with '/'" not in out
    assert "Patterns with '/' match the path" in p.glob("src/**/*.ts")


def test_grep_time_budget_stops_and_says_so():
    p = Project()
    search.TIME_BUDGET = -1        # already expired
    try:
        out = p.grep("run_turn")
    finally:
        search.TIME_BUDGET = 10.0
    assert "search stopped after" in out and "incomplete" in out


def test_grep_reports_singular_and_plural():
    p = Project()
    assert p.grep("run_turn here", path="sub").splitlines()[-1] == "(1 match in 1 file)"
    assert p.grep("run_turn", include="*.py").splitlines()[-1] == "(2 matches in 2 files)"


# ===== glob ========================================================================================

def test_glob_patterns():
    p = Project()
    assert lines(p.glob("**/*.py")) == ["a.py", "sub/b.py", "sub/deep/c.py"]    # no .venv files
    assert lines(p.glob("*.py")) == ["a.py", "sub/b.py", "sub/deep/c.py"]       # no '/': any depth
    assert lines(p.glob("sub/*.py")) == ["sub/b.py"]
    assert lines(p.glob("sub/**/*.py")) == ["sub/b.py", "sub/deep/c.py"]
    assert lines(p.glob("*.{md,txt}")) == ["big.txt", "link_out.txt", "notes.md", "sub/c.txt"]
    assert lines(p.glob("[ab].py")) == ["a.py", "sub/b.py"]
    assert lines(p.glob("notes.md")) == ["notes.md"]
    assert lines(p.glob("n?tes.md")) == ["notes.md"]


def test_glob_skips_noise_folders_but_lists_hidden_files():
    p = Project()
    everything = lines(p.glob("**/*"))
    for noisy in (".venv/lib/z.py", "node_modules/m.js", ".git/config", "__pycache__/x.pyc.txt"):
        assert noisy not in everything, noisy
    assert ".env.example" in everything and ".env" in everything       # names are fine to list


def test_glob_path_argument_and_paths_are_project_relative():
    p = Project()
    assert lines(p.glob("*.py", path="sub")) == ["sub/b.py", "sub/deep/c.py"]
    assert lines(p.glob("deep/*.py", path="sub")) == ["sub/deep/c.py"]
    assert "outside the working directory" in p.glob("*", path="../outside")
    assert "path is not a folder" in p.glob("*", path="a.py")


def test_glob_rejects_patterns_that_escape():
    p = Project()
    for bad in ("/etc/*", "~/*", "../*", "sub/../../*", "a/../b"):
        assert p.glob(bad).startswith("error:"), bad


def test_glob_cap_no_match_and_empty_pattern():
    p = Project()
    for i in range(30):
        p.add(f"many/f{i:02}.dat", "")
    out = p.glob("many/*.dat", max_results=5)
    assert len(lines(out)) == 5 and "25 more not shown" in out
    assert p.glob("*.nothing").startswith("no files match")
    assert "must not be empty" in p.glob("")


def test_glob_does_not_follow_folder_symlinks():
    p = Project()
    everything = lines(p.glob("**/*"))
    assert not any(f.startswith("link_dir/") for f in everything)


# ===== glob pattern translation (unit) ==============================================================

def test_expand_braces():
    assert search.expand_braces("*.{py,js}") == ["*.py", "*.js"]
    assert search.expand_braces("{a,b}/{c,d}") == ["a/c", "a/d", "b/c", "b/d"]
    assert search.expand_braces("no_braces") == ["no_braces"]
    assert search.expand_braces("x.{a,{b,c}}") == ["x.a", "x.b", "x.c"]
    try:
        search.expand_braces("{" + ",".join(["a"] * 80) + "}")
    except ValueError as e:
        assert "too many" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_glob_special_characters_are_literal():
    m = search.compile_glob("file(1)+v2.txt")
    assert m("file(1)+v2.txt") and not m("file1v2.txt")
    assert search.compile_glob("a[b")("a[b")                       # unclosed bracket: literal
    assert search.compile_glob("[!a]x")("bx") and not search.compile_glob("[!a]x")("ax")


# ===== tool wiring ===================================================================================

def test_tools_are_registered_and_described():
    names = [t["name"] for t in tools.TOOL_SPECS]
    assert "grep" in names and "glob" in names
    bash = next(t for t in tools.TOOL_SPECS if t["name"] == "bash")
    assert "grep, glob" in bash["description"]
    spec = next(t for t in tools.TOOL_SPECS if t["name"] == "grep")
    assert spec["parameters"]["required"] == ["pattern"]
    assert spec["parameters"]["properties"]["ignore_case"]["type"] == "boolean"


def test_null_optional_arguments_are_treated_as_not_given():
    p = Project()
    out = tools.run_tool("grep", {"pattern": "run_turn", "include": None, "path": None,
                                  "ignore_case": None})
    assert "a.py:1:" in out
    assert tools.run_tool("grep", {"pattern": None}).startswith("error: grep: missing required")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
