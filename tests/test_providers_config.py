"""Tests for the optional max-tokens cap on OpenAI-compatible providers (no network, no SDKs).

Run:  python tests/test_providers_config.py     (or: pytest)
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import ConfigError, _max_tokens  # noqa: E402
from providers.openai_compat import OpenAICompatProvider  # noqa: E402


def _clear():
    for name in ("OLLAMA", "GROQ", "CEREBRAS", "OPENROUTER"):
        os.environ.pop(f"{name}_MAX_TOKENS", None)


def test_defaults():
    _clear()
    assert _max_tokens("ollama") == 4096       # local small models can loop: capped by default
    assert _max_tokens("groq") is None         # cloud providers: no cap


def test_env_values():
    _clear()
    os.environ["OLLAMA_MAX_TOKENS"] = "1024"
    os.environ["GROQ_MAX_TOKENS"] = " 8192 "
    assert _max_tokens("ollama") == 1024 and _max_tokens("groq") == 8192
    for off in ("off", "OFF", "none", "0", "unlimited"):
        os.environ["OLLAMA_MAX_TOKENS"] = off
        assert _max_tokens("ollama") is None, off
    os.environ["OLLAMA_MAX_TOKENS"] = ""
    assert _max_tokens("ollama") == 4096       # empty = default
    _clear()


def test_invalid_values_raise_config_error():
    _clear()
    for bad in ("lots", "12.5", "-3"):
        os.environ["GROQ_MAX_TOKENS"] = bad
        try:
            _max_tokens("groq")
        except ConfigError as e:
            assert "GROQ_MAX_TOKENS" in str(e)
        else:
            raise AssertionError(f"expected ConfigError for {bad!r}")
    _clear()


def _client(finish_reason=None, content="hi", seen=None):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)

    def create(**kw):
        if seen is not None:
            seen.update(kw)
        return SimpleNamespace(choices=[choice])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_max_tokens_is_sent_only_when_set():
    seen = {}
    p = OpenAICompatProvider("ollama", "http://x", "k", "m", client=_client(seen=seen), max_tokens=512)
    p.generate("sys", [{"role": "user", "text": "hi"}], [])
    assert seen["max_tokens"] == 512
    seen.clear()
    p = OpenAICompatProvider("groq", "http://x", "k", "m", client=_client(seen=seen))
    p.generate("sys", [{"role": "user", "text": "hi"}], [])
    assert "max_tokens" not in seen


def test_cut_off_reply_is_marked():
    p = OpenAICompatProvider("ollama", "http://x", "k", "m",
                             client=_client(finish_reason="length", content="partial"), max_tokens=5)
    reply = p.generate("sys", [{"role": "user", "text": "hi"}], [])
    assert reply.text.startswith("partial") and "cut off at the max_tokens limit" in reply.text
    p = OpenAICompatProvider("ollama", "http://x", "k", "m",
                             client=_client(finish_reason="stop", content="done"))
    assert p.generate("sys", [{"role": "user", "text": "hi"}], []).text == "done"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
