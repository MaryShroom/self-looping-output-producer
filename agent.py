"""The agent loop: model -> tools -> model ... until the model answers without a tool call."""
import time

from tools import TOOL_SPECS, FailureStreak, format_preview, run_tool

MAX_STEPS = 15      # safety cap on tool round-trips per user message
MAX_ARG_CHARS = 60  # long argument values (file contents) are shortened when printed


def _short_args(args) -> dict:
    if not isinstance(args, dict):
        return {"arguments": args}
    return {
        k: (v[:MAX_ARG_CHARS] + "…" if isinstance(v, str) and len(v) > MAX_ARG_CHARS else v)
        for k, v in args.items()
    }


def _stop(history: list[dict], say, message: str) -> None:
    """End the turn cleanly. The assistant note keeps the history well-formed: it must not
    end on tool results, or the next user message would break some providers."""
    say(f"({message})")
    history.append(
        {"role": "assistant", "text": f"(I stopped: {message}.)", "tool_calls": [], "raw": {}}
    )


def run_turn(provider, history: list[dict], system: str,
             tool_specs=TOOL_SPECS, run=run_tool, say=print) -> None:
    """One user message -> model, run any requested tools, repeat until done."""
    streak = FailureStreak()
    for _ in range(MAX_STEPS):
        started = time.monotonic()
        reply = provider.generate(system, history, tool_specs)
        say(f"  ({reply.provider}, {time.monotonic() - started:.1f}s)")
        history.append(reply.to_message())
        if reply.text:
            say(f"\n{reply.text}\n")
        if not reply.tool_calls:
            return
        too_many_errors = False
        for call in reply.tool_calls:  # every call gets a result, even when we stop after
            say(f"  -> {call.name}({_short_args(call.args)})")
            result = run(call.name, call.args)
            preview = format_preview(result)
            if preview:
                say(preview)
            history.append(
                {"role": "tool", "id": call.id, "name": call.name, "result": result}
            )
            too_many_errors = streak.record(result) or too_many_errors
        if too_many_errors:
            _stop(history, say, f"{streak.limit} tool errors in a row")
            return
    _stop(history, say, "too many tool steps")
