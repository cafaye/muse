"""`config/routes.yaml` — the routing table, and the loader that reads it.

The router's behaviour is entirely determined by this file, so a typo in it is a
production incident with no test failure. Every test here is about refusing
something at load time, because a route table that cannot work should never reach
a request: an unknown provider, a route with no candidates, two routes claiming
the same model name, a file that is not YAML at all.

`RouteTable.validate(registry)` is the one check that needs a live registry, so it
cannot happen at load — the loader has no idea which providers exist. It is a
separate call the app factory makes at boot, and the committed `config/routes.yaml`
is run through it in the suite so the file that ships is the file that was tested.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from muse.errors import RouteConfigError
from muse.providers import ProviderRegistry
from muse.providers.fake import FakeProvider
from muse.routes import (
    BackoffPolicy,
    Candidate,
    RetryPolicy,
    Route,
    RouteTable,
    TimeoutPolicy,
    routes_from_yaml,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITTED = REPO_ROOT / "config" / "routes.yaml"


def load(document: dict) -> RouteTable:
    return routes_from_yaml(yaml.safe_dump(document))


def minimal(**overrides) -> dict:
    """The smallest valid document, with fields overridden per test."""
    return {
        "version": 1,
        "routes": [{"model": "fast", "candidates": [{"provider": "openai"}]}],
        **overrides,
    }


# --- loading the committed file --------------------------------------------


def test_the_committed_file_exists() -> None:
    """A missing example is a missing documentation of how routing works, and the
    README points at it."""
    assert COMMITTED.is_file()


def test_the_committed_file_is_valid_yaml() -> None:
    assert yaml.safe_load(COMMITTED.read_text())["version"] == 1


def test_the_committed_file_is_explained_in_its_own_comments() -> None:
    """Every field the loader reads is named in the file. A field that exists in the
    loader but not in the example is a field nobody knows they can set."""
    text = COMMITTED.read_text()
    for field in (
        "version",
        "routes",
        "candidates",
        "provider",
        "max_attempts",
        "timeout_seconds",
        "backoff_initial_seconds",
        "backoff_max_seconds",
    ):
        assert field in text, f"config/routes.yaml never mentions {field}"


# --- the shape -------------------------------------------------------------


def test_a_route_knows_its_model_and_its_candidates() -> None:
    route = load(minimal()).routes[0]
    assert route.model == "fast"
    assert route.candidates == (Candidate("openai", "fast"),)


def test_models_are_listed_in_file_order() -> None:
    """Order is for humans reading a diff, not for resolution — resolution is a
    lookup. Sorted would be tidier and would hide a reordered file, which is a
    behaviour change to spend."""
    table = load(
        {
            "version": 1,
            "routes": [
                {"model": "smart", "candidates": [{"provider": "openai"}]},
                {"model": "fast", "candidates": [{"provider": "openai"}]},
            ],
        }
    )
    assert table.models() == ("smart", "fast")


def test_a_route_knows_whether_it_has_a_fallback() -> None:
    """Stated as a property because it is the question a reviewer asks of every
    route: is this one vendor pretending to be a chain?"""
    assert load(minimal()).routes[0].has_fallback is False
    two = load(
        {
            "version": 1,
            "routes": [
                {
                    "model": "f",
                    "candidates": [{"provider": "openai"}, {"provider": "anthropic"}],
                }
            ],
        }
    )
    assert two.routes[0].has_fallback is True


def test_a_table_finds_a_route_by_model() -> None:
    assert load(minimal()).find("fast").model == "fast"


def test_finding_an_absent_model_returns_none() -> None:
    """`None` rather than raising, so the router decides what an unrouted model means
    — and it decides differently from an unknown provider."""
    assert load(minimal()).find("nope") is None


# --- policy defaults and inheritance ---------------------------------------


def test_the_default_retry_policy_does_not_retry() -> None:
    """One attempt. A retry that has not been measured against a real incident is
    latency added to every failure, and the conservative default means a new route
    has to opt in to being slow."""
    assert RetryPolicy().max_attempts == 1


def test_the_default_backoff_is_short_and_capped() -> None:
    assert BackoffPolicy() == BackoffPolicy(initial=0.25, maximum=2.0)


def test_the_default_timeout_is_bounded() -> None:
    assert TimeoutPolicy().seconds == 30.0


def test_defaults_apply_to_a_route_that_states_nothing() -> None:
    route = load(minimal(defaults={"max_attempts": 3})).routes[0]
    assert route.retry == RetryPolicy(max_attempts=3)


def test_a_route_state_overrides_the_file_default() -> None:
    table = load(
        {
            "version": 1,
            "defaults": {"max_attempts": 1, "timeout_seconds": 30.0},
            "routes": [
                {
                    "model": "f",
                    "candidates": [{"provider": "openai"}],
                    "max_attempts": 4,
                    "timeout_seconds": 120.0,
                }
            ],
        }
    )
    assert table.routes[0].retry.max_attempts == 4
    assert table.routes[0].timeout.seconds == 120.0


def test_a_route_inherits_the_parts_it_does_not_override() -> None:
    """Per-field, not all-or-nothing: a route that only wants a longer timeout should
    not have to restate the attempt count, and restating it is how two numbers that
    mean the same thing drift apart."""
    table = load(
        {
            "version": 1,
            "defaults": {"max_attempts": 3, "backoff_initial_seconds": 1.0},
            "routes": [
                {
                    "model": "f",
                    "candidates": [{"provider": "openai"}],
                    "timeout_seconds": 90.0,
                }
            ],
        }
    )
    route = table.routes[0]
    assert route.retry.max_attempts == 3
    assert route.retry.backoff.initial == 1.0
    assert route.timeout.seconds == 90.0


def test_the_backoff_ceiling_defaults_alongside_the_initial_delay() -> None:
    route = load(
        {
            "version": 1,
            "defaults": {"backoff_initial_seconds": 0.5},
            "routes": [{"model": "f", "candidates": [{"provider": "openai"}]}],
        }
    ).routes[0]
    assert route.retry.backoff.initial == 0.5
    assert route.retry.backoff.maximum == BackoffPolicy().maximum


# --- refusals --------------------------------------------------------------


def test_an_empty_document_is_rejected() -> None:
    """Distinct from "a file with no routes". An empty file is a mount that did not
    happen or a truncated write, and saying "declares no routes" would send someone
    to add a route rather than to find out why their file is blank."""
    with pytest.raises(RouteConfigError, match="is empty"):
        routes_from_yaml("")


def test_a_document_that_is_not_yaml_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="not valid YAML"):
        routes_from_yaml("\t\tthis: [is: not: yaml")


def test_a_yaml_document_that_is_not_a_mapping_is_rejected() -> None:
    """A list at the top level parses fine and means nothing here. Catching it at
    load beats a `KeyError` from somewhere inside the app factory."""
    with pytest.raises(RouteConfigError, match="mapping"):
        routes_from_yaml("- one\n- two\n")


def test_a_missing_version_is_rejected() -> None:
    """The version is how a future format change is detected rather than
    misread. A file without one is a file nobody has looked at."""
    with pytest.raises(RouteConfigError, match="version"):
        routes_from_yaml(yaml.safe_dump({"routes": minimal()["routes"]}))


def test_an_unknown_version_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="version 2"):
        routes_from_yaml(yaml.safe_dump(minimal(version=2)))


def test_a_document_with_no_routes_is_rejected() -> None:
    """An empty table means every request 404s, which looks exactly like a broken
    router. Failing at boot says which it is."""
    with pytest.raises(RouteConfigError, match="at least one route"):
        routes_from_yaml(yaml.safe_dump({"version": 1, "routes": []}))


def test_a_route_with_no_candidates_is_rejected() -> None:
    """A route that can never serve anything. The error names the model, because a
    routing table with three good routes and one empty one is otherwise invisible."""
    with pytest.raises(RouteConfigError, match="fast"):
        routes_from_yaml(
            yaml.safe_dump({"version": 1, "routes": [{"model": "fast", "candidates": []}]})
        )


def test_a_candidate_with_no_provider_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="provider"):
        routes_from_yaml(
            yaml.safe_dump(
                {"version": 1, "routes": [{"model": "fast", "candidates": [{"model": "gpt"}]}]}
            )
        )


def test_a_blank_model_name_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="model"):
        routes_from_yaml(
            yaml.safe_dump(
                {"version": 1, "routes": [{"model": "  ", "candidates": [{"provider": "o"}]}]}
            )
        )


def test_two_routes_with_the_same_model_are_rejected() -> None:
    """The second would silently shadow the first, and which one wins would depend
    on file order — a reordering that changes which vendor a customer's requests
    reach, with no diff in the behaviour anyone reviews."""
    with pytest.raises(RouteConfigError, match="duplicate"):
        routes_from_yaml(
            yaml.safe_dump(
                {
                    "version": 1,
                    "routes": [
                        {"model": "fast", "candidates": [{"provider": "openai"}]},
                        {"model": "fast", "candidates": [{"provider": "anthropic"}]},
                    ],
                }
            )
        )


def test_a_zero_max_attempts_is_rejected() -> None:
    """Zero attempts is a route that never calls anything, which the router cannot
    report as a failure of a candidate it never tried."""
    with pytest.raises(RouteConfigError, match="max_attempts"):
        load(minimal(defaults={"max_attempts": 0}))


def test_a_negative_max_attempts_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="max_attempts"):
        load(minimal(defaults={"max_attempts": -1}))


def test_a_negative_backoff_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="backoff_initial_seconds"):
        load(minimal(defaults={"backoff_initial_seconds": -1.0}))


def test_a_ceiling_below_the_initial_delay_is_rejected() -> None:
    """`max(0.25, 2.0)` is a silent fix, and a silent fix in a config loader means
    the file says one thing and the router does another."""
    with pytest.raises(RouteConfigError, match="backoff_max_seconds"):
        load(minimal(defaults={"backoff_initial_seconds": 5.0, "backoff_max_seconds": 1.0}))


def test_a_zero_timeout_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="timeout_seconds"):
        load(minimal(defaults={"timeout_seconds": 0}))


def test_an_unknown_key_is_rejected() -> None:
    """Not ignored. A misspelled `max_attempts` that is silently dropped leaves a
    route retrying once while its author believes it retries three times — a
    reliability setting that appears configured and is not."""
    with pytest.raises(RouteConfigError, match="max_attemtps"):
        routes_from_yaml(
            yaml.safe_dump(
                {
                    "version": 1,
                    "routes": [
                        {
                            "model": "f",
                            "candidates": [{"provider": "openai"}],
                            "max_attemtps": 3,
                        }
                    ],
                }
            )
        )


def test_an_unknown_candidate_key_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="porvider"):
        routes_from_yaml(
            yaml.safe_dump(
                {
                    "version": 1,
                    "routes": [{"model": "f", "candidates": [{"porvider": "openai"}]}],
                }
            )
        )


def test_a_candidate_weight_is_accepted_and_ignored() -> None:
    """Accepted now so a later packet's load balancing is a behaviour change rather
    than a format change — but ignored, so nothing acts on it yet. A weight that is
    parsed and not used would be a silent promise."""
    route = load(
        {
            "version": 1,
            "routes": [
                {
                    "model": "f",
                    "candidates": [{"provider": "openai", "weight": 3}],
                }
            ],
        }
    ).routes[0]
    assert route.candidates[0] == Candidate("openai", "f")


# --- validation against the registry ---------------------------------------


def test_validation_passes_when_every_provider_is_registered() -> None:
    table = load(
        {
            "version": 1,
            "routes": [
                {
                    "model": "fast",
                    "candidates": [{"provider": "openai"}, {"provider": "anthropic"}],
                }
            ],
        }
    )
    table.validate(
        ProviderRegistry().register(FakeProvider("openai")).register(FakeProvider("anthropic"))
    )


def test_validation_rejects_an_unregistered_provider() -> None:
    """The check the loader cannot do. It runs at boot, so a route naming a vendor
    muse has no adapter for is a container that will not start rather than a 503 on
    a customer's first request."""
    table = load(minimal())
    with pytest.raises(RouteConfigError, match="openai"):
        table.validate(ProviderRegistry())


def test_validation_names_every_unregistered_provider() -> None:
    """Both, so a deploy with three bad names is fixed in one pass instead of one
    restart per name."""
    table = load(
        {
            "version": 1,
            "routes": [
                {
                    "model": "f",
                    "candidates": [{"provider": "bedrock"}, {"provider": "vertex"}],
                }
            ],
        }
    )
    with pytest.raises(RouteConfigError) as excinfo:
        table.validate(ProviderRegistry())
    assert "bedrock" in str(excinfo.value)
    assert "vertex" in str(excinfo.value)


def test_the_committed_file_validates_against_its_providers() -> None:
    """The file that ships is validated in the suite. `openai` and `anthropic` are
    the two adapters this packet ships, so this is the real check — a fourth vendor
    added to the file without an adapter fails here."""
    routes_from_yaml(COMMITTED.read_text()).validate(
        ProviderRegistry().register(FakeProvider("openai")).register(FakeProvider("anthropic"))
    )


# --- immutability ----------------------------------------------------------


def test_a_loaded_table_cannot_be_mutated() -> None:
    """A route table is read on the request path by every concurrent request. A
    mutable one is a config change that half the fleet sees and half does not."""
    route = load(minimal()).routes[0]
    with pytest.raises(AttributeError):
        route.model = "changed"  # type: ignore[misc]


def test_candidates_are_immutable() -> None:
    candidate = load(minimal()).routes[0].candidates[0]
    with pytest.raises(AttributeError):
        candidate.provider = "changed"  # type: ignore[misc]


def test_policies_compare_by_value() -> None:
    """So a test can assert a whole policy object rather than four of its fields."""
    assert RetryPolicy(max_attempts=2) == RetryPolicy(max_attempts=2)
    assert RetryPolicy(max_attempts=2) != RetryPolicy(max_attempts=3)


def test_routes_compare_by_value() -> None:
    assert Route("f", (Candidate("openai", "f"),)) == Route("f", (Candidate("openai", "f"),))


def test_a_candidate_defaults_its_model_to_the_routes() -> None:
    """Constructed directly rather than through the loader, so the invariant holds
    however a `Candidate` is built."""
    assert Candidate("openai").model is None
    route = Route("f", (Candidate("openai"),))
    assert route.candidates[0].model == "f"


def test_a_route_built_directly_with_an_explicit_model_keeps_it() -> None:
    assert Route("smart", (Candidate("anthropic", "claude-sonnet-4-5"),)).candidates[0].model == (
        "claude-sonnet-4-5"
    )


def test_an_empty_yaml_body_counts_as_empty() -> None:
    """A file of newlines is what a heredoc with a stripped body produces, and it is
    the same failure as a zero-byte one — reported the same way, so there is one
    message to recognise."""
    with pytest.raises(RouteConfigError, match="is empty"):
        routes_from_yaml("   \n\n\t\n")


# --- the shapes the loader refuses -----------------------------------------


def test_a_defaults_block_that_is_not_a_mapping_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="`defaults` must be a mapping"):
        load(minimal(defaults=["max_attempts"]))


def test_a_routes_key_that_is_not_a_list_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="`routes` must be a list"):
        routes_from_yaml(yaml.safe_dump({"version": 1, "routes": {"fast": []}}))


def test_a_route_that_is_not_a_mapping_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="each route must be a mapping"):
        routes_from_yaml(yaml.safe_dump({"version": 1, "routes": ["fast"]}))


def test_a_candidate_that_is_not_a_mapping_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="each candidate must be a mapping"):
        load({"version": 1, "routes": [{"model": "f", "candidates": ["openai"]}]})


def test_a_candidates_key_that_is_not_a_list_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="`candidates` must be a list"):
        routes_from_yaml(
            yaml.safe_dump({"version": 1, "routes": [{"model": "fast", "candidates": {}}]})
        )


def test_a_document_with_no_routes_key_is_rejected() -> None:
    """Distinct from `routes: []`. A missing key means a file written against a
    different shape; an empty list is a deliberate file with nothing in it. They need
    different fixes, so they get different messages."""
    with pytest.raises(RouteConfigError, match="no `routes` key"):
        routes_from_yaml(yaml.safe_dump({"version": 1}))


# --- numbers the loader refuses to coerce ----------------------------------


def test_a_non_numeric_max_attempts_is_rejected() -> None:
    """`"3"` as a string is what a template produces. Coercing it would work; refusing
    it makes the mistake visible in the file rather than at 3am."""
    with pytest.raises(RouteConfigError, match="whole number"):
        load(minimal(defaults={"max_attempts": "3"}))


def test_a_boolean_max_attempts_is_rejected() -> None:
    """`true` is an int in Python. Left unchecked, `max_attempts: true` would silently
    mean one attempt — the same as the default, and nothing like what the file says."""
    with pytest.raises(RouteConfigError, match="whole number"):
        load(minimal(defaults={"max_attempts": True}))


def test_a_non_numeric_timeout_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="must be a number"):
        load(minimal(defaults={"timeout_seconds": "30s"}))


def test_a_boolean_timeout_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="must be a number"):
        load(minimal(defaults={"timeout_seconds": True}))


def test_a_non_numeric_backoff_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="must be a number"):
        load(minimal(defaults={"backoff_initial_seconds": "fast"}))


def test_a_zero_backoff_is_rejected() -> None:
    """Zero would be a hot loop against a provider that is already overloaded, which
    is the opposite of what a backoff is for."""
    with pytest.raises(RouteConfigError, match="must be positive"):
        load(minimal(defaults={"backoff_initial_seconds": 0}))


def test_a_negative_timeout_on_a_route_is_rejected() -> None:
    with pytest.raises(RouteConfigError, match="must be positive"):
        load(
            {
                "version": 1,
                "routes": [
                    {
                        "model": "f",
                        "candidates": [{"provider": "openai"}],
                        "timeout_seconds": -1.0,
                    }
                ],
            }
        )


# --- kept fields -----------------------------------------------------------


def test_a_route_description_is_kept() -> None:
    """Kept because it is what a human reads in a diff, and dropped descriptions are
    how a routing table becomes a list of unexplained vendor pairs."""
    route = load(
        {
            "version": 1,
            "routes": [
                {
                    "model": "f",
                    "description": "cheap and small",
                    "candidates": [{"provider": "openai"}],
                }
            ],
        }
    ).routes[0]
    assert route.description == "cheap and small"


def test_a_route_without_a_description_has_an_empty_one() -> None:
    """Optional. Requiring prose in a config file is how config files stop being read.
    The committed file writes one for every route; nothing forces it."""
    assert load(minimal()).routes[0].description == ""


def test_a_table_lists_the_providers_it_names() -> None:
    """What `validate` iterates, and what a boot error lists."""
    table = load(
        {
            "version": 1,
            "routes": [
                {
                    "model": "f",
                    "candidates": [{"provider": "openai"}, {"provider": "anthropic"}],
                },
                {"model": "g", "candidates": [{"provider": "openai"}]},
            ],
        }
    )
    assert table.providers() == ("anthropic", "openai")


def test_a_table_keeps_its_default_timeout() -> None:
    """The file-level default, kept on the table so a caller can read the whole policy
    without walking every route."""
    assert load(minimal(defaults={"timeout_seconds": 12.0})).timeout == TimeoutPolicy(seconds=12.0)


# --- the policies validate themselves ---------------------------------------


def test_a_negative_backoff_is_refused_by_the_policy_itself() -> None:
    """The dataclass validates independently of the loader, so a route built in code
    — by a later packet — is held to the same rule as one read from a file."""
    with pytest.raises(ValueError, match="must not be negative"):
        BackoffPolicy(initial=-1.0)


def test_a_backoff_ceiling_below_the_initial_delay_is_refused_by_the_policy() -> None:
    with pytest.raises(ValueError, match="below the initial delay"):
        BackoffPolicy(initial=5.0, maximum=1.0)


def test_a_zero_max_attempts_is_refused_by_the_policy() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy(max_attempts=0)


def test_a_zero_timeout_is_refused_by_the_policy() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        TimeoutPolicy(seconds=0)


def test_a_candidate_without_a_provider_is_refused_by_the_dataclass() -> None:
    with pytest.raises(ValueError, match="must name a provider"):
        Candidate(provider="")


def test_a_route_without_a_model_is_refused_by_the_dataclass() -> None:
    with pytest.raises(ValueError, match="model name"):
        Route(model="  ", candidates=(Candidate("openai"),))


def test_a_route_without_candidates_is_refused_by_the_dataclass() -> None:
    with pytest.raises(ValueError, match="no candidates"):
        Route(model="fast", candidates=())


def test_a_route_resolves_its_candidates_models_at_construction() -> None:
    """One representation, so there is nothing for a consumer to forget to resolve. A
    `Route` holding `model=None` is a footgun for the one caller that forgets, and
    that caller sends `None` to a provider as a model name."""
    route = Route("smart", (Candidate("anthropic"), Candidate("openai", "gpt-4o")))
    assert [candidate.model for candidate in route.candidates] == ["smart", "gpt-4o"]
