"""`/v1/chat/completions` — delegation, read off the source tree rather than the wire.

## Why this file exists, when `tests/test_alias.py` already covers the behaviour

`test_alias.py` holds the alias to being an alias from the outside: both paths call
the provider once, both emit one metered row, both fail identically. That is the
property that matters and it is behavioural, which is the right level to assert it at.

But every one of those tests passes against a handler that inlined its own copy of the
routing logic and happened to agree with `/v1/route` on every observable the suite
reaches. The delegation is a *structural* claim — `muse.api.build_router` registers
three endpoints, and two of them are two-line renderers over one shared function — and
no request/response assertion can distinguish "delegates" from "copied and matches".

So this file parses `src/muse/api.py` and asserts the structure. It is the same
technique `tests/test_tenant_scoping.py` uses for SQL statements and
`tests/test_dependencies.py` uses for imports: **the tree is the oracle for a claim
about the tree.**

## The limit, stated because a check that cannot be trusted is worse than none

An AST walk sees calls it can name and misses work done through anything else — a
subclass, a patched collaborator, a `getattr` dispatch table. It therefore holds the
*delegation* claim, and `test_alias.py` holds the *behaviour* claim; neither replaces
the other, and a successor who breaks this file should read both before deciding which
half they broke.

The guards below each name the defect they prevent, and each was broken on purpose and
watched go red before being committed (AGENTS.md rule 23: a guard that cannot fail is a
comment).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "muse" / "api.py"

#: The three paths the router is expected to register, and the one function the two
#: completion endpoints must both go through.
COMPLETION_ENDPOINTS = ("/route", "/chat/completions")
DELEGATED_TO = "serve_completion"

#: Calls that would mean a second dispatch. Named rather than pattern-matched on the
#: word "route", because a docstring explaining that routing happens once, and a
#: function called `serve_completion`, both contain it — and a guard that fires on
#: prose is a guard that gets deleted the first day it is inconvenient.
DISPATCH_CALLS = frozenset({"route", "complete", "acompletion"})


def module() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def router_builder() -> ast.FunctionDef:
    """`build_router`, the one function that registers the versioned surface.

    Located by name and required to be unique: two functions with that name would mean
    the walk below had silently picked one of them and proved nothing about the other.
    """
    found = [
        node
        for node in ast.walk(module())
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "build_router"
    ]
    assert len(found) == 1, f"expected exactly one build_router, found {len(found)}"
    return found[0]


#: Which method each path is registered under. Stated rather than inferred, because
#: `/v1/models` being a GET is a contract fact — a stock client discovers models with
#: GET — and a walk that derived the method would have nothing to compare a change to.
METHODS = {"/route": "post", "/chat/completions": "post", "/decisions": "post", "/models": "get"}


def endpoint_handlers() -> dict[str, ast.AsyncFunctionDef]:
    """Each registered endpoint's path, mapped to the handler it registers.

    Read off the decorator rather than the function name, because the path is the
    contract and the name is not: a handler called `chat_completion` serving `/route`
    would satisfy any name-based guard and break every caller.
    """
    handlers: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(router_builder()):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            # Only a decorator *call* registers a path: `@api.post` bare does not, and
            # treating the attribute as one would read `api.post`'s own arguments.
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr not in {"post", "get"}:
                continue
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                handlers[decorator.args[0].value] = node
    return handlers


def registered_methods() -> dict[str, str]:
    """Each registered path, mapped to the HTTP method it was registered under."""
    methods = {}
    for node in ast.walk(router_builder()):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr not in {"post", "get"}:
                continue
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                methods[decorator.args[0].value] = decorator.func.attr
    return methods


def called_names(handler: ast.AsyncFunctionDef) -> set[str]:
    """Every function and method name the handler calls.

    Names rather than qualified paths, because the claim under test is "does this
    handler dispatch work itself", and a dispatch is a call by any spelling.
    """
    return {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name)
    }


def test_the_router_registers_exactly_three_endpoints():
    """The surface is three paths, asserted from the code rather than the document.

    `tests/test_openapi.py` checks the document against the app; this checks the app
    against a stated intent, so a path added to both the code and the document — which
    would leave the other test perfectly green — still needs a deliberate edit here.
    """
    assert sorted(endpoint_handlers()) == ["/chat/completions", "/decisions", "/models", "/route"]


def test_the_paths_are_registered_under_the_methods_the_contract_says():
    """Both completions are POST, the listing is GET.

    A completion is POST-only because it costs money, and there is nothing safe to
    serve on a GET of the same path; the listing is GET because that is how a stock
    OpenAI client discovers models. Stated here as well as in `openapi/v1.yaml` because
    a document and a registration can agree while both are wrong about intent — and
    this is the file that would go quiet.
    """
    assert registered_methods() == METHODS


def test_both_completion_endpoints_delegate_to_the_one_function():
    """The load-bearing assertion of this file.

    Each completion endpoint's handler calls `serve_completion`. Together with
    `test_the_completion_endpoints_hold_no_dispatch_of_their_own`, this says the alias
    and `/v1/route` differ in their rendering and in nothing else.
    """
    handlers = endpoint_handlers()
    for path in COMPLETION_ENDPOINTS:
        assert DELEGATED_TO in called_names(handlers[path]), (
            f"{path} does not call {DELEGATED_TO}; a second implementation of routing "
            "is the defect this packet exists to prevent"
        )


def test_the_completion_endpoints_hold_no_dispatch_of_their_own():
    """Neither handler calls the router, a provider, or the meter directly.

    The half of the delegation claim that a positive assertion cannot make: finding
    `serve_completion` in a handler is compatible with a handler that *also* routes
    something itself, and that is precisely the shape of the bug. So the absence is
    asserted, separately, and both halves are needed for either to mean anything.
    """
    handlers = endpoint_handlers()
    for path in COMPLETION_ENDPOINTS:
        calls = called_names(handlers[path])
        offenders = sorted(calls & DISPATCH_CALLS)
        assert not offenders, f"{path} dispatches work itself: {offenders}"


def test_the_shared_path_is_where_the_metering_happens():
    """Metering lives in `serve_completion`, and neither endpoint calls `record`.

    Worth its own guard because it is the load-bearing consequence rather than the
    mechanism: an alias that metered twice would double-bill and one that did not
    meter would serve completions nobody is charged for, and both would look correct
    in every response assertion this repository makes.
    """
    served = [
        node
        for node in ast.walk(module())
        if isinstance(node, ast.AsyncFunctionDef) and node.name == DELEGATED_TO
    ]
    assert len(served) == 1, f"expected exactly one {DELEGATED_TO}, found {len(served)}"

    assert "record" in called_names(served[0]), (
        f"{DELEGATED_TO} does not meter; a completion nobody is charged for is worse than a 503"
    )
    for path in COMPLETION_ENDPOINTS:
        assert "record" not in called_names(endpoint_handlers()[path]), (
            f"{path} meters for itself, so the shared path would meter it a second time"
        )


def test_the_shared_path_authenticates_before_it_reads_the_body():
    """The ordering, as a structural fact rather than an observed status.

    An unauthenticated request must not be able to make muse spend money, and it must
    not be able to learn whether a model name routes at all — so the bearer check comes
    before the body is parsed and long before the router is reached. Asserted on the
    source because a test can only observe it by measuring something the handler
    declines to do, which is exactly the kind of thing a test stops checking.
    """
    served = next(
        node
        for node in ast.walk(module())
        if isinstance(node, ast.AsyncFunctionDef) and node.name == DELEGATED_TO
    )
    calls = [
        (node.lineno, node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id)
        for node in ast.walk(served)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name)
    ]
    auth = next(line for line, name in calls if name == "require_bearer")
    parsed = next(line for line, name in calls if name == "model_validate")
    routed = next(line for line, name in calls if name in DISPATCH_CALLS)

    assert auth < parsed < routed, (
        f"require_bearer at line {auth}, body parsed at {parsed}, dispatch at {routed}"
    )


def test_the_shared_path_is_not_reachable_only_through_one_endpoint():
    """Two callers, one function.

    The inverse of the delegation tests, and it is the one that catches a plausible
    refactor: extracting `serve_completion` for `/v1/route` and then giving the alias
    its own private copy would pass every behavioural test in `test_alias.py` while
    leaving this file green on "it delegates" — because each copy would still be a
    function. Counting callers is what makes "one path" an actual statement.
    """
    handlers = endpoint_handlers()
    callers = [
        path for path in COMPLETION_ENDPOINTS if DELEGATED_TO in called_names(handlers[path])
    ]
    assert len(callers) == len(COMPLETION_ENDPOINTS)


def test_the_model_list_authenticates_the_same_way():
    """`GET /v1/models` calls `require_bearer` — the same check, not a weaker one.

    A separate, looser check on a listing endpoint is the exact shape of the defect
    this packet is avoiding elsewhere: a second auth policy, which is a second thing to
    keep in agreement and a second thing to get wrong under time pressure. The scope is
    the same constant for the same reason — `models:read` would be a new scope
    namespace, which is a platform decision this service does not make here.
    """
    handler = endpoint_handlers()["/models"]
    calls = called_names(handler)
    assert "require_bearer" in calls, "the model list is reachable without a credential"
    assert "auth_problem" in calls, "an auth failure on the listing must be a problem+json"


def test_the_model_list_is_sourced_from_the_route_table():
    """It reads `container.routes`, and no model name is written as a literal.

    A hardcoded list is a second source of truth about what muse can route to, and it
    goes stale the first time somebody edits `config/routes.yaml`. Asserted on the
    source because the behavioural control in `test_alias.py` — a route table named
    alpha/beta/gamma — is the only thing that would notice, and it cannot see a list
    built from a literal that happens to be right today.
    """
    handler = endpoint_handlers()["/models"]
    # Docstrings excluded, exactly as `tests/test_tenant_scoping.py::_string_literals`
    # excludes them: a handler that *explains* where the ids come from must not fail a
    # guard about literals, or the first person to document the function would have to
    # delete the documentation to keep the suite green.
    docstrings = _docstrings(handler)
    # Decorators excluded as well as docstrings: `summary=` and `description=` are prose
    # by another name, and a guard that counted them would fail the moment somebody
    # documented the endpoint. What remains is the handler's *logic*, where a string is
    # a value being used.
    decorated = {id(node) for decorator in handler.decorator_list for node in ast.walk(decorator)}
    literals = [
        node.value
        for node in ast.walk(handler)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and id(node) not in decorated
    ]
    # `table.models()` is the read; `routes` is the attribute it is reached through.
    # Both are asserted because the behavioural control in `test_alias.py` — a route
    # table named alpha/beta/gamma — is the only thing that would catch a literal list
    # that happens to be right today, and it cannot see one built from the wrong source
    # that still returns route names.
    assert "models" in called_names(handler), "the listing does not call the table's models()"
    attributes = {
        node.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }
    assert "routes" in attributes, "the listing does not read the container's route table"
    # `list` and `model` are OpenAI's two reserved `object` values. Every other string in
    # the handler is prose, so the list is short enough to pin exactly: a model id added
    # here would be the third kind of thing and would fail.
    assert sorted(literals) == ["list", "model"], (
        f"the listing writes model ids as literals: {sorted(literals)}"
    )


def _docstrings(handler: ast.AsyncFunctionDef) -> set[int]:
    """The `id`s of this function's docstring constants — its own and any nested."""
    found: set[int] = set()
    for node in ast.walk(handler):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


@pytest.mark.parametrize("path", COMPLETION_ENDPOINTS)
def test_the_completion_endpoints_declare_a_response_model(path):
    """Each declares one, so the generated document and the served body agree.

    Cheap, and it is the difference between a contract a client can be generated from
    and a path that works by accident.
    """
    handlers = endpoint_handlers()
    handler = handlers[path]
    declared = {
        keyword.value.id
        for decorator in handler.decorator_list
        if isinstance(decorator, ast.Call)
        for keyword in decorator.keywords
        if keyword.arg == "response_model" and isinstance(keyword.value, ast.Name)
    }
    assert declared == {"RouteResponseBody" if path == "/route" else "ChatCompletionBody"}
