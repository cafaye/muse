"""Deterministic providers, for tests and for the local compose stack.

These live in `src/` rather than in `tests/` for a reason: the router's own tests
need a provider, and a packet that writes contract tests against the API needs one
too. A double that exists only inside one test directory cannot be reused by the
next one, and then the second one reimplements it slightly differently — which is
how two suites end up disagreeing about what the router does.

`FakeProvider` echoes a fixed completion. `ScriptedProvider` takes a script, which
is how the fallback tests are written: "fail once, then succeed" in source order
rather than in a counter.
"""

from __future__ import annotations

from collections.abc import Sequence

from muse.errors import PriceUnavailable
from muse.providers import (
    Completion,
    CompletionRequest,
    Health,
    Message,
    Price,
)


class FakeProvider:
    """A provider that always answers the same way.

    The default price is per-provider, not per-model, because a test that has to
    state a price to test something unrelated is a test that will be deleted rather
    than fixed.
    """

    def __init__(
        self,
        name: str = "fake",
        content: str = "fake completion",
        tokens_in: int = 10,
        tokens_out: int = 5,
        price: Price | None = None,
        healthy: bool = True,
    ) -> None:
        self.name = name
        self.content = content
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.price = price if price is not None else Price(1000, 2000)
        self.healthy = healthy
        self.calls = 0
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        self.requests.append(request)
        return Completion(
            provider=self.name,
            model=request.model,
            content=self.content,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            price=self.cost_per_1k_tokens(request.model),
        )

    async def health(self) -> Health:
        return Health(healthy=self.healthy, detail="fake provider")

    def cost_per_1k_tokens(self, model: str) -> Price:
        return self.price


class ScriptedProvider(FakeProvider):
    """A provider whose next result the test chooses.

    `errors` and `completions` are consumed in order, and `errors` first while any
    remain: a script reads as "this happens, then that", which is how the fallback
    tests are written. Once both are exhausted the last completion is replayed, so
    a router retrying a candidate does not need a second scripted entry — and an
    exhausted script is not a test failure about the double.
    """

    def __init__(
        self,
        name: str = "fake",
        price: Price | None = None,
        errors: Sequence[Exception] = (),
        completions: Sequence[Completion] = (),
        prices: dict[str, Price] | None = None,
        healthy: bool = True,
    ) -> None:
        super().__init__(name=name, price=price, healthy=healthy)
        self._errors = list(errors)
        self._completions = list(completions)
        self._prices = dict(prices) if prices else {}
        #: The scripted outcomes in the order they were queued, kept so a test can
        #: assert what it configured without restating the constructor arguments.
        self.script: tuple[object, ...] = (*self._errors, *self._completions)

    async def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        self.requests.append(request)
        if self._errors:
            raise self._errors.pop(0)
        if not self._completions:
            raise AssertionError(
                f"provider {self.name!r} has no scripted completion left; the router "
                "retried past the end of the script"
            )
        scripted = self._completions.pop(0)
        return Completion(
            provider=self.name,
            model=request.model,
            content=scripted.content,
            tokens_in=scripted.tokens_in,
            tokens_out=scripted.tokens_out,
            price=self._prices.get(request.model, scripted.price),
            finish_reason=scripted.finish_reason,
        )

    def cost_per_1k_tokens(self, model: str) -> Price:
        if self._prices:
            # An explicit table is authoritative: a model missing from it is a
            # deliberate "this provider cannot serve that model", which is a routing
            # decision the test is making, not a missing fixture.
            try:
                return self._prices[model]
            except KeyError:
                raise PriceUnavailable(
                    f"provider {self.name!r} has no price for model {model!r}"
                ) from None
        return self.price


__all__ = ["FakeProvider", "Message", "ScriptedProvider"]
