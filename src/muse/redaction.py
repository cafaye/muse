"""Secrets, and keeping them out of everything that is not the vault.

A provider API key is the one credential muse holds that anyone who reads a log
line, an exception message, or a `problem+json` body can spend. Two mechanisms
keep it out of all three, and both live here:

- `Secret` — a string wrapper whose `repr`, `str`, and `__format__` are the
  redaction marker. `%s` and f-strings therefore cannot leak a key by accident,
  which is the failure that actually happens.
- `redact()` — a scrubber for text that already contains a key, which is what
  provider SDKs hand back when they reject one. A provider error message is
  attacker-adjacent text: it is written by a third party, and the third party's
  message is where keys end up in logs.

`Secret.reveal()` is the only way to read the value, so every read is greppable.
"""

from __future__ import annotations

import hmac

#: What a redacted value is replaced with. Chosen to be obviously not-a-value, so
#: a redacted log line is never mistaken for a working credential.
REDACTED = "[redacted]"


class Secret:
    """A string that will not print itself.

    Not a dataclass and not a subclass of `str`: subclassing `str` leaks through
    every C-level string operation, and a dataclass would happily show the value
    in its generated `__repr__`.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The plaintext. Every caller of this is a place worth reviewing."""
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        # Without this, `f"{secret}"` and `f"{secret!s}"` would fall through to
        # `str.__format__` on a class that is not a str, and `f"{secret:>40}"` would
        # either raise or — worse, after someone "fixed" it by subclassing str —
        # pad the plaintext. Formatting the marker instead means the width and
        # alignment still work and the padding is made of asterisks.
        return format(REDACTED, spec) if spec else REDACTED

    def __eq__(self, other: object) -> bool:
        """Constant-time comparison.

        Two reasons this is hand-written. Correctness first: it is what makes
        `Secret == "sk-..."` true, so callers holding a bare string do not have to
        remember to wrap. Second, and the reason it is not `self._value ==
        other._value`: a `==` on a secret is a timing oracle if the comparison
        short-circuits, and `hmac.compare_digest` does not.
        """
        if isinstance(other, Secret):
            return hmac.compare_digest(self._value, other._value)
        if isinstance(other, str):
            return hmac.compare_digest(self._value, other)
        return NotImplemented

    def __hash__(self) -> int:
        # Hashed so a `Secret` can be a dict key, but from the *redacted* value: a
        # hash of a secret is a slow oracle if it ever lands in a log or a repr.
        return hash(REDACTED)

    def __bool__(self) -> bool:
        return bool(self._value)


def coerce(value: Secret | str) -> Secret:
    """Wrap a bare string. Idempotent for a value that is already a `Secret`."""
    if isinstance(value, Secret):
        return value
    if isinstance(value, str):
        return Secret(value)
    raise TypeError(f"expected a Secret or str, got {type(value).__name__}")


def redact(text: str, *secrets: Secret | str) -> str:
    """Replace every occurrence of each secret in `text` with the marker.

    Longest first: replacing a short secret that is a prefix of a longer one
    would leave the remainder of the longer one behind, which is most of a key.
    Empty secrets are skipped, because `str.replace("", x)` inserts the marker
    between every character.
    """
    plaintexts = sorted(
        (secret if isinstance(secret, Secret) else Secret(secret) for secret in secrets),
        key=lambda secret: len(secret.reveal()),
        reverse=True,
    )
    for secret in plaintexts:
        value = secret.reveal()
        if value:
            text = text.replace(value, REDACTED)
    return text
