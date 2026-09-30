"""A circuit breaker per provider, and the rule about what counts as a failure.

Three states, and the one that carries the whole design is the third:

- **closed** — calls go through. Normal operation.
- **open** — calls are refused *before any I/O*. This is the property the module
  exists for, and `tests/test_breaker.py` asserts it at the litellm seam rather than
  on the state, because a breaker that records "open" and then still dials the
  provider makes the dashboard look better and the incident exactly as bad.
- **half-open** — one probe is admitted. A half-open breaker that admits the whole
  waiting fleet is a closed breaker wearing a hat; the point is that *one* request
  finds out whether the provider came back.

Per provider rather than per service, because anthropic being down is no reason to
stop serving the requests openai can serve — that is the entire reason a fallback
chain exists, and a service-wide breaker would throw it away.

**What counts as a failure** is the decision worth arguing. The threshold is over
*transient* failures only (`muse.errors.trips_breaker`), because the breaker exists
to stop muse adding load to a provider that cannot take it. A 400 or a 401 is muse
sending something the provider is right to refuse, so counting it would let one
caller with a malformed request take the route down for everybody: a bug becomes a
denial of service. Those are recorded in the failure chain, where support will see
them, and nowhere else.

A *success* resets the count, so the threshold is over consecutive failures. A
provider that fails twice, serves four thousand requests, and fails twice more has
not had four consecutive failures, and opening its breaker would shed traffic from a
route that is working.

The clock is injected. Every duration this module cares about is then a number a
test writes down rather than a number it waits for (AGENTS.md rule 13).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

#: Consecutive transient failures before a provider is held back. Five is chosen to
#: sit above the noise of a single bad rollout and below the point at which a caller
#: notices: a request with `max_attempts: 1` and a 0.25s backoff already takes three
#: calls to exhaust, so five failures is a sustained outage rather than a blip.
DEFAULT_BREAKER_THRESHOLD = 5

#: How long a provider stays held back before one probe is admitted. Thirty seconds
#: is long enough that a provider mid-restart is not probed, and short enough that a
#: recovery is served rather than waited out.
DEFAULT_BREAKER_RESET_SECONDS = 30.0

#: Successful probes needed to close a half-open breaker. One by default: this
#: service would rather retry against a recovered provider than sit out a route whose
#: fallback may also be down. Raise it for a provider that flaps.
DEFAULT_BREAKER_SUCCESSES = 1


class BreakerState(StrEnum):
    """The three states. Named, so a span attribute and a test assertion read the
    same word rather than three spellings of one concept."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """One provider's breaker.

    Mutable and *not* shared between providers, but shared between concurrent
    requests for the same provider — which is the point. Under asyncio the
    read-modify-write in `allow()` and `record_*()` cannot interleave, because there
    is no `await` inside any of them. The same would not be true across threads, so
    this is documented as single-event-loop rather than claimed as thread-safe.
    """

    threshold: int = DEFAULT_BREAKER_THRESHOLD
    reset_seconds: float = DEFAULT_BREAKER_RESET_SECONDS
    successes_to_close: int = DEFAULT_BREAKER_SUCCESSES
    clock: Callable[[], float] = time.monotonic
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _successes: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    #: Whether a half-open probe is already in flight. Without it every waiting
    #: request would be admitted at once, which is the herd this state exists to
    #: avoid.
    _probing: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError(f"breaker threshold must be at least 1, got {self.threshold}")
        if self.reset_seconds <= 0:
            raise ValueError(f"breaker reset must be positive, got {self.reset_seconds}")
        if self.successes_to_close < 1:
            raise ValueError(
                f"breaker successes_to_close must be at least 1, got {self.successes_to_close}"
            )

    @property
    def state(self) -> BreakerState:
        return self._state

    def allow(self) -> bool:
        """Whether a call may be dispatched, and advances half-open if it is due.

        The *only* gate. The router consults it before touching the provider, so a
        `False` here is a refusal that never becomes a socket.
        """
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            if self.clock() - self._opened_at < self.reset_seconds:
                return False
            self._state = BreakerState.HALF_OPEN
            self._probing = True
            return True
        # Half-open: one probe at a time. The second caller gets `False` and fails
        # fast, which is the correct answer — the provider is either about to be
        # declared healthy or about to be held back again, and neither needs a crowd.
        return not self._probing

    def record_success(self) -> None:
        """One candidate served. Resets the consecutive-failure count."""
        if self._state is BreakerState.HALF_OPEN:
            self._successes += 1
            if self._successes >= self.successes_to_close:
                self._close()
            return
        self._failures = 0

    def record_failure(self) -> None:
        """One transient failure against a provider.

        A failure in half-open re-opens rather than counting toward the threshold:
        the provider was asked once and said no, which is all the information a
        probe is supposed to produce. The reset clock restarts too, so the next probe
        is a full `reset_seconds` away — a breaker that re-probed immediately would
        be polling a provider that has just told it twice that it is down.
        """
        if self._state is BreakerState.HALF_OPEN:
            self._open()
            return
        self._failures += 1
        if self._failures >= self.threshold:
            self._open()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self.clock()
        self._successes = 0
        self._probing = False

    def _close(self) -> None:
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._successes = 0
        self._probing = False


class BreakerRegistry:
    """A breaker per provider name, created on first sight.

    A dict of `name -> CircuitBreaker` rather than a dict of state, so a new provider
    costs one object and a lookup returns the same breaker every time — a registry
    that minted a fresh breaker per lookup would never open anything, which is the
    quietest possible failure of this component.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_BREAKER_THRESHOLD,
        reset_seconds: float = DEFAULT_BREAKER_RESET_SECONDS,
        successes_to_close: int = DEFAULT_BREAKER_SUCCESSES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._factory = lambda: CircuitBreaker(
            threshold=threshold,
            reset_seconds=reset_seconds,
            successes_to_close=successes_to_close,
            clock=clock,
        )
        self._breakers: dict[str, CircuitBreaker] = {}

    def for_provider(self, name: str) -> CircuitBreaker:
        breaker = self._breakers.get(name)
        if breaker is None:
            breaker = self._factory()
            self._breakers[name] = breaker
        return breaker

    def state(self, name: str) -> str:
        """The state of `name` as a plain string, for a span attribute or a test.

        An unknown provider reads as `closed`, which is what it is: a vendor muse has
        never called has never failed.
        """
        return self._breakers[name].state if name in self._breakers else BreakerState.CLOSED

    def __repr__(self) -> str:
        return f"{type(self).__name__}({ {n: str(b.state) for n, b in self._breakers.items()} })"
