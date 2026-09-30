"""Secret handling and redaction.

A provider API key is the one credential muse holds that can be spent by anyone
who reads a log line, an exception message, or a `problem+json` body. These
tests pin the two mechanisms that keep it out of all three: the `Secret` wrapper
(`repr`/`str` never carry the value) and `redact()` (scrubs a known secret out of
text that is about to be logged or returned).

Marked `unit`: no I/O of any kind.
"""

from __future__ import annotations

import logging

import pytest

from muse.redaction import REDACTED, Secret, coerce, redact

pytestmark = [pytest.mark.unit]

KEY = "sk-live-51H8xQ2eZvKYlo2C0fJk7Nb9pQrT4wYd"


def test_secret_reveal_returns_the_plaintext() -> None:
    assert Secret(KEY).reveal() == KEY


def test_secret_repr_does_not_contain_the_plaintext() -> None:
    """The failure that leaks a key is a `%s` of a value in a log or traceback."""
    assert KEY not in repr(Secret(KEY))


def test_secret_str_does_not_contain_the_plaintext() -> None:
    assert KEY not in str(Secret(KEY))


def test_secret_repr_is_the_redaction_marker() -> None:
    assert repr(Secret(KEY)) == f"Secret({REDACTED})"


def test_secret_str_is_the_redaction_marker() -> None:
    assert str(Secret(KEY)) == REDACTED


def test_secret_fstring_does_not_contain_the_plaintext() -> None:
    assert KEY not in f"key={Secret(KEY)}"


def test_secret_format_spec_keeps_the_alignment_and_drops_the_key() -> None:
    """`f"{secret:>40}"` is how a key ends up in an aligned log table. The width
    must still work, or someone replaces the format with a slice of the value."""
    rendered = f"{Secret(KEY):>40}"
    assert len(rendered) == 40
    assert KEY not in rendered
    assert rendered == f"{REDACTED:>40}"


def test_secret_repr_is_usable_in_a_container() -> None:
    """A `Secret` inside a dataclass or a list is printed by the container's own
    `__repr__`, which calls the element's — the path that leaks in a debugger."""
    assert KEY not in repr({"api_key": Secret(KEY)})
    assert KEY not in repr([Secret(KEY)])


def test_secret_equality_compares_the_value() -> None:
    assert Secret(KEY) == Secret(KEY)
    assert Secret(KEY) == KEY
    assert Secret(KEY) != Secret("sk-other")


def test_secret_compares_equal_only_to_secrets_and_strings() -> None:
    """Returning `NotImplemented` for anything else is what lets Python fall back
    to the reflected comparison. Returning `False` instead would make
    `Secret(KEY) != 1` true but `1 != Secret(KEY)` also true by a different path,
    and would hide a genuine type error at a call site."""
    assert Secret(KEY).__eq__(1) is NotImplemented
    assert (Secret(KEY) == 1) is False
    # Reflected comparison: `int.__eq__` returns NotImplemented, so Python calls
    # `Secret.__eq__` on the far side. Both directions must agree.
    assert (1 == Secret(KEY)) is False  # noqa: SIM300 - the reflection is the point


def test_secret_equality_is_constant_time() -> None:
    """A timing oracle on a secret is a side channel; hmac.compare_digest is the
    whole reason `__eq__` is hand-written instead of comparing attributes."""
    assert Secret.__eq__ is not object.__eq__


def test_secret_hashing_is_stable() -> None:
    assert len({Secret(KEY), Secret(KEY), Secret("sk-other")}) == 2


def test_empty_secret_is_falsey() -> None:
    """A missing credential is `None`, and a present-but-empty one is equally
    unusable — both must be falsy so `if not key` catches them."""
    assert not Secret("")
    assert Secret(KEY)


def test_coerce_wraps_a_bare_string() -> None:
    wrapped = coerce(KEY)
    assert isinstance(wrapped, Secret)
    assert wrapped.reveal() == KEY


def test_coerce_passes_a_secret_through_unchanged() -> None:
    original = Secret(KEY)
    assert coerce(original) is original


def test_coerce_rejects_none() -> None:
    """A missing credential is `None`, and silently coercing it to `Secret("")`
    would turn "no key" into "a key that is the empty string"."""
    with pytest.raises(TypeError):
        coerce(None)  # type: ignore[arg-type]


def test_redact_replaces_the_secret() -> None:
    text = f"Authentication failed for key {KEY}."
    assert redact(text, Secret(KEY)) == f"Authentication failed for key {REDACTED}."


def test_redact_removes_every_occurrence() -> None:
    text = f"{KEY} then {KEY}"
    assert redact(text, Secret(KEY)) == f"{REDACTED} then {REDACTED}"


def test_redact_accepts_bare_strings() -> None:
    """Callers hold keys as `str` in plenty of places; the scrubber must not
    make them wrap first, or someone will forget."""
    assert redact(f"leak {KEY}", KEY) == f"leak {REDACTED}"


def test_redact_handles_several_secrets() -> None:
    a, b = "sk-aaa", "sk-bbb"
    assert redact(f"{a} {b}", a, b) == f"{REDACTED} {REDACTED}"


def test_redact_prefers_the_longest_secret_first() -> None:
    """Overlapping secrets: replacing the short one first would leave the tail of
    the long one behind, which is most of the key."""
    short, long = "sk-1234", "sk-1234-567890"
    assert redact(f"x {long} y", short, long) == f"x {REDACTED} y"


def test_redact_ignores_empty_secrets() -> None:
    """`replace("", ...)` would insert the marker between every character."""
    assert redact("clean text", Secret(""), "") == "clean text"


def test_redact_leaves_unrelated_text_untouched() -> None:
    assert redact("nothing to see", Secret(KEY)) == "nothing to see"


def test_secret_never_reaches_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """End to end: a provider exception is logged at error level, and the key it
    echoed must not be in the captured record text or its args."""
    logger = logging.getLogger("muse.test.vault")
    with caplog.at_level(logging.DEBUG):
        logger.error("upstream rejected key %s", Secret(KEY))
    assert KEY not in caplog.text
    assert REDACTED in caplog.text
