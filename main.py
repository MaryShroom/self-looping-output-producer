"""Minimal coding assistant: terminal chat + tool loop, with provider failover."""
import os
import time
from pathlib import Path

from providers import build_provider
from tools import TOOL_SPECS, run_tool

SYSTEM = (
    "You are a coding assistant running in the user's terminal. "
    "Use the available tools only when the request needs information from the user's project; "
    "answer greetings and general questions directly. Be concise and accurate."
)
MAX_STEPS = 15  # safety cap on tool round-trips per user message


def load_env(path: Path) -> None:
    """Tiny .env loader (KEY=value lines). Real environment variables win over the file."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def run_turn(provider, history: list[dict]) -> None:
    """One user message -> model, run any requested tools, repeat until done."""
    for _ in range(MAX_STEPS):
        started = time.monotonic()
        reply = provider.generate(SYSTEM, history, TOOL_SPECS)
        print(f"  ({reply.provider}, {time.monotonic() - started:.1f}s)")
        history.append(reply.to_message())
        if reply.text:
            print(f"\n{reply.text}\n")
        if not reply.tool_calls:
            return
        for call in reply.tool_calls:
            print(f"  -> {call.name}({call.args})")
            history.append(
                {
                    "role": "tool",
                    "id": call.id,
                    "name": call.name,
                    "result": run_tool(call.name, call.args),
                }
            )
    print("(stopped: too many tool steps)")


def main() -> None:
    # Look for .env next to this script first, then in the folder you launched from.
    load_env(Path(__file__).resolve().parent / ".env")
    load_env(Path.cwd() / ".env")

    provider = build_provider()
    history: list[dict] = []
    names = " -> ".join(p.name for p in provider.providers)
    print(f"Coding assistant ready (providers: {names}). Type a request, or 'exit' to quit.")
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
