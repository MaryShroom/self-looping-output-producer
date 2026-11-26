# Self-Looping Output Producer

A tiny Claude Code-style agent: terminal chat + tool loop, with automatic
failover between LLM providers (Gemini, Groq, Cerebras, OpenRouter, Ollama).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then edit .env and add your keys
python main.py
```

Run it from the folder of the project you want it to look at; the tools are
restricted to that folder.

## Keys and providers (.env)

`.env` holds your API keys and is git-ignored. Set `PROVIDERS` to a
comma-separated priority list, e.g. `PROVIDERS=gemini,groq,cerebras`.
Providers without a key or model are skipped with a warning.

On a temporary failure (503 overloaded, timeout) the agent retries with
backoff, then moves to the next provider. A rate limit (429) switches
immediately. A bad key or bad request does not fail over, it shows the error.
A provider that just failed is skipped for 60 seconds.

## Layout

- `main.py`  - chat loop and the agent loop (model -> tools -> model ...)
- `providers/` - `base.py` (neutral message format, error classes),
  `gemini.py`, `openai_compat.py` (Groq/Cerebras/OpenRouter/Ollama),
  `fallback.py` (retry + failover), `__init__.py` (builds the chain from env)
- `tools.py` - tools the model can call (read_file, list_files, write_file, edit_file); refuses `.env*`
- `tests/test_fallback.py` - `python tests/test_fallback.py`

## Try

- `what files are in this project?`
- `read main.py and explain how the loop stops`
