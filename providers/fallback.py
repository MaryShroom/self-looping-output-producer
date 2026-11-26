"""Try providers in order; retry transient errors, then fail over to the next."""
import time

from .base import Reply, _status, is_rate_limited, is_transient


def _short(exc: Exception) -> str:
    """One-line description: type, status code if any, and the start of the message."""
    lines = str(exc).splitlines() or [""]
    code = _status(exc)
    head = f"{type(exc).__name__}" + (f" {code}" if code else "")
    return f"{head}: {lines[0][:160]}"


class FallbackProvider:
    name = "fallback"

    def __init__(
        self,
        providers,
        retries: int = 2,        # extra attempts per provider on transient errors
        base_delay: float = 1.0,  # backoff: 1s, 2s, 4s, ...
        cooldown: float = 60.0,   # skip a provider that just failed for this long
        notify=print,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self.providers = list(providers)
        self.retries = retries
        self.base_delay = base_delay
        self.cooldown = cooldown
        self._notify = notify
        self._sleep = sleep
        self._clock = clock
        self._down_until: dict[str, float] = {}
        self._last_used: str | None = None

    def _order(self) -> list:
        """Healthy providers first (in configured order), cooling-down ones last."""
        now = self._clock()
        ready = [p for p in self.providers if self._down_until.get(p.name, 0) <= now]
        cooling = [p for p in self.providers if p not in ready]
        return ready + cooling

    def generate(self, system: str, history: list[dict], tools: list[dict]) -> Reply:
        order = self._order()
        last_exc: Exception | None = None
        for i, provider in enumerate(order):
            for attempt in range(self.retries + 1):
                try:
                    reply = provider.generate(system, history, tools)
                except Exception as exc:
                    if not is_transient(exc):
                        raise  # bad key, bad request, etc.: failing over would not help
                    last_exc = exc
                    # A rate limit will not clear in a few seconds: switch right away.
                    if is_rate_limited(exc) or attempt == self.retries:
                        break
                    delay = self.base_delay * 2**attempt
                    self._notify(f"  [{provider.name}] {_short(exc)} - retrying in {delay:g}s")
                    self._sleep(delay)
                    continue
                if self._last_used is not None and self._last_used != provider.name:
                    self._notify(f"  [switched to {provider.name}]")
                self._last_used = provider.name
                self._down_until.pop(provider.name, None)
                return reply
            self._down_until[provider.name] = self._clock() + self.cooldown
            if i + 1 < len(order):
                self._notify(
                    f"  [{provider.name}] unavailable ({_short(last_exc)}), "
                    f"trying {order[i + 1].name}"
                )
        raise RuntimeError(f"All providers failed. Last error: {last_exc}") from last_exc
