"""A stand-in for the litellm module.

Injected into `LiteLLMProvider` rather than monkeypatched, for two reasons. The
tests state exactly which litellm surface the adapter touches, so if it starts
calling a third entry point they fail loudly instead of the suite quietly reaching
the real module and a real network. And the stub raises the *same exception class
names* the real module raises, so `LiteLLMProvider._translate` — the mapping that
decides what gets retried and what is reported to a caller — is the code under
test rather than a mock of it.
"""

from __future__ import annotations

import types
from typing import Any

#: Every exception class name the adapter's mapping consults. A stub that omits one
#: is exercising the default branch, which is a different test.
LITELLM_ERROR_NAMES = (
    "AuthenticationError",
    "RateLimitError",
    "ServiceUnavailableError",
    "Timeout",
    "APIConnectionError",
    "APIError",
    "ContextWindowExceededError",
    "ContentPolicyViolationError",
    "BadRequestError",
    "NotFoundError",
    "UnprocessableEntityError",
    "InternalServerError",
    "InvalidRequestError",
    "BudgetExceededError",
    "PermissionDeniedError",
)

#: litellm's price table, dollars per token, for the models the tests use.
PRICES: dict[str, dict[str, float]] = {
    "openai/gpt-4o-mini": {"input_cost_per_token": 1.5e-07, "output_cost_per_token": 6e-07},
    "anthropic/claude-sonnet-4-5": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
    },
}

#: A well-formed completion response, shaped like the dict form of litellm's
#: `ModelResponse`. Every test overrides the fields it cares about.
RESPONSE: dict[str, Any] = {
    "choices": [{"message": {"content": "hello there"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 8},
    "model": "gpt-4o-mini",
}

#: Distinguishes "no response given, use the default" from "the response is None".
#: A test asserting the adapter's shape check has to be able to ask for a null.
DEFAULT = object()


def make_stub(
    *,
    response: Any = DEFAULT,
    raises: str | None = None,
    error_message: str = "",
    model_info: dict[str, dict[str, Any]] | None = None,
    status_code: int | None = None,
) -> types.ModuleType:
    """Build the stub.

    `raises` names an exception *class* from `LITELLM_ERROR_NAMES`; the stub raises
    an instance of its own class of that name. Naming the class rather than passing
    an instance is the point: the adapter's mapping is `isinstance(error,
    module.<ClassName>)`, so a test that raised a class the stub does not have
    would fall through to the default branch and pass for the wrong reason.

    `response=None` is honoured as a null response rather than replaced by the
    default, which is what a test asserting the shape check needs.

    `status_code` is attached to the raised exception, because that is where the
    real litellm puts it and the adapter's 408 handling reads it. A test that raised
    a 408 without a status would exercise the deadline branch instead.

    `model_info` overrides the price table, and a model missing from it raises
    `KeyError` — what the real `get_model_info` does, and what `PriceUnavailable`
    exists to translate.
    """
    module = types.ModuleType("litellm_stub")
    captured: dict[str, Any] = {}
    module.captured = captured  # type: ignore[attr-defined]
    #: How many times `acompletion` was entered. The counter a "no request was
    #: issued" assertion needs, and the last statement before a socket would exist.
    module.acompletion_calls = 0  # type: ignore[attr-defined]

    for name in LITELLM_ERROR_NAMES:
        module.__dict__[name] = type(name, (Exception,), {})

    async def acompletion(**kwargs: Any) -> Any:
        module.acompletion_calls += 1  # type: ignore[attr-defined]
        captured.update(kwargs)
        if raises is not None:
            error = module.__dict__[raises](error_message)
            if status_code is not None:
                error.status_code = status_code
            raise error
        return RESPONSE if response is DEFAULT else response

    def get_model_info(model: str) -> dict[str, Any]:
        table = PRICES if model_info is None else model_info
        if model not in table:
            raise KeyError(model)
        return table[model]

    module.acompletion = acompletion  # type: ignore[attr-defined]
    module.get_model_info = get_model_info  # type: ignore[attr-defined]
    return module
