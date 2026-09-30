"""The contract constants core owns, and the validators that enforce them.

core publishes `schemas/event-envelope.schema.json` and the grammar in
`docs/event-naming.md`. A service that emits an event has to satisfy those, and
the cheapest way to satisfy them is to check the value before the insert rather
than to find out from a consumer.

**The patterns below are copies.** core's rule is that a pattern is stated once,
in the schema, and duplicated only where a service must enforce it without a
checkout of core on hand. `tests/test_contracts.py` asserts this copy is
byte-identical to the schema whenever `MUSE_CORE_SCHEMAS` points at a core
checkout, so a change on either side fails a test rather than drifting.

`muse.tokens.consumed` follows the grammar — `<service>.<entity>.<action>`, three
segments, publisher-prefixed. The action `consumed` is not in core's v0 action
vocabulary; the packet brief names it, so it is used as specified and flagged for
the manager (see the worker report).
"""

from __future__ import annotations

import re

from muse.errors import InvalidEventType, InvalidServiceName, InvalidSubject

#: This service's cafaye namespace name. It is the repository name, the envelope
#: `source`, and the first segment of every type this service publishes.
SERVICE_NAME = "muse"

#: core $defs.eventType — three segments, publisher-prefixed, kebab-case service
#: then snake_case entity and action.
EVENT_TYPE_PATTERN = (
    r"^[a-z][a-z0-9]*(-[a-z0-9]+)*\.[a-z][a-z0-9]*(_[a-z0-9]+)*\.[a-z][a-z0-9]*(_[a-z0-9]+)*$"
)

#: core $defs.serviceName.
SERVICE_NAME_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"

#: core's envelope `subject` property. Note the character class includes `_`, so
#: the prefixed ids in core's examples (`usr_01J9Z8…`) are valid subjects. muse
#: uses a UUID as its subject, which is valid and unambiguous.
SUBJECT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$"

#: The one event type muse publishes in v1: token consumption for a routed call.
TOKENS_CONSUMED = "muse.tokens.consumed"

_EVENT_TYPE_RE = re.compile(EVENT_TYPE_PATTERN)
_SERVICE_NAME_RE = re.compile(SERVICE_NAME_PATTERN)
_SUBJECT_RE = re.compile(SUBJECT_PATTERN)


def validate_event_type(value: str) -> str:
    """Return `value` if it is a well-formed event type, else raise.

    Returns the value so a call site can validate and assign in one line, and so
    the tests can assert identity as well as acceptance.
    """
    if not _EVENT_TYPE_RE.match(value):
        raise InvalidEventType(
            f"{value!r} is not a valid event type; expected "
            "<service>.<entity>.<action> per core's envelope schema"
        )
    return value


def validate_service_name(value: str) -> str:
    """Return `value` if it is a valid cafaye service name, else raise.

    The length bound is core's `minLength`/`maxLength`, not part of the copied
    pattern, so it is enforced here. core states the rule as 2-40 characters; a
    one-character name is the case a pattern alone would let through.
    """
    if not 2 <= len(value) <= 40 or not _SERVICE_NAME_RE.match(value):
        raise InvalidServiceName(
            f"{value!r} is not a valid cafaye service name; expected 2-40 "
            "lowercase kebab-case characters"
        )
    return value


def validate_subject(value: str) -> str:
    """Return `value` if it is a valid envelope subject, else raise.

    core's `subject` is required and capped at 200 characters. The cap is enforced
    here as well as by the pattern so a long value fails with a message that says
    which rule it broke.
    """
    if not value or len(value) > 200 or not _SUBJECT_RE.match(value):
        raise InvalidSubject(
            f"{value!r} is not a valid event subject; expected 1-200 characters "
            "matching core's envelope schema pattern"
        )
    return value
