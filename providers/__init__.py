"""Providers. `build_provider()` reads PROVIDERS and the API keys from the environment."""
import os
import sys

from .base import Reply, ToolCall
from .fallback import FallbackProvider

# name -> (default base URL, API key variable, model variable)
OPENAI_COMPAT = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "CEREBRAS_MODEL"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"),
    "ollama": ("http://localhost:11434/v1", None, "OLLAMA_MODEL"),  # local, no key needed
}


class ConfigError(Exception):
    pass


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# Longest reply (in tokens) a provider may generate. Local small models can get stuck
# repeating themselves until the request times out; a cap stops that. Others: no cap.
DEFAULT_MAX_TOKENS = {"ollama": 4096}


def _max_tokens(name: str):
    """NAME_MAX_TOKENS from the environment: a number, or off/none/0 for no cap."""
    var = f"{name.upper()}_MAX_TOKENS"
    raw = _env(var).lower()
    if not raw:
        return DEFAULT_MAX_TOKENS.get(name)
    if raw in ("off", "none", "0", "unlimited"):
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{var} must be a whole number (or off), not {raw!r}") from None
    if value < 1:
        raise ConfigError(f"{var} must be at least 1 (or off)")
    return value


# Most text (history + system prompt + tool descriptions, in characters, ~4 per token) sent in
# one request. Older conversation is shortened or left out beyond this. These are conservative
# guesses: small local models and free tiers have small windows. Change them in .env.
DEFAULT_CONTEXT_CHARS = {"ollama": 16_000, "groq": 40_000}
FALLBACK_CONTEXT_CHARS = 200_000
MIN_CONTEXT_CHARS = 4_000


def _context_chars(name: str):
    """<NAME>_CONTEXT_CHARS, else SLOP_CONTEXT_CHARS, else the default. off/0 = no limit."""
    for var in (f"{name.upper()}_CONTEXT_CHARS", "SLOP_CONTEXT_CHARS"):
        raw = _env(var).lower()
        if not raw:
            continue
        if raw in ("off", "none", "0", "unlimited"):
            return None
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{var} must be a whole number (or off), not {raw!r}") from None
        if value < MIN_CONTEXT_CHARS:
            raise ConfigError(f"{var} must be at least {MIN_CONTEXT_CHARS} (or off)")
        return value
    return DEFAULT_CONTEXT_CHARS.get(name, FALLBACK_CONTEXT_CHARS)


def _build_one(name: str):
    if name == "gemini":
        key = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
        if not key:
            raise ConfigError("GEMINI_API_KEY is not set")
        try:
            from .gemini import GeminiProvider
        except ImportError as e:
            raise ConfigError(f"missing package ({e}); run pip install -r requirements.txt")
        provider = GeminiProvider(model=_env("GEMINI_MODEL") or None, api_key=key)
        provider.context_chars = _context_chars(name)
        return provider

    if name in OPENAI_COMPAT:
        default_url, key_var, model_var = OPENAI_COMPAT[name]
        key = _env(key_var) if key_var else "ollama"  # Ollama ignores the key
        model = _env(model_var)
        if not key:
            raise ConfigError(f"{key_var} is not set")
        if not model:
            raise ConfigError(f"{model_var} is not set (check the provider's docs for a current model)")
        try:
            from .openai_compat import OpenAICompatProvider

            provider = OpenAICompatProvider(
                name, _env(f"{name.upper()}_BASE_URL") or default_url, key, model,
                max_tokens=_max_tokens(name),
            )
            provider.context_chars = _context_chars(name)
            return provider
        except ImportError as e:
            raise ConfigError(f"missing package ({e}); run pip install -r requirements.txt")

    raise ConfigError(f"unknown provider (known: gemini, {', '.join(OPENAI_COMPAT)})")


def build_provider(names: list[str] | None = None) -> FallbackProvider:
    """Build the provider chain, in priority order. Always wrapped for retry/backoff."""
    if names is None:
        raw = os.environ.get("PROVIDERS") or "gemini"
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    providers = []
    for n in names:
        try:
            providers.append(_build_one(n))
        except ConfigError as e:
            print(f"warning: skipping {n}: {e}", file=sys.stderr)
    if not providers:
        raise SystemExit("No usable provider. Check PROVIDERS and your .env (see .env.example).")
    return FallbackProvider(providers)
