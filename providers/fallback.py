"""Try providers in order; retry transient errors, then fail over to the next.

Error handling per provider:
  - transient (503, timeout, ...): fail over at once if another provider is left;
                                   the last provider is retried with backoff
                                   (a failed provider is skipped for 60s, doubling
                                   on repeat failures, up to 10 min)
  - rate limited (429):            fail over immediately
  - misconfigured (401/403/404):   disabled for the rest of the session, fail over
  - anything else (e.g. 400):      no retry, fail over (another model may cope)
If every provider fails, the last error is raised.
"""
import time

from .base import Reply, _status, is_provider_fatal, is_rate_limited, is_transient


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
        cooldown: float = 60.0,   # skip a provider that just failed for this long...
        max_cooldown: float = 600.0,  # ...doubling on each repeat failure, up to this
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
        self.max_cooldown = max_cooldown
        self._fails: dict[str, int] = {}  # consecutive failures per provider
        self._notify = notify
        self._sleep = sleep
        self._clock = clock
        self._down_until: dict[str, float] = {}
        self._disabled: dict[str, str] = {}  # name -> reason (bad key/model)
        self._last_used: str | None = None

    def _order(self) -> list:
        """Healthy providers first (in configured order), cooling-down ones last.
        Providers disabled for a bad key/model are left out entirely."""
        now = self._clock()
        alive = [p for p in self.providers if p.name not in self._disabled]
        ready = [p for p in alive if self._down_until.get(p.name, 0) <= now]
        cooling = [p for p in alive if p not in ready]
        return ready + cooling

    def generate(self, system: str, history: list[dict], tools: list[dict]) -> Reply:
        order = self._order()
        if not order:
            reasons = "; ".join(f"{n}: {r}" for n, r in self._disabled.items())
            raise RuntimeError(f"No usable providers left. Fix your .env and restart. ({reasons})")
        last_exc: Exception | None = None
        for i, provider in enumerate(order):
            # Only retry (with backoff) when nothing else is left to try. With another
            # provider available, switching is faster than waiting on a struggling one.
            max_attempts = self.retries + 1 if i + 1 == len(order) else 1
            for attempt in range(max_attempts):
                try:
                    reply = provider.generate(system, history, tools)
                except Exception as exc:
                    last_exc = exc
                    if is_provider_fatal(exc):
                        self._disabled[provider.name] = _short(exc)
                        self._notify(
                            f"  [{provider.name}] disabled for this session "
                            f"({_short(exc)}) - check its key/model in .env"
                        )
                        break
                    if not is_transient(exc):
                        break  # e.g. 400: retrying is pointless, another provider may cope
                    # A rate limit will not clear in a few seconds: switch right away.
                    if is_rate_limited(exc) or attempt == max_attempts - 1:
                        break
                    delay = self.base_delay * 2**attempt
                    self._notify(f"  [{provider.name}] {_short(exc)} - retrying in {delay:g}s")
                    self._sleep(delay)
                    continue
                if self._last_used is not None and self._last_used != provider.name:
                    self._notify(f"  [switched to {provider.name}]")
                self._last_used = provider.name
                self._down_until.pop(provider.name, None)
                self._fails.pop(provider.name, None)
                return reply
            if provider.name not in self._disabled:
                n = self._fails.get(provider.name, 0) + 1
                self._fails[provider.name] = n
                wait = min(self.cooldown * 2 ** (n - 1), self.max_cooldown)
                self._down_until[provider.name] = self._clock() + wait
                if i + 1 < len(order):
                    self._notify(
                        f"  [{provider.name}] unavailable ({_short(last_exc)}), "
                        f"trying {order[i + 1].name}"
                    )
            elif i + 1 < len(order):
                self._notify(f"  trying {order[i + 1].name}")
        raise RuntimeError(f"All providers failed. Last error: {_short(last_exc)}") from last_exc
