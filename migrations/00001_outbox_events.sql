-- 00001_outbox_events.sql — the transactional outbox.
--
-- The column list is core's, from cafaye/core docs/event-outbox.md: "One table,
-- in the publishing service's own database, in its own migration. The column
-- list is the contract; the implementation is the service's business." The two
-- CHECK constraints below copy core's $defs.eventType and $defs.serviceName
-- patterns, so a malformed type is rejected where it is written instead of being
-- published and rejected by every consumer.
--
-- The rule this table exists to enforce: an event is never published outside a
-- transaction that also wrote the state it describes. muse has no usage table —
-- the outbox row *is* the usage record, and billing aggregates it from the bus —
-- so the transaction that inserts this row is the whole write.
--
-- Plain SQL on purpose: muse has no migration tool, and adding one to run two
-- files would be a dependency for nothing. Apply with
-- `psql "$MUSE_DATABASE_URL" -f migrations/00001_outbox_events.sql`; every
-- statement is guarded, so re-running is a no-op.

create table if not exists outbox_events (
    -- The envelope `id`. Primary key *and* the consumer's dedupe key: the same
    -- UUID that goes on the wire is the one that makes a republished row
    -- recognisable as a duplicate, so at-least-once delivery stays safe.
    id          uuid        primary key,

    -- The envelope `type`, and the NATS subject this row is published to. No
    -- mapping table, no prefix rewriting.
    event_type  text        not null,

    -- The publishing service, equal to the envelope's `source` and the manifest's
    -- `name`.
    source      text        not null,

    -- The entity the event is about. Required by core, with the reserved literal
    -- `platform` for an event with no single entity.
    subject     text        not null,

    -- When the state change happened, not when the row was inserted or published.
    -- A row that sat unpublished for an hour still reports the original time.
    time        timestamptz not null,

    -- The envelope `data` verbatim. `jsonb` and not `json`: jsonb is parsed, so a
    -- contract test can reach inside the payload, and it normalises key order so
    -- two identical payloads are byte-identical.
    data        jsonb       not null,

    -- Insert time, and the publisher's ordering key, so a slow batch cannot
    -- publish event 2 before event 1.
    created_at  timestamptz not null default now(),

    -- `null` until the broker acknowledges. This is the only definition of
    -- "published" — not "attempted", not "handed to a client".
    published_at timestamptz,

    -- Publish attempts so far. Incremented on failure; the input to the backoff
    -- and the signal that alerts on.
    attempts    int         not null default 0,

    constraint outbox_events_type_format check (
        event_type ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*\.[a-z][a-z0-9]*(_[a-z0-9]+)*\.[a-z][a-z0-9]*(_[a-z0-9]+)*$'
    ),
    constraint outbox_events_source_format check (
        source ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$'
    ),
    -- core's own bounds, so a value that passes here passes the schema too.
    constraint outbox_events_type_length check (char_length(event_type) between 5 and 120),
    constraint outbox_events_source_length check (char_length(source) between 2 and 40),
    constraint outbox_events_subject_length check (char_length(subject) between 1 and 200),
    constraint outbox_events_attempts_non_negative check (attempts >= 0)
);

-- The publisher's only query is
--   select ... where published_at is null order by created_at, id limit $1
--   for update skip locked
-- so the index is (created_at, id) and partial on the unpublished rows. Without
-- it, that query is a sequential scan of every event this service has ever
-- published, forever. `id` is in the index as well as `created_at` so the order
-- is total: rows written in one transaction share a `created_at`.
create index if not exists outbox_events_unpublished_idx
    on outbox_events (created_at, id)
    where published_at is null;
