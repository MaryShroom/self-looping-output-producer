"""Adapter for any OpenAI-compatible chat API (Groq, Cerebras, OpenRouter, Ollama, ...)."""
import json

from .base import Reply, ToolCall


class OpenAICompatProvider:
    def __init__(self, name, base_url, api_key, model, timeout=60, client=None):
        self.name = name
        self.model = model
        if client is None:
            from openai import OpenAI  # imported here so the module loads without the SDK

            # max_retries=0: FallbackProvider owns the retry/backoff policy.
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)
        self.client = client

    def _to_messages(self, system: str, history: list[dict]) -> list[dict]:
        msgs = [{"role": "system", "content": system}]
        for m in history:
            role = m["role"]
            if role == "user":
                msgs.append({"role": "user", "content": m["text"]})
            elif role == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["id"], "content": m["result"]})
            else:  # assistant
                calls = m.get("tool_calls") or []
                text = m.get("text") or None
                if not calls and text is None:
                    continue
                msg = {"role": "assistant", "content": text}
                if calls:
                    msg["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["args"])},
                        }
                        for c in calls
                    ]
                msgs.append(msg)
        return msgs

    def generate(self, system: str, history: list[dict], tools: list[dict]) -> Reply:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=self._to_messages(system, history),
            tools=[{"type": "function", "function": t} for t in tools],
        )
        if not resp.choices:
            raise RuntimeError("The model returned no choices.")
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(name=tc.function.name, args=args, id=tc.id))
        return Reply(text=msg.content or "", tool_calls=calls, provider=self.name)
