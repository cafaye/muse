"""`config/routes.yaml` — the routing table and the loader that reads it.

The router's behaviour is entirely determined by this file, so a typo in it is a
production incident with no test failure. Nearly every test here is about refusing
something at load time, because a route table that cannot work should never reach
a request: an unknown provider, a route with no candidates, two routes claiming the
same model name, a file that is not YAML at all.

Two rules shape the loader:

- **An unknown key is an error, not a warning.** A misspelled `max_attemtps` that
  is silently dropped leaves a route retrying once while its author believes it
  retries three times — a reliability setting that looks configured and is not.
- **Refusals name the offending value.** "invalid config" sends someone to a file
  with forty lines. "duplicate model name: fast" is a one-line fix.

`RouteTable.validate(registry)` is the one check the loader cannot do, because
loading does not know which providers exist. It is a separate call the app factory
makes at boot, and the committed `config/routes.yaml` is run through it in the
suite — so the file that ships is the file that was tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from muse.errors import RouteConfigError
from muse.providers import ProviderRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMMITTED = REPO_ROOT / "config" / "routes.yaml"

#: The keys each mapping in the file may contain. Anything else is a load failure,
#: so a typo cannot be a silent no-op.
_TOP_LEVEL_KEYS = frozenset({"version", "defaults", "routes"})
_DEFAULT_KEYS = frozenset(
    {
        "max_attempts",
        "backoff_initial_seconds",
        "backoff_max_seconds",
        "timeout_seconds",
    }
)
_ROUTE_KEYS = _DEFAULT_KEYS | frozenset({"model", "description", "candidates"})
_CANDIDATE_KEYS = frozenset({"provider", "model", "weight"})

#: The only file format version this build understands. A file declaring a newer
#: one is refused rather than partially read, because reading half a route table is
#: how a request silently loses its fallback.
SUPPORTED_VERSION = 1

#: `weight` is accepted and stored but not used. Declared here so a later packet's
#: load balancing is a behaviour change rather than a format change — and pinned by
#: a test so it cannot be mistaken for a feature that works.
RESERVED_CANDIDATE_KEYS = frozenset({"weight"})


#: The defaults, as named constants rather than as reads of the dataclass fields.
#: A `slots=True` dataclass has no class-level attribute to read, and a constant
#: that cannot be read is a constant nobody reuses.
DEFAULT_BACKOFF_INITIAL = 0.25
DEFAULT_BACKOFF_MAXIMUM = 2.0
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_TIMEOUT_SECONDS = 30.0


#: The defaults, as named constants rather than as reads of the dataclass fields.
#: A `slots=True` dataclass has no class-level attribute to read, and a constant that
#: cannot be read is a constant nobody reuses.
DEFAULT_BACKOFF_INITIAL = 0.25
DEFAULT_BACKOFF_MAXIMUM = 2.0
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff between retries of one candidate.

    `initial` is the first wait; each subsequent wait doubles, capped at `maximum`.
    The cap is not optional: unbounded exponential backoff against a provider that
    is down for minutes is a request that hangs until the client's own timeout.
    """

    initial: float = DEFAULT_BACKOFF_INITIAL
    maximum: float = DEFAULT_BACKOFF_MAXIMUM

    def __post_init__(self) -> None:
        if self.initial < 0:
            raise ValueError(f"backoff initial must not be negative: {self.initial}")
        if self.maximum < self.initial:
            raise ValueError(
                f"backoff maximum {self.maximum} is below the initial delay {self.initial}"
            )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How hard the router tries one candidate before moving to the next.

    `max_attempts` counts the first try, so 1 means no retry. Only transient
    failures are retried — see `muse.errors.RETRYABLE_PROVIDER_ERRORS`.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {self.max_attempts}")


@dataclass(frozen=True, slots=True)
class TimeoutPolicy:
    """The per-attempt wall clock for one provider call.

    Per attempt rather than per request, so a route with retries has a total budget
    the reader can compute: `max_attempts * seconds`.
    """

    seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {self.seconds}")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One provider that may serve a route, and the model it would use.

    `model` is the *vendor's* model id, not the route's name. They usually differ —
    the route is `smart`, the candidate is `claude-sonnet-4-5` — and sending the
    route name to a vendor is a 404. `None` means "the same as the route's", which
    is the common case where the two names agree and restating it would be noise in
    every route.
    """

    provider: str
    model: str | None = None

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("a candidate must name a provider")


@dataclass(frozen=True, slots=True)
class Route:
    """One model a client can ask for, and the ordered candidates that serve it.

    Ordered, not scored: the first candidate that succeeds wins, and a failure
    advances. That is a deliberate choice over a weighted or cost-optimising
    selection, because the order is the whole thing a reader of the file can verify.
    A smarter policy is invisible in a config file, and an invisible spend decision
    is one nobody reviews.
    """

    model: str
    candidates: tuple[Candidate, ...]
    description: str = ""
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: TimeoutPolicy = field(default_factory=TimeoutPolicy)

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("a route must have a model name")
        if not self.candidates:
            raise ValueError(f"route {self.model!r} has no candidates")
        # Resolved here rather than at read time. A `Route` holding a candidate whose
        # model is `None` is a footgun: every consumer has to remember to resolve it,
        # and the one that forgets sends `None` to a provider as a model name. One
        # representation means there is nothing to forget.
        object.__setattr__(
            self,
            "candidates",
            tuple(
                candidate if candidate.model else replace(candidate, model=self.model)
                for candidate in self.candidates
            ),
        )

    @property
    def has_fallback(self) -> bool:
        """Whether a failure on the first candidate can be absorbed.

        Asked of every route in review, so it is a property rather than something a
        reader counts by eye. A single-candidate route is one vendor with extra
        syntax, and the description should say so.
        """
        return len(self.candidates) > 1


@dataclass(frozen=True, slots=True)
class RouteTable:
    """The whole file: a version, the defaults, and the routes.

    Frozen, because it is read on the request path by every concurrent request. A
    mutable table is a config change that half the fleet sees and half does not.
    """

    version: int
    defaults: RetryPolicy
    routes: tuple[Route, ...]
    timeout: TimeoutPolicy = field(default_factory=TimeoutPolicy)

    def find(self, model: str) -> Route | None:
        """The route for `model`, or `None`.

        `None` rather than a raise, so the router decides what an unrouted model
        means. It decides differently from an unknown provider, and the two are
        different kinds of mistake.
        """
        for route in self.routes:
            if route.model == model:
                return route
        return None

    def models(self) -> tuple[str, ...]:
        """Every routed model, in file order.

        File order rather than sorted: this is what a human reads in a diff, and
        sorting would hide a reordered file — which is a change to which vendor a
        customer's requests reach.
        """
        return tuple(route.model for route in self.routes)

    def providers(self) -> tuple[str, ...]:
        """Every provider name any route mentions, sorted and deduplicated.

        What `validate` checks, and what a boot error message lists.
        """
        return tuple(
            sorted({candidate.provider for route in self.routes for candidate in route.candidates})
        )

    def validate(self, registry: ProviderRegistry) -> None:
        """Check every provider this table names against `registry`.

        Separate from loading because loading does not know which providers exist —
        the file is data, the registry is the runtime. Called by the app factory at
        boot, so a route naming a vendor muse has no adapter for is a container that
        refuses to start rather than a 503 on a customer's first request.
        """
        missing = [name for name in self.providers() if name not in registry.names()]
        if missing:
            raise RouteConfigError(
                f"routes reference providers that are not registered: "
                f"{', '.join(missing)}; registered: {', '.join(registry.names()) or 'none'}"
            )


def routes_from_yaml(text: str) -> RouteTable:
    """Parse and validate a routes file.

    Every failure is a `RouteConfigError` naming the offending value. The loader
    does not validate against a registry — see `RouteTable.validate`.
    """
    document = _parse(text)
    _check_keys(document, _TOP_LEVEL_KEYS, "the top level")
    version = _require(document, "version", "the top level")
    if version != SUPPORTED_VERSION:
        raise RouteConfigError(
            f"unsupported routes version {version!r}; this build reads version {SUPPORTED_VERSION}"
        )
    if "routes" not in document:
        raise RouteConfigError("the routes file has no `routes` key")
    defaults, default_timeout = _read_defaults(document.get("defaults") or {})
    routes = _read_routes(document["routes"], defaults, default_timeout)
    return RouteTable(
        version=version,
        defaults=defaults,
        routes=routes,
        timeout=default_timeout,
    )


def _parse(text: str) -> dict[str, Any]:
    if not text.strip():
        raise RouteConfigError("the routes file is empty")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise RouteConfigError(f"the routes file is not valid YAML: {error}") from error
    if not isinstance(document, dict):
        # A list at the top level parses cleanly and means nothing here. Catching it
        # here beats a KeyError from somewhere inside the app factory.
        raise RouteConfigError(
            f"the routes file must be a mapping at the top level, got {type(document).__name__}"
        )
    return document


def _read_defaults(block: Any) -> tuple[RetryPolicy, TimeoutPolicy]:
    _check_keys(block, _DEFAULT_KEYS, "`defaults`")
    return (
        RetryPolicy(
            max_attempts=_attempts(block, DEFAULT_MAX_ATTEMPTS),
            backoff=_backoff_from(block, None),
        ),
        _timeout(block),
    )


def _backoff_from(block: dict[str, Any], inherited: BackoffPolicy | None) -> BackoffPolicy:
    """The backoff for this block: the file default, overridden field by field.

    The cross-field check lives here rather than catching the dataclass's
    `ValueError`, because only this layer knows which *key* was wrong. The dataclass
    says "maximum is below initial"; the operator needs to be told which line of the
    file to edit.
    """
    initial = _number(
        block,
        "backoff_initial_seconds",
        inherited.initial if inherited else DEFAULT_BACKOFF_INITIAL,
    )
    maximum = _number(
        block,
        "backoff_max_seconds",
        inherited.maximum if inherited else DEFAULT_BACKOFF_MAXIMUM,
    )
    if maximum < initial:
        raise RouteConfigError(
            f"`backoff_max_seconds` ({maximum}) is below `backoff_initial_seconds` "
            f"({initial}); the first retry would wait longer than the ceiling allows"
        )
    return BackoffPolicy(initial=initial, maximum=maximum)


def _timeout(block: dict[str, Any], fallback: float = DEFAULT_TIMEOUT_SECONDS) -> TimeoutPolicy:
    # `_number` has already refused a non-positive value, so the dataclass's own
    # check cannot fire here. No second check: an unreachable branch is one more
    # thing to keep true, and a coverage report pointing at it is noise.
    return TimeoutPolicy(seconds=_number(block, "timeout_seconds", fallback))


def _attempts(block: dict[str, Any], inherited: int) -> int:
    """`max_attempts` for this block, falling back to the inherited value.

    The fallback is an int the caller supplies rather than a dataclass default, so a
    route inheriting `max_attempts: 3` from `defaults` must get 3, not the
    dataclass's 1.
    """
    value = block.get("max_attempts")
    if value is None:
        return inherited
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteConfigError(f"`max_attempts` must be a whole number, got {value!r}")
    if value < 1:
        raise RouteConfigError(f"`max_attempts` must be at least 1, got {value}")
    return value


def _read_routes(
    entries: Any, defaults: RetryPolicy, default_timeout: TimeoutPolicy
) -> tuple[Route, ...]:
    if not isinstance(entries, list):
        raise RouteConfigError(f"`routes` must be a list, got {type(entries).__name__}")
    if not entries:
        raise RouteConfigError("the routes file must declare at least one route")
    routes = tuple(_read_route(entry, defaults, default_timeout) for entry in entries)
    seen: set[str] = set()
    for route in routes:
        if route.model in seen:
            # The second would silently shadow the first, and which one won would
            # depend on file order — a reordering that changes which vendor a
            # customer's requests reach, with no behavioural diff to review.
            raise RouteConfigError(
                f"duplicate model name {route.model!r}; each model may be routed once"
            )
        seen.add(route.model)
    return routes


def _read_route(entry: Any, defaults: RetryPolicy, default_timeout: TimeoutPolicy) -> Route:
    if not isinstance(entry, dict):
        raise RouteConfigError(f"each route must be a mapping, got {type(entry).__name__}")
    _check_keys(entry, _ROUTE_KEYS, "a route")
    model = entry.get("model")
    if not isinstance(model, str) or not model.strip():
        raise RouteConfigError(f"a route needs a non-empty `model` name, got {model!r}")
    return Route(
        model=model,
        candidates=_read_candidates(entry.get("candidates"), model),
        description=entry.get("description", ""),
        retry=RetryPolicy(
            max_attempts=_attempts(entry, defaults.max_attempts),
            backoff=_backoff_from(entry, defaults.backoff),
        ),
        timeout=_timeout(entry, default_timeout.seconds),
    )


def _read_candidates(entries: Any, model: str) -> tuple[Candidate, ...]:
    if not isinstance(entries, list):
        raise RouteConfigError(
            f"route {model!r}: `candidates` must be a list, got {type(entries).__name__}"
        )
    if not entries:
        raise RouteConfigError(f"route {model!r} declares no candidates, so it can never be served")
    return tuple(_read_candidate(entry, model) for entry in entries)


def _read_candidate(entry: Any, route_model: str) -> Candidate:
    if not isinstance(entry, dict):
        raise RouteConfigError(
            f"route {route_model!r}: each candidate must be a mapping, got {type(entry).__name__}"
        )
    _check_keys(entry, _CANDIDATE_KEYS, f"a candidate of route {route_model!r}")
    provider = entry.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise RouteConfigError(
            f"route {route_model!r}: a candidate must name a `provider`, got {provider!r}"
        )
    # `weight` is deliberately not read. It is accepted so a later packet can add
    # load balancing without a format change, and ignored so nothing acts on it yet
    # — a weight that is parsed and not honoured is a promise the file makes that
    # the router does not keep.
    return Candidate(provider=provider, model=entry.get("model"))


def _number(block: dict[str, Any], key: str, fallback: float) -> float:
    value = block.get(key)
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RouteConfigError(f"`{key}` must be a number, got {value!r}")
    if value <= 0:
        raise RouteConfigError(f"`{key}` must be positive, got {value!r}")
    return float(value)


def _check_keys(block: Any, allowed: frozenset[str], where: str) -> None:
    if not isinstance(block, dict):
        raise RouteConfigError(f"{where} must be a mapping, got {type(block).__name__}")
    unknown = sorted(set(block) - allowed)
    if unknown:
        # Refusing rather than ignoring. A misspelled key that is dropped is a
        # setting that looks configured and is not, which is worse than a boot
        # failure because nothing reports it.
        raise RouteConfigError(
            f"unknown key in {where}: {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}"
        )


def _require(block: dict[str, Any], key: str, where: str) -> Any:
    if key not in block:
        raise RouteConfigError(f"{where} is missing the required key `{key}`")
    return block[key]
