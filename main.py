"""Minimal coding assistant: terminal chat + tool loop."""
from llm import GeminiProvider
from tools import TOOL_SPECS, run_tool

SYSTEM = (
    "You are Self-Looping Output Producer, SLOP for short, a coding assistant running in the user's terminal. "
    "Use the available tools to look at the user's project before answering "
    "questions about it. Be concise and accurate."
)
MAX_STEPS = 15  # safety cap on tool round-trips per user message


def run_turn(provider, history: list[dict]) -> None:
    """One user message -> model, run any requested tools, repeat until done."""
    for _ in range(MAX_STEPS):
        reply = provider.generate(SYSTEM, history, TOOL_SPECS)
        history.append({"role": "assistant", "raw": reply.raw})
        if reply.text:
            print(f"\n{reply.text}\n")
        if not reply.tool_calls:
            return
        for call in reply.tool_calls:
            print(f"  -> {call.name}({call.args})")
            history.append(
                {"role": "tool", "name": call.name, "result": run_tool(call.name, call.args)}
            )
    print("(stopped: too many tool steps)")


def main() -> None:
    provider = GeminiProvider()
    history: list[dict] = []
    print("Coding assistant ready. Type a request, or 'exit' to quit.")
    while True:
        try:
            user = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user in ("exit", "quit"):
            break
        if not user:
            continue
        history.append({"role": "user", "text": user})
        try:
            run_turn(provider, history)
        except Exception as e:
            print(f"error: {e}")


if __name__ == "__main__":
    main()
