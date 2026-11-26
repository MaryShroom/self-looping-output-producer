"""Gemini adapter (google-genai SDK)."""
import os

from google import genai
from google.genai import types

from .base import Reply, ToolCall


class GeminiProvider:
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "models/gemini-3.8-flash")
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()

    def _to_contents(self, history: list[dict]) -> list:
        """Convert neutral history into Gemini messages."""
        contents = []
        pending = []  # consecutive tool results are sent as one message

        def flush():
            nonlocal pending
            if pending:
                contents.append(types.Content(role="user", parts=pending))
                pending = []

        for m in history:
            if m["role"] == "tool":
                pending.append(
                    types.Part.from_function_response(
                        name=m["name"], response={"result": m["result"]}
                    )
                )
                continue
            flush()
            if m["role"] == "user":
                contents.append(
                    types.Content(role="user", parts=[types.Part.from_text(text=m["text"])])
                )
                continue
            # assistant: reuse Gemini's own message if it wrote this turn,
            # otherwise rebuild it from the neutral fields.
            raw = (m.get("raw") or {}).get(self.name)
            if raw is not None:
                contents.append(raw)
                continue
            parts = []
            if m.get("text"):
                parts.append(types.Part.from_text(text=m["text"]))
            for c in m.get("tool_calls") or []:
                parts.append(types.Part.from_function_call(name=c["name"], args=c["args"]))
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        flush()
        return contents

    def generate(self, system: str, history: list[dict], tools: list[dict]) -> Reply:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[types.FunctionDeclaration(**t) for t in tools]
                )
            ],
            # We run the tool loop ourselves instead of letting the SDK do it.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        # API errors propagate; FallbackProvider classifies them by status code.
        resp = self.client.models.generate_content(
            model=self.model,
            contents=self._to_contents(history),
            config=config,
        )
        if not resp.candidates:
            raise RuntimeError("The model returned no candidates (possibly blocked).")
        content = resp.candidates[0].content
        parts = content.parts or []
        text = "".join(p.text for p in parts if p.text and not p.thought)
        calls = [ToolCall(fc.name, dict(fc.args or {})) for fc in (resp.function_calls or [])]
        return Reply(text=text, tool_calls=calls, provider=self.name, raw=content)
