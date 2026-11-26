# TODO

## Multi-provider failover (branch: failover)
- [x] Retry with exponential backoff on transient errors (503, 429, timeouts)
- [x] `OpenAICompatProvider` for Groq, Cerebras, OpenRouter, Ollama (base URL, key, model via env vars)
- [x] Neutral history format (text + tool calls), with optional provider-specific `raw` kept for Gemini
- [x] `FallbackProvider` wrapper: ordered provider list (env var), fail over on transient errors only
- [x] Print a notice when switching providers (e.g. `[switched to groq]`)
- [x] Only list models with reliable tool calling in the fallback chain (verify per model)
- [x] Handle smaller context windows on fallback providers
- [x] Test against the real APIs (only fake-provider tests so far)
- [ ] If switching back to Gemini fails on thought signatures, stay on the fallback for the rest of the task

## Next
- [ ] `write_file` / `edit_file` with confirmation prompt
- [ ] `bash` tool with confirmation
- [ ] Search tools (grep, glob)
- [ ] Context management
