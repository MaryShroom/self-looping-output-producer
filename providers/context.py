"""Keep the conversation small enough for a provider's context window.

`fit(history, budget)` returns a copy of the history that is at most `budget` characters
(a rough stand-in for tokens: about 4 characters per token), plus a stats dict. The stored
history is never changed, so a provider with a bigger window still gets the full conversation
after a failover.

What gets cut, in this order, stopping as soon as it fits:
  1. tool output of OLDER turns is shortened (the model has already acted on it)
  2. the oldest turns are dropped, whole (a turn = one user message and everything the model
     did in answer to it), so a tool result never loses its tool call
  3. tool output of the CURRENT turn is shortened, oldest first
The user's current message is never shortened (when older turns are dropped, a one-line note
is put in front of the first message that remains).
"""
import json

KEEP_OLD = 400          # characters of an old tool result that are kept
KEEP_MIN = 200          # last resort, for the current turn
DROP_NOTE = "[Earlier messages were dropped to fit the context window.]\n"
MIN_BUDGET = 2000       # never ask for less than this, whatever the arithmetic says


def size(msg: dict) -> int:
    n = 40 + len(msg.get("text") or "") + len(msg.get("result") or "")
    for call in msg.get("tool_calls") or []:
        n += 20 + len(call.get("name", "")) + len(json.dumps(call.get("args", {}), default=str))
    return n


def total(history: list[dict]) -> int:
    return sum(size(m) for m in history)


def _turns(msgs: list[dict]) -> list[tuple[int, int]]:
    """(start, end) index pairs; every turn starts at a user message."""
    starts = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    if not starts:
        return [(0, len(msgs))] if msgs else []
    starts[0] = 0                         # anything before the first user message joins turn 1
    return [(s, starts[k + 1] if k + 1 < len(starts) else len(msgs)) for k, s in enumerate(starts)]


def _clip(msg: dict, keep: int) -> dict:
    """A copy of a tool message with its result shortened (or the same object if it is short)."""
    result = msg.get("result") or ""
    if len(result) <= keep + 80:
        return msg
    note = f"\n[output trimmed: {len(result) - keep} more characters]"
    return {**msg, "result": result[:keep] + note}


def fit(history: list[dict], budget) -> tuple[list[dict], dict]:
    stats = {"shrunk": 0, "dropped": 0}
    if not budget or total(history) <= budget:
        return history, stats
    budget = max(int(budget), MIN_BUDGET)
    msgs = list(history)                  # replace items below; never modify the originals
    cur = total(msgs)
    turns = _turns(msgs)

    def shrink(indices, keep):
        nonlocal cur
        for i in indices:
            if cur <= budget:
                return
            if msgs[i].get("role") != "tool":
                continue
            clipped = _clip(msgs[i], keep)
            if clipped is not msgs[i]:
                cur -= size(msgs[i]) - size(clipped)
                msgs[i] = clipped
                stats["shrunk"] += 1

    older, last = turns[:-1], turns[-1]
    # 1. shorten tool output in older turns, oldest first
    shrink([i for s, e in older for i in range(s, e)], KEEP_OLD)

    # 2. drop the oldest turns, whole
    drop_to = 0                           # index of the first message that is kept
    for s, e in older:
        if cur <= budget:
            break
        if drop_to == 0:
            cur += len(DROP_NOTE)
        cur -= sum(size(m) for m in msgs[s:e])
        drop_to = e
    if drop_to:
        stats["dropped"] = drop_to
        msgs = msgs[drop_to:]
        first = msgs[0]
        msgs[0] = {**first, "text": DROP_NOTE + (first.get("text") or "")}
        last = (last[0] - drop_to, last[1] - drop_to)
    cur = total(msgs)

    # 3. still too big: the current turn's own tool output, oldest first, newest last
    mine = [i for i in range(*last) if msgs[i].get("role") == "tool"]
    shrink(mine[:-1], KEEP_OLD)
    shrink(mine, KEEP_OLD)
    shrink(mine, KEEP_MIN)
    return msgs, stats
