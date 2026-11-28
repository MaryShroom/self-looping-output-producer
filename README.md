# Self-Looping Output Producer

A small Claude Code-style coding assistant for the terminal, written in Python. You type a
request; the model reads, searches and edits your project and runs commands, asking you before
anything that changes something.

> Early project. It works, but expect rough edges. Local models (tiny ones especially) make
> mistakes; judge results by the `|` lines SLOP prints under each tool call, not by the model's
> summary.

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

Keys, models and options live in `.env` (never committed). See `.env.example` for all of them.

## Providers

SLOP talks to several providers and fails over between them, in the order of `PROVIDERS`:

| Provider   | Notes                                            |
|------------|--------------------------------------------------|
| Gemini     | `GEMINI_API_KEY`, `GEMINI_MODEL`                 |
| Groq       | `GROQ_API_KEY`, `GROQ_MODEL`                     |
| Cerebras   | `CEREBRAS_API_KEY`, `CEREBRAS_MODEL`             |
| OpenRouter | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`         |
| Ollama     | local, no key, `OLLAMA_MODEL`                    |

```
PROVIDERS=openrouter,gemini,groq,ollama
```

If a provider is overloaded or rate-limited, SLOP moves to the next one straight away and prints
which one answered and how long it took. A provider that keeps failing is put on a cooldown; one
with a bad key or unknown model is skipped for the rest of the session.

## Tools

| Tool         | What it does                                                          | Asks first? |
|--------------|-----------------------------------------------------------------------|-------------|
| `read_file`  | Reads a text file, 500 lines per call by default (`offset`, `limit`)  | no          |
| `list_files` | Lists a folder                                                        | no          |
| `grep`       | Searches file contents (regex or plain text, `include` filter)        | no          |
| `glob`       | Finds files by name (`*.py`, `**/*.py`, `tests/test_*.py`, `*.{md,txt}`) | no       |
| `write_file` | Creates or overwrites a file, shows a diff                            | yes (y/N/a) |
| `edit_file`  | Replaces one exact piece of text in a file, shows a diff              | yes (y/N/a) |
| `bash`       | Runs a shell command (in a sandbox when available)                    | yes (y/N)   |

Answering `a` at a file-change prompt approves the rest of the session's file changes. Shell
commands are always asked about.

### Guard rails

- Everything stays inside the folder you started SLOP in; `..` and symlinks pointing out are refused.
- `.env*` files (except `.env.example`) cannot be read, searched or written, and `.git/` cannot be written.
- Reads are bounded: output is capped in lines and characters, very long lines are clipped, and
  binary files, pipes and devices are refused. A huge file is never loaded into memory.
- `grep` and `glob` skip `.git`, virtual environments, `node_modules` and caches, skip binary
  files and files over 1 MB, and stop after a result cap or a time budget (and say so).
- Tool arguments are type-checked; a wrong call gets a clear error the model can fix, and four
  failed calls in a row stop the turn.
- A turn is limited to 15 tool round-trips.
- Terminal escape sequences in model output or file contents are shown as text, never obeyed.
- API keys are removed from the environment of every command the model runs.

## Sandbox for shell commands

`bash` runs inside [bubblewrap](https://github.com/containers/bubblewrap) when it is installed:
the project folder is writable, the rest of the system is read-only, `.env` files are unreadable
and there is no network by default. Switch it while running:

```
/sandbox            show status
/sandbox test       check that it works on this machine
/sandbox on|auto|off
/sandbox net on|off
```

It is a strong barrier, not a virtual machine. Setup (including the Ubuntu 24.04 AppArmor
note) and its limits are in [SANDBOX.md](SANDBOX.md).

## Layout

- `main.py` — startup, `.env` loading, the input loop and slash commands
- `agent.py` — the agent loop (model -> tools -> model ...)
- `tools.py` — tool implementations, schemas, argument checking, approval prompts
- `search.py` — `grep` and `glob`
- `shell.py` — the `bash` tool and the bubblewrap sandbox
- `providers/` — Gemini and OpenAI-compatible adapters, plus the failover wrapper

## Tests

No network or API keys needed:

```bash
for f in tests/test_*.py; do python3 "$f"; done      # or: pytest
```

The three real-sandbox tests skip themselves when bubblewrap is not usable.

## Try

- `find where run_turn is defined`
- `list all test files`
- `read main.py and explain how failover works`
- `add a docstring to agent.py` (shows a diff and asks first)
- `run the tests` (asks before running)

## Options worth knowing

| Variable                   | Effect                                                     |
|----------------------------|------------------------------------------------------------|
| `PROVIDERS`                | Provider order for failover                                |
| `SLOP_SANDBOX`             | `auto` (default), `on`, `off`                              |
| `SLOP_SANDBOX_NETWORK`     | `off` (default) or `on`                                    |
| `SLOP_SANDBOX_RO`          | Extra read-only folders inside the sandbox                 |
| `SLOP_SHOW_OUTPUT`         | `off` hides the `\|` result previews                       |
| `<PROVIDER>_MAX_TOKENS`    | Cap on reply length (Ollama defaults to 4096)              |
