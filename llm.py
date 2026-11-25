"""Provider layer.

The rest of the app only deals with ToolCall / Reply and plain-dict messages.
To support another provider later, write a class with the same `generate`
method (e.g. AnthropicProvider, OpenAICompatProvider for Ollama and friends).
"""
import os
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import types


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Provider-specific message object. We keep it and send it back unchanged,
    # because some Gemini models attach extra data (thought signatures) to
    # function calls that must be returned as-is.
    raw: Any = None


class GeminiProvider:
    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "models/gemini-3.8-flash")
        self.client = genai.Client()  # reads GEMINI_API_KEY from the environment

    def _to_contents(self, history: list[dict]) -> list:
        """Convert our neutral history into Gemini's message format."""
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
            else:  # assistant
                contents.append(m["raw"])
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
        return Reply(text=text, tool_calls=calls, raw=content)
