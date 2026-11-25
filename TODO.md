# TODO

## Multi-provider failover
Swap providers automatically when one is overloaded (e.g. Gemini 503).

- [ ] Retry with exponential backoff on transient errors (503, 429, timeouts)
- [ ] `OpenAICompatProvider` for Groq, Cerebras, OpenRouter, Ollama (base URL, key, model via env vars)
- [ ] Neutral history format (text + tool calls), with optional provider-specific `raw` kept for Gemini
- [ ] `FallbackProvider` wrapper: ordered provider list (env var), fail over on transient errors only
- [ ] Print a notice when switching providers (e.g. `switched to groq`)
- [ ] Only list models with reliable tool calling in the fallback chain
- [ ] Handle smaller context windows on fallback providers
- [ ] If switching back to Gemini fails on thought signatures, stay on the fallback for the rest of the task
