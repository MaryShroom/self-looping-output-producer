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


def _build_one(name: str):
    if name == "gemini":
        key = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
        if not key:
            raise ConfigError("GEMINI_API_KEY is not set")
        try:
            from .gemini import GeminiProvider
        except ImportError as e:
            raise ConfigError(f"missing package ({e}); run pip install -r requirements.txt")
        return GeminiProvider(model=_env("GEMINI_MODEL") or None, api_key=key)

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

            return OpenAICompatProvider(
                name, _env(f"{name.upper()}_BASE_URL") or default_url, key, model
            )
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
