# TODO

## Done
- [x] Chat loop with tool calls (`agent.py`), 15-step cap, stops after repeated tool failures
- [x] Multi-provider failover: Gemini, Groq, Cerebras, OpenRouter, Ollama
  - [x] Retry and backoff on transient errors (503, 429, timeouts), request timeouts
  - [x] Fast failover when another provider exists; escalating cooldown for flaky ones
  - [x] Skip a provider for the session on bad key / unknown model (401, 403, 404)
  - [x] Neutral history format, provider-specific `raw` kept for Gemini
  - [x] Notice when switching providers, with timing
- [x] `write_file` / `edit_file` with diff preview and approval (y/N/a)
- [x] `bash` tool with approval, timeout, secret-stripped environment, risky-command warnings
- [x] bubblewrap sandbox, switchable at runtime (`/sandbox`), `.env` hidden, network off by default
- [x] Hardening: path confinement, `.env` and `.git` protection, argument type checks,
      terminal escape sanitising, bounded `read_file` (offset/limit)
- [x] Context management: per-provider limits, old tool output shortened, oldest turns dropped, `/context`, `/clear`
- [x] Gemini that rejects another provider's tool calls (400 thought_signature) is skipped for the session
- [x] `/auto files|shell on|off` switch (shell auto-approve only for sandboxed, safe-looking commands)
- [x] `grep` and `glob` (pure Python, capped, skips noise folders)
- [x] Offline test suite (fake providers, fake bwrap, real-bwrap tests that skip when unavailable)

## Next
- [ ] Only list models with reliable tool calling in the fallback chain (document which ones)

## Later
- [ ] Summarise dropped turns (a model-written recap) instead of just leaving them out; could grow
      into notes that persist between sessions
- [ ] Count real tokens (provider usage numbers) instead of characters
- [ ] Let Gemini rejoin mid-conversation after another provider's tool calls (placeholder
      thought signature; check Google's docs first)
- [ ] TUI / GUI (long commands and diffs do not fit a small terminal window)
- [ ] Image support: a `view_image` tool for models with vision, and a clear message otherwise
- [ ] `.gitignore`-aware search, and optional ripgrep acceleration when `rg` is installed
- [ ] Per-project config file (allowed commands, default sandbox mode, extra read-only paths)
- [ ] Persistent session history / resume

## Known limits
- bubblewrap is not a virtual machine; a kernel bug could still escape it (see SANDBOX.md)
- Regex search runs inside SLOP, so a pathological pattern can be slow
- Very small models (e.g. qwen2.5:0.5b) call tools wrongly and summarise unreliably; the guard
  rails keep that harmless, but use a larger model for real work
