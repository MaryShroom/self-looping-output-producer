"""Tests for context management: shortening old output, dropping old turns, per-provider limits.

Run:  python tests/test_context.py     (or: pytest)
"""
import contextlib
import copy
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands  # noqa: E402
import providers  # noqa: E402
from providers import context  # noqa: E402
from providers.base import Reply  # noqa: E402
from providers.fallback import FallbackProvider  # noqa: E402


def turn(n, result_chars=3000, question=None):
    """One user message, one tool call with its result, one final answer."""
    return [
        {"role": "user", "text": question or f"question {n}"},
        {"role": "assistant", "text": "", "tool_calls": [{"id": f"c{n}", "name": "read_file",
                                                          "args": {"path": f"f{n}.txt"}}], "raw": {}},
        {"role": "tool", "id": f"c{n}", "name": "read_file", "result": f"{n}" * result_chars},
        {"role": "assistant", "text": f"answer {n}", "tool_calls": [], "raw": {}},
    ]


def convo(turns, **kw):
    out = []
    for n in range(1, turns + 1):
        out += turn(n, **kw)
    return out


def assert_well_formed(msgs):
    assert msgs and msgs[0]["role"] == "user", "must start with a user message"
    open_calls = set()
    for m in msgs:
        if m["role"] == "user":
            assert not open_calls, "a tool call lost its result"
        elif m["role"] == "assistant":
            open_calls |= {c["id"] for c in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            assert m["id"] in open_calls, "a tool result lost its tool call"
            open_calls.discard(m["id"])
    assert not open_calls


# ===== fit() =======================================================================================

def test_small_conversations_are_returned_untouched():
    h = convo(2, result_chars=100)
    out, stats = context.fit(h, 50_000)
    assert out is h and stats == {"shrunk": 0, "dropped": 0}
    out, _ = context.fit(h, None)
    assert out is h


def test_old_tool_output_is_shortened_first_and_nothing_is_dropped():
    h = convo(4, result_chars=3000)                       # ~12k of tool output
    out, stats = context.fit(h, 9_000)
    assert stats["dropped"] == 0 and stats["shrunk"] >= 1
    assert context.total(out) <= 9_000
    assert len(out) == len(h)
    assert_well_formed(out)
    assert "[output trimmed:" in out[2]["result"] and out[2]["result"].startswith("1" * 400)
    assert out[-2]["result"] == "4" * 3000                 # the newest turn is intact


def test_oldest_turns_go_first_and_whole():
    h = convo(6, result_chars=3000)
    out, stats = context.fit(h, 6_000)
    assert stats["dropped"] > 0 and stats["dropped"] % 4 == 0      # whole turns of 4 messages
    assert context.total(out) <= 6_000
    assert_well_formed(out)
    assert out[0]["text"].startswith(context.DROP_NOTE)
    assert [m["text"] for m in out if m["role"] == "user"][-1] == "question 6"
    assert out[-1]["text"] == "answer 6"
    assert "question 1" not in "".join(m.get("text", "") for m in out)


def test_the_stored_history_is_never_changed():
    h = convo(6, result_chars=3000)
    before = copy.deepcopy(h)
    context.fit(h, 5_000)
    assert h == before


def test_the_current_question_survives_even_when_everything_else_is_too_big():
    h = convo(3, result_chars=3000) + turn(4, result_chars=50_000, question="the current question")
    out, stats = context.fit(h, 8_000)
    assert_well_formed(out)
    assert out[-4]["text"].endswith("the current question")
    assert out[-2]["id"] == "c4" and len(out[-2]["result"]) < 50_000     # its own output is cut
    assert out[-2]["result"].startswith("4" * 200)
    assert stats["dropped"] > 0


def test_current_turn_keeps_its_newest_output_longest():
    h = [{"role": "user", "text": "go"}]
    for i in range(4):
        h += [{"role": "assistant", "text": "", "tool_calls": [{"id": f"x{i}", "name": "grep", "args": {}}], "raw": {}},
              {"role": "tool", "id": f"x{i}", "name": "grep", "result": f"{i}" * 3000}]
    out, _ = context.fit(h, 7_000)
    assert_well_formed(out)
    results = [m["result"] for m in out if m["role"] == "tool"]
    assert len(results[-1]) >= len(results[0])
    assert context.total(out) <= 7_000


def test_it_gives_up_gracefully_when_nothing_more_can_be_cut():
    h = [{"role": "user", "text": "x" * 20_000}]
    out, stats = context.fit(h, 5_000)
    assert out[0]["text"] == "x" * 20_000 and stats["dropped"] == 0     # never cut the user's text


def test_messages_before_the_first_user_message_do_not_crash():
    h = [{"role": "assistant", "text": "hi", "tool_calls": [], "raw": {}}] + convo(3, result_chars=3000)
    out, _ = context.fit(h, 5_000)
    assert out


def test_result_shortening_keeps_ids_and_names():
    h = convo(3, result_chars=5000)
    out, _ = context.fit(h, 8_000)
    for a, b in zip(h, out):
        if a["role"] == "tool":
            assert (a["id"], a["name"]) == (b["id"], b["name"])


# ===== per-provider limits ===========================================================================

class Spy:
    def __init__(self, name, limit=None, fail=None):
        self.name, self.context_chars, self.fail, self.seen = name, limit, fail, []

    def generate(self, system, history, tools):
        self.seen.append(history)
        if self.fail:
            raise self.fail
        return Reply(text="ok", provider=self.name)


class Boom(Exception):
    code = 503


def test_each_provider_gets_a_history_that_fits_its_own_limit():
    small, big = Spy("small", 12_000, fail=Boom()), Spy("big", None)
    notes = []
    fb = FallbackProvider([small, big], notify=notes.append, sleep=lambda s: None)
    h = convo(40, result_chars=3000)
    fb.generate("system", h, [])
    assert context.total(small.seen[0]) <= 12_000
    assert big.seen[0] is h                                # no limit: the full conversation
    assert any("context trimmed for small" in n for n in notes), notes


def test_trimming_is_announced_once_not_on_every_step():
    p = Spy("p", 12_000)
    notes = []
    fb = FallbackProvider([p], notify=notes.append, sleep=lambda s: None)

    def announced():
        return len([n for n in notes if "context trimmed" in n])

    h = convo(40, result_chars=3000)
    for _ in range(3):
        fb.generate("system", h, [])
    assert announced() == 1
    for extra in range(3):                                  # the conversation keeps growing
        h = h + turn(100 + extra, result_chars=3000)
        fb.generate("system", h, [])
    assert announced() == 1
    fb.generate("system", convo(1, result_chars=100), [])   # e.g. after /clear: nothing dropped
    fb.generate("system", convo(40, result_chars=3000), [])
    assert announced() == 2                                 # a new trimming episode is announced


def test_system_prompt_and_tool_descriptions_count_against_the_limit():
    p = Spy("p", 12_000)
    fb = FallbackProvider([p], notify=lambda s: None)
    tools = [{"name": "t", "description": "d" * 6000, "parameters": {}}]
    fb.generate("s" * 3000, convo(4, result_chars=1000), tools)
    assert context.total(p.seen[0]) <= max(12_000 - 9_000, context.MIN_BUDGET)


def test_providers_without_a_limit_attribute_still_work():
    class Plain:
        name = "plain"
        def generate(self, system, history, tools):
            return Reply(text="ok", provider="plain")
    assert FallbackProvider([Plain()]).generate("s", convo(2), []).text == "ok"


# ===== configuration ===================================================================================

def with_env(**env):
    keys = [k for k in os.environ if k.endswith("_CONTEXT_CHARS")]
    saved = {k: os.environ.pop(k) for k in keys}
    os.environ.update(env)
    return saved, list(env)


def restore(saved, added):
    for k in added:
        os.environ.pop(k, None)
    os.environ.update(saved)


def test_context_limits_defaults_and_overrides():
    saved, added = with_env()
    try:
        assert providers._context_chars("ollama") == 16_000
        assert providers._context_chars("groq") == 40_000
        assert providers._context_chars("gemini") == 200_000
        os.environ["SLOP_CONTEXT_CHARS"] = "30000"
        assert providers._context_chars("gemini") == 30_000
        os.environ["OLLAMA_CONTEXT_CHARS"] = "off"             # the specific variable wins
        assert providers._context_chars("ollama") is None
        os.environ["GROQ_CONTEXT_CHARS"] = "50000"
        assert providers._context_chars("groq") == 50_000
    finally:
        restore(saved, added + ["SLOP_CONTEXT_CHARS", "OLLAMA_CONTEXT_CHARS", "GROQ_CONTEXT_CHARS"])


def test_bad_context_limits_are_rejected():
    saved, added = with_env()
    try:
        for bad in ("lots", "10", "-5"):
            os.environ["SLOP_CONTEXT_CHARS"] = bad
            try:
                providers._context_chars("gemini")
            except providers.ConfigError as e:
                assert "SLOP_CONTEXT_CHARS" in str(e)
            else:
                raise AssertionError(bad)
    finally:
        restore(saved, added + ["SLOP_CONTEXT_CHARS"])


# ===== /context and /clear ===============================================================================

def cmd(line, history, provider=None):
    with contextlib.redirect_stdout(io.StringIO()):
        return commands.handle_slash(line, history=history, provider=provider)


def test_context_command_reports_size_and_limits():
    p = FallbackProvider([Spy("a", 40_000), Spy("b", 12_000), Spy("c", None)])
    out = cmd("/context", convo(8, result_chars=3000), p)
    assert "conversation: 32 messages" in out
    assert "a: limit 40,000" in out and "b: limit 12,000" in out and "c: no limit set" in out
    assert "trimmed" in out.split("b: limit")[1]                       # the small one is over
    assert "trimmed" not in out.split("b: limit")[0].split("a: limit")[1]


def test_clear_command_empties_the_history_in_place():
    h = convo(2)
    same = h
    assert "cleared 8 messages" in cmd("/clear", h)
    assert h == [] and same is h
    assert commands.handle_slash("/clear") is None            # needs a history to act on


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
