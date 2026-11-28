"""Tests for the agent loop, using a scripted fake provider (no network).

Run:  python tests/test_agent.py     (or: pytest)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
from providers.base import Reply, ToolCall  # noqa: E402


class Scripted:
    """Provider that returns the given replies in order (the last one repeats)."""

    name = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def generate(self, system, history, tools):
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return Reply(text=reply.get("text", ""), provider="fake",
                     tool_calls=[ToolCall(n, a) for n, a in reply.get("calls", [])])


def run(provider, **kw):
    history, out = [{"role": "user", "text": "go"}], []
    agent.run_turn(provider, history, "system", say=out.append, **kw)
    return history, out


def assert_well_formed(history):
    """Every tool call has exactly one result, and the history does not end on a tool result."""
    asked = [c["id"] for m in history if m["role"] == "assistant" for c in m.get("tool_calls", [])]
    answered = [m["id"] for m in history if m["role"] == "tool"]
    assert sorted(asked) == sorted(answered), (asked, answered)
    assert history[-1]["role"] == "assistant", history[-1]


def test_plain_answer_returns_immediately():
    p = Scripted({"text": "hello"})
    history, out = run(p)
    assert p.calls == 1 and [m["role"] for m in history] == ["user", "assistant"]
    assert any("hello" in line for line in out) and any("(fake," in line for line in out)


def test_tool_call_then_answer_shows_args_and_result_preview():
    p = Scripted({"calls": [("list_files", {"path": "."})]}, {"text": "done"})
    history, out = run(p)
    assert p.calls == 2 and [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert any(line.startswith("  -> list_files(") for line in out)
    assert any("    | " in line for line in out)          # the raw result excerpt is shown
    assert_well_formed(history)


def test_long_arguments_are_shortened_when_printed():
    p = Scripted({"calls": [("write_file", {"path": "x", "content": "A" * 500})]}, {"text": "ok"})
    _, out = run(p, run=lambda name, args: "error: pretend")      # do not really write
    shown = next(line for line in out if line.startswith("  -> write_file("))
    assert "A" * 100 not in shown and "…" in shown


def test_loop_stops_after_repeated_tool_errors():
    bad = {"calls": [("read_file", {"path": "x", "invented": "1"})]}
    p = Scripted(bad)                                         # keeps making the same mistake
    history, out = run(p)
    assert p.calls == 4                                       # not 15
    assert any("4 tool errors in a row" in line for line in out)
    assert_well_formed(history)


def test_a_success_resets_the_error_count():
    bad = {"calls": [("read_file", {"nope": 1})]}
    good = {"calls": [("list_files", {})]}
    p = Scripted(bad, bad, bad, good, bad, bad, bad, {"text": "finished"})
    history, out = run(p)
    assert p.calls == 8 and not any("tool errors in a row" in line for line in out)
    assert_well_formed(history)


def test_every_call_in_one_reply_gets_a_result_even_when_stopping():
    bad = {"calls": [("read_file", {"x": 1})] * 3}            # 3 failing calls in one reply
    p = Scripted(bad)
    history, out = run(p)
    assert_well_formed(history)                               # no orphaned tool calls
    assert any("tool errors in a row" in line for line in out)


def test_step_limit_ends_cleanly():
    p = Scripted({"calls": [("list_files", {})]})             # succeeds forever
    history, out = run(p)
    assert p.calls == agent.MAX_STEPS
    assert any("too many tool steps" in line for line in out)
    assert_well_formed(history)


def test_terminal_codes_in_the_models_reply_are_shown_not_obeyed():
    reply = {"text": "safe\x1b[2Jhidden\rX\x07\nsecond line\twith tab"}
    history, out = run(Scripted(reply))
    printed = "\n".join(out)
    for raw in ("\x1b", "\r", "\x07"):
        assert raw not in printed, repr(raw)
    assert "\\x1b[2J" in printed
    assert "\nsecond line\twith tab" in printed       # line breaks and tabs are kept
    assert history[-1]["text"] == reply["text"]        # what the model said is stored unchanged


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
