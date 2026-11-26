"""Tests for failover logic and the OpenAI-compatible adapter (no network, no SDKs needed).

Run:  python tests/test_fallback.py     (or: pytest)
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.base import Reply  # noqa: E402
from providers.fallback import FallbackProvider  # noqa: E402
from providers.openai_compat import OpenAICompatProvider  # noqa: E402


class FakeErr(Exception):
    def __init__(self, code):
        super().__init__(f"{code} error from fake provider")
        self.code = code


class Fake:
    """Provider whose responses follow a script: an Exception to raise, or anything else = success."""

    def __init__(self, name, script=()):
        self.name = name
        self.script = list(script)
        self.calls = 0

    def generate(self, system, history, tools):
        self.calls += 1
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, Exception):
            raise item
        return Reply(text=f"from {self.name}", provider=self.name)


def make(providers, **kw):
    log, sleeps, now = [], [], [0.0]
    fb = FallbackProvider(
        providers, notify=log.append, sleep=sleeps.append, clock=lambda: now[0], **kw
    )
    return fb, log, sleeps, now


def test_primary_ok():
    a, b = Fake("a"), Fake("b")
    fb, log, sleeps, _ = make([a, b])
    assert fb.generate("s", [], []).provider == "a"
    assert sleeps == [] and log == [] and b.calls == 0


def test_retry_then_success():
    a, b = Fake("a", [FakeErr(503)]), Fake("b")
    fb, log, sleeps, _ = make([a, b])
    assert fb.generate("s", [], []).provider == "a"
    assert sleeps == [1.0] and b.calls == 0


def test_failover_after_retries():
    a, b = Fake("a", [FakeErr(503)] * 3), Fake("b")
    fb, log, sleeps, _ = make([a, b])
    assert fb.generate("s", [], []).provider == "b"
    assert a.calls == 3 and sleeps == [1.0, 2.0]


def test_rate_limit_switches_immediately():
    a, b = Fake("a", [FakeErr(429)]), Fake("b")
    fb, log, sleeps, _ = make([a, b])
    assert fb.generate("s", [], []).provider == "b"
    assert a.calls == 1 and sleeps == []


def test_failover_message_includes_reason():
    a, b = Fake("a", [FakeErr(429)]), Fake("b")
    fb, log, *_ = make([a, b])
    fb.generate("s", [], [])
    assert any("429" in line and "trying b" in line for line in log), log


def test_non_transient_error_raises():
    a, b = Fake("a", [FakeErr(401)]), Fake("b")
    fb, *_ = make([a, b])
    try:
        fb.generate("s", [], [])
    except FakeErr:
        pass
    else:
        raise AssertionError("expected FakeErr")
    assert b.calls == 0


def test_cooldown_skips_failed_provider_then_retries_it():
    a, b = Fake("a", [FakeErr(503)] * 3), Fake("b")
    fb, log, sleeps, now = make([a, b])
    fb.generate("s", [], [])           # a fails 3x, b answers
    assert a.calls == 3
    fb.generate("s", [], [])           # a is cooling down: straight to b
    assert a.calls == 3 and b.calls == 2
    now[0] = 61.0                      # cooldown over: a is tried again and recovers
    assert fb.generate("s", [], []).provider == "a"
    assert any("switched to a" in line for line in log)


def test_all_fail():
    a, b = Fake("a", [FakeErr(503)] * 3), Fake("b", [FakeErr(500)] * 3)
    fb, *_ = make([a, b])
    try:
        fb.generate("s", [], [])
    except RuntimeError as e:
        assert "All providers failed" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_openai_compat_roundtrip():
    captured = {}
    msg = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="abc123",
                function=SimpleNamespace(name="read_file", arguments='{"path": "main.py"}'),
            )
        ],
    )

    def create(**kw):
        captured.update(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    p = OpenAICompatProvider("groq", "http://x", "k", "some-model", client=client)

    history = [
        {"role": "user", "text": "hi"},
        # an assistant turn written by a different provider (raw is ignored here)
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [{"id": "z9", "name": "list_files", "args": {}}],
            "raw": {"gemini": object()},
        },
        {"role": "tool", "id": "z9", "name": "list_files", "result": "main.py"},
    ]
    reply = p.generate("sys", history, [{"name": "read_file", "description": "d", "parameters": {}}])

    msgs = captured["messages"]
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "list_files", "arguments": "{}"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "z9", "content": "main.py"}
    assert captured["tools"][0]["function"]["name"] == "read_file"
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].args == {"path": "main.py"}
    assert reply.tool_calls[0].id == "abc123"
    assert reply.provider == "groq"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
