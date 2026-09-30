"""Configuration for the resilience layer: breakers, budgets, jitter, and tracing.

Every knob this packet added is settable and every one has a safe default, so the
tests here are mostly about the *fallback* rather than the value: what happens when
the value is missing, when it is nonsense, and when it is out of range.

The rule being pinned is the packet's: **missing configuration degrades to a
documented default; it never stops the service from starting.** That is a deliberate
choice against the house pattern elsewhere in this repo — `MUSE_VAULT_KEY` missing
*is* a boot failure, and rightly so, because a vault with a fallback key is a vault
whose keys are readable. The difference is that a resilience knob's default is safe:
a breaker threshold that falls back to 5 sheds a little load during an incident,
whereas a vault key that falls back to zero gives away every credential. A setting
whose default is dangerous refuses to boot; a setting whose default is merely
conservative does not.
"""

from __future__ import annotations

import pytest

from muse.breaker import (
    DEFAULT_BREAKER_RESET_SECONDS,
    DEFAULT_BREAKER_SUCCESSES,
    DEFAULT_BREAKER_THRESHOLD,
    BreakerRegistry,
    CircuitBreaker,
)
from muse.errors import ConfigError, RouteConfigError
from muse.main import Settings
from muse.router import Router
from muse.routes import (
    DEFAULT_BACKOFF_JITTER,
    DEFAULT_BUDGET_SECONDS,
    BackoffPolicy,
    Candidate,
    RetryPolicy,
    RouteTable,
    routes_from_yaml,
)
from muse.telemetry import Telemetry, build_provider

from .conftest import write_routes
from .test_breaker import Clock
from .test_router import PRIMARY, registry_with, table

pytestmark = pytest.mark.unit


# --- the breaker's own validation -----------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"threshold": 0}, "threshold must be at least 1", id="threshold-zero"),
        pytest.param({"reset_seconds": 0.0}, "reset must be positive", id="reset-zero"),
        pytest.param({"reset_seconds": -1.0}, "reset must be positive", id="reset-negative"),
        pytest.param(
            {"successes_to_close": 0}, "successes_to_close must be at least 1", id="successes-zero"
        ),
    ],
)
def test_a_breaker_refuses_a_nonsensical_threshold(kwargs, message: str) -> None:
    """A breaker that never opens, or that opens on the first call, is worse than
    none — so the constructor refuses rather than guessing."""
    with pytest.raises(ValueError, match=message):
        CircuitBreaker(**kwargs)


def test_a_provider_muse_has_never_called_reads_as_closed() -> None:
    """`state()` on an unknown name is an answer, not a `KeyError`.

    A readiness body or an admin surface asking about a vendor that has not been
    called yet should see `closed` — which is what it is: never called, never failed.
    """
    assert BreakerRegistry().state("never-seen") == "closed"


def test_the_registry_repr_names_every_provider_it_is_holding() -> None:
    """A `repr` that omits the states would be a repr that hides the only interesting
    thing about a breaker registry, which is which providers are currently held
    back."""
    clock = Clock()
    registry = BreakerRegistry(threshold=1, clock=clock)
    registry.for_provider("openai").record_failure()
    registry.for_provider("anthropic")

    text = repr(registry)

    assert text.startswith("BreakerRegistry(")
    assert "'openai': 'open'" in text
    assert "'anthropic': 'closed'" in text


def test_the_router_exposes_its_breakers() -> None:
    """A property rather than an attribute so a future readiness body cannot
    accidentally reach past the registry and mutate a breaker's internals."""
    router = Router(registry_with(), table(Candidate(PRIMARY)))

    assert isinstance(router.breakers, BreakerRegistry)


def test_a_clock_a_test_moves_by_hand() -> None:
    """The injectable clock, used directly.

    The point of `clock` being a collaborator rather than a call to `time.monotonic`
    is that this class exists at all: every duration the breaker and the budget care
    about is then a number a test writes down instead of a number it waits for
    (AGENTS.md rule 13).
    """

    class Hand:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    hand = Hand()
    breaker = CircuitBreaker(threshold=1, reset_seconds=10.0, clock=hand)
    breaker.record_failure()

    hand.now = 10.0

    assert breaker.allow() is True


# --- breaker configuration from the environment ---------------------------


def test_the_breaker_defaults_are_production_safe() -> None:
    """Named constants, so a reader of `build_container` can look up what "no
    configuration" means rather than inferring it from a literal."""
    settings = Settings.from_env({})

    assert settings.breaker_threshold == DEFAULT_BREAKER_THRESHOLD == 5
    assert settings.breaker_reset_seconds == DEFAULT_BREAKER_RESET_SECONDS == 30.0
    assert DEFAULT_BREAKER_SUCCESSES == 1


def test_the_breaker_is_configurable_from_the_environment() -> None:
    settings = Settings.from_env(
        {"MUSE_BREAKER_THRESHOLD": "9", "MUSE_BREAKER_RESET_SECONDS": "2.5"}
    )

    assert settings.breaker_threshold == 9
    assert settings.breaker_reset_seconds == 2.5


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="absent"),
        pytest.param({"MUSE_BREAKER_THRESHOLD": "not-a-number"}, id="not-a-number"),
        pytest.param({"MUSE_BREAKER_THRESHOLD": "0"}, id="zero"),
        pytest.param({"MUSE_BREAKER_THRESHOLD": "-3"}, id="negative"),
        pytest.param({"MUSE_BREAKER_RESET_SECONDS": ""}, id="empty"),
        pytest.param({"MUSE_BREAKER_RESET_SECONDS": "0"}, id="reset-zero"),
        pytest.param({"MUSE_BREAKER_RESET_SECONDS": "nope"}, id="reset-nonsense"),
    ],
)
def test_unusable_breaker_configuration_falls_back_rather_than_crashing(environ) -> None:
    """The packet's rule: missing config means sane defaults, never a crash.

    A breaker threshold of `0` would open on the first call and shed all traffic; a
    threshold of "nine" would leave a broken provider serving for nine failures. Both
    are worse than the documented default of five, so both fall back.
    """
    settings = Settings.from_env(environ)

    assert settings.breaker_threshold == DEFAULT_BREAKER_THRESHOLD
    assert settings.breaker_reset_seconds == DEFAULT_BREAKER_RESET_SECONDS


def test_the_tracing_endpoint_is_read_from_the_standard_variable() -> None:
    """`MUSE_OTEL_EXPORTER_OTLP_ENDPOINT` is the name the OpenTelemetry SDK defines,
    so the standard `OTEL_*` tooling configures muse with no muse-specific variable.
    """
    assert Settings.from_env({}).otel_endpoint is None
    assert Settings.from_env({"MUSE_OTEL_EXPORTER_OTLP_ENDPOINT": ""}).otel_endpoint is None
    assert (
        Settings.from_env(
            {"MUSE_OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}
        ).otel_endpoint
        == "http://collector:4318"
    )
    assert Settings.from_env({"OTEL_SERVICE_NAME": "muse-canary"}).otel_service_name == (
        "muse-canary"
    )


# --- the routes file: budget, jitter, and the indeterminate opt-in ----------


def test_the_committed_routes_file_states_the_new_keys() -> None:
    """The file that ships is the file that was tested, per `muse.routes`' own rule.

    Asserted on the *parsed* file rather than on its text, so this is a statement
    about the routing table muse will actually run rather than about how it is
    spelled. The committed table deliberately leaves `retry_indeterminate` off — the
    key appears only in a comment explaining why — so the parse is what proves the
    decision was made rather than merely discussed.
    """
    from .conftest import ROUTES_YAML

    committed = routes_from_yaml(ROUTES_YAML.read_text(encoding="utf-8"))

    assert committed.defaults.budget_seconds == DEFAULT_BUDGET_SECONDS
    assert committed.defaults.backoff.jitter == DEFAULT_BACKOFF_JITTER
    assert committed.defaults.retry_indeterminate is False
    for route in committed.routes:
        assert route.retry.retry_indeterminate is False, (
            f"route {route.model!r} opted into retrying an ambiguous outcome"
        )


def test_a_route_inherits_the_budget_and_the_opt_in_from_the_defaults() -> None:
    document = write_routes(
        {
            "version": 1,
            "defaults": {
                "max_attempts": 3,
                "budget_seconds": 12.5,
                "retry_indeterminate": True,
                "backoff_jitter": 0.25,
            },
            "routes": [{"model": "fast", "candidates": [{"provider": "openai"}]}],
        }
    )

    route = routes_from_yaml(document).routes[0]

    assert route.retry.budget_seconds == 12.5
    assert route.retry.retry_indeterminate is True
    assert route.retry.backoff.jitter == 0.25


def test_a_route_overrides_only_what_it_names() -> None:
    """Field-by-field inheritance, so `budget_seconds: 5.0` on a route does not
    silently reset the jitter it inherited."""
    document = write_routes(
        {
            "version": 1,
            "defaults": {"backoff_jitter": 0.25, "retry_indeterminate": True},
            "routes": [
                {
                    "model": "fast",
                    "candidates": [{"provider": "openai"}],
                    "budget_seconds": 5.0,
                    "retry_indeterminate": False,
                }
            ],
        }
    )

    route = routes_from_yaml(document).routes[0]

    assert route.retry.budget_seconds == 5.0
    assert route.retry.retry_indeterminate is False
    assert route.retry.backoff.jitter == 0.25


def test_the_defaults_budget_is_bounded_and_the_jitter_is_in_range() -> None:
    policy = RetryPolicy()

    assert policy.budget_seconds == DEFAULT_BUDGET_SECONDS
    assert 0.0 < DEFAULT_BACKOFF_JITTER <= 1.0


@pytest.mark.parametrize(
    ("block", "message"),
    [
        pytest.param({"budget_seconds": 0}, "must be positive", id="budget-zero"),
        pytest.param({"budget_seconds": "soon"}, "must be a number", id="budget-string"),
        pytest.param({"budget_seconds": True}, "must be a number", id="budget-bool"),
        pytest.param({"backoff_jitter": 1.5}, "backoff_jitter", id="jitter-too-high"),
        pytest.param({"backoff_jitter": -0.5}, "backoff_jitter", id="jitter-negative"),
        pytest.param({"backoff_jitter": "lots"}, "must be a number", id="jitter-string"),
        pytest.param({"backoff_jitter": True}, "must be a number", id="jitter-bool"),
        pytest.param({"retry_indeterminate": "false"}, "must be true or false", id="flag-string"),
        pytest.param({"retry_indeterminate": 1}, "must be true or false", id="flag-int"),
    ],
)
def test_an_unusable_resilience_key_is_a_load_error(block, message: str) -> None:
    """Refused at load, not coerced.

    The `retry_indeterminate: "false"` case is the one that matters most: a quoted
    string is truthy, so coercing it would switch on the single setting that can bill
    a customer twice, and the file would still read as though it said `false`.
    """
    document = write_routes(
        {
            "version": 1,
            "defaults": block,
            "routes": [{"model": "f", "candidates": [{"provider": "openai"}]}],
        }
    )

    with pytest.raises(RouteConfigError, match=message):
        routes_from_yaml(document)


def test_an_unknown_resilience_key_is_still_a_load_error() -> None:
    """`max_attemtps` with three t's must not be dropped.

    Same rule as every other key in the file: a misspelled setting that is silently
    ignored is a setting that looks configured and is not, which is worse than a boot
    failure because nothing reports it.
    """
    document = write_routes(
        {
            "version": 1,
            "defaults": {"budget_second": 5.0},
            "routes": [{"model": "f", "candidates": [{"provider": "openai"}]}],
        }
    )

    with pytest.raises(RouteConfigError, match="unknown key"):
        routes_from_yaml(document)


def test_a_backoff_policy_can_be_built_by_hand_with_no_jitter() -> None:
    """The dataclass defaults are the production ones; jitter 0 is the escape hatch
    for a schedule that must be reproducible."""
    assert BackoffPolicy(jitter=0.0).jitter == 0.0
    assert BackoffPolicy().jitter == DEFAULT_BACKOFF_JITTER


# --- the tracing provider -------------------------------------------------


def test_no_endpoint_means_a_provider_that_exports_nowhere() -> None:
    """The default, and the case the whole test suite runs in.

    Asserted by the *absence* of processors rather than by inspecting the object: a
    provider with no span processor is what "nothing leaves the process" means, and
    that is the property that keeps the suite hermetic (AGENTS.md rule 3).
    """
    provider = build_provider()

    assert provider._active_span_processor._span_processors == ()


def test_a_configured_endpoint_gets_a_batch_processor() -> None:
    """Only when an endpoint is configured does anything get exported.

    Constructed but never started, so no socket is opened: the assertion is that the
    wiring exists, not that muse can reach a collector.
    """
    provider = build_provider(endpoint="http://localhost:4318", service_name="muse-test")

    assert len(provider._active_span_processor._span_processors) == 1
    assert provider.resource.attributes["service.name"] == "muse-test"
    assert provider.resource.attributes["service.namespace"] == "cafaye"


def test_a_missing_optional_extra_is_a_boot_error_naming_both_fixes() -> None:
    """The OTLP exporter is an optional extra, so "endpoint set, extra absent" is a
    real deployment state.

    It has to be a `ConfigError` and not silence: an operator who configured a
    collector and did not get traces would otherwise have no signal that the
    configuration was wrong, and would conclude the traces were simply not being
    generated.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("opentelemetry.exporter.otlp"):
            raise ImportError("No module named 'opentelemetry.exporter.otlp'")
        return real_import(name, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(builtins, "__import__", refuse)
        with pytest.raises(ConfigError, match="muse\\[otel\\]"):
            build_provider(endpoint="http://collector:4318")


def test_a_table_with_no_resilience_keys_uses_every_default() -> None:
    """A file written before this packet still loads, with the safe defaults.

    `SUPPORTED_VERSION` is unchanged, so a routes file from muse-02 is not a
    breaking change — which only holds if every new key is optional.
    """
    table_from_yaml = routes_from_yaml(
        write_routes(
            {"version": 1, "routes": [{"model": "f", "candidates": [{"provider": "openai"}]}]}
        )
    )

    assert table_from_yaml.routes[0].retry == RetryPolicy(
        max_attempts=1, backoff=BackoffPolicy(), budget_seconds=DEFAULT_BUDGET_SECONDS
    )


def test_a_table_reports_its_routes_and_providers_unchanged() -> None:
    """Guard against the loader regressing while the new keys were added."""
    parsed: RouteTable = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "routes": [
                    {"model": "fast", "candidates": [{"provider": "openai"}]},
                    {"model": "smart", "candidates": [{"provider": "anthropic"}]},
                ],
            }
        )
    )

    assert parsed.models() == ("fast", "smart")
    assert parsed.providers() == ("anthropic", "openai")
    assert Telemetry.noop() is not None
