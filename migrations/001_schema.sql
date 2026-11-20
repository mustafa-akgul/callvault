-- 001: core data model.
-- Runs as app_owner (table owner, NOT superuser). Tenant isolation is added in 002.
--
-- Identity model: every tenant-owned row is keyed by (tenant_id, ...). Foreign keys are
-- composite, so a row of tenant A can never reference a row of tenant B, even though
-- FK checks themselves bypass row-level security.

-- ---------------------------------------------------------------------------
-- Tenant context. Fail closed: no context -> SQL error, never "all rows" or "no rows".
-- current_setting(..., true) is used and '' is treated as unset on purpose: after a
-- SET LOCAL transaction ends, the GUC survives on the session as '' (empty string), so
-- a plain current_setting('app.tenant_id') would NOT error on a pooled connection.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_current_tenant() RETURNS text
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    t text := current_setting('app.tenant_id', true);
BEGIN
    IF t IS NULL OR t = '' THEN
        RAISE EXCEPTION 'tenant context is not set (app.tenant_id)'
            USING ERRCODE = 'insufficient_privilege',
                  HINT = 'open the transaction through app.db.tenant_tx()';
    END IF;
    RETURN t;
END
$$;

-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    id          text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Pipeline steps (where a recording is / failed) and recording statuses.
CREATE TYPE pipeline_step AS ENUM ('VALIDATION', 'TRANSCRIPTION', 'MASKING', 'CRM');

CREATE TYPE recording_status AS ENUM (
    'PENDING',          -- ingested, not picked up yet
    'VALIDATING',
    'TRANSCRIBING',
    'MASKING',
    'PROCESSING_CRM',
    'COMPLETED',        -- terminal
    'FAILED',           -- terminal until `reprocess`; failed_step + error_* say where and why
    'DUPLICATE'         -- terminal; byte-identical audio of duplicate_of, never sent to CRM
);

CREATE TABLE recordings (
    tenant_id       text NOT NULL REFERENCES tenants (id),
    id              text NOT NULL CHECK (id ~ '^[A-Za-z0-9_.-]{1,64}$'),
    file_path       text NOT NULL,
    language        text,
    channel         text,
    recorded_at     timestamptz NOT NULL,

    audio_hash      text CHECK (audio_hash ~ '^[0-9a-f]{64}$'),   -- sha256 of the file bytes
    audio_bytes     bigint,
    duplicate_of    text,

    status          recording_status NOT NULL DEFAULT 'PENDING',
    current_step    pipeline_step,          -- step being worked on / waiting for a worker
    failed_step     pipeline_step,
    error_code      text,
    error_message   text,
    retry_count     integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, duplicate_of) REFERENCES recordings (tenant_id, id),
    CHECK (duplicate_of IS NULL OR duplicate_of <> id),
    -- The status and its explanation can never disagree.
    CHECK ((status = 'FAILED')    = (failed_step IS NOT NULL)),
    CHECK ((status = 'DUPLICATE') = (duplicate_of IS NOT NULL)),
    CHECK (status <> 'FAILED' OR error_code IS NOT NULL)
);

CREATE INDEX recordings_hash_idx   ON recordings (tenant_id, audio_hash);
CREATE INDEX recordings_status_idx ON recordings (tenant_id, status);

-- ---------------------------------------------------------------------------
CREATE TABLE transcripts (
    tenant_id       text NOT NULL,
    recording_id    text NOT NULL,
    provider        text NOT NULL,
    model           text,
    language        text,
    raw_text_enc    bytea,          -- unmasked text, application-encrypted (phase 3); never served
    masked_text     text,           -- the only text the API returns
    segments        jsonb,          -- masked segments: [{channel, start, end, text}]
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, recording_id),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id)
);

-- ---------------------------------------------------------------------------
-- Outbox of CRM side effects. One row per (recording, action). The row is written
-- BEFORE the HTTP call, so a crash can never "forget" an in-flight request.
CREATE TABLE crm_actions (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id           text NOT NULL,
    recording_id        text NOT NULL,
    action_type         text NOT NULL CHECK (action_type IN ('booking', 'complaint')),
    -- Tenant-scoped idempotency key, derived (cannot be set wrong by application code).
    -- The CRM's key store is global, so the tenant prefix is what keeps two tenants'
    -- "rec_001" from replaying each other's responses.
    idempotency_key     text GENERATED ALWAYS AS
                            (tenant_id || ':' || action_type || ':' || recording_id) STORED,
    status              text NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING', 'IN_FLIGHT', 'SUCCEEDED',
                                              'NEEDS_FOLLOWUP', 'FAILED')),
    request_payload     jsonb,
    response_payload    jsonb,
    requested_text      text,           -- what the caller said (masked)
    requested_start     timestamptz,    -- what the caller asked for (resolved)
    booked_slot_start   timestamptz,    -- what was booked
    external_id         text,           -- CRM id (bk_..., cp_...)
    attempts            integer NOT NULL DEFAULT 0,
    last_error          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id),
    UNIQUE (tenant_id, recording_id, action_type),
    UNIQUE (idempotency_key)
);

-- A duplicate recording must never reach the CRM, whatever the application does.
CREATE FUNCTION crm_actions_reject_duplicates() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM recordings r
               WHERE r.tenant_id = NEW.tenant_id AND r.id = NEW.recording_id
                 AND (r.duplicate_of IS NOT NULL OR r.status = 'DUPLICATE')) THEN
        RAISE EXCEPTION 'recording %/% is a duplicate; CRM actions are not allowed',
            NEW.tenant_id, NEW.recording_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER crm_actions_no_duplicates
    BEFORE INSERT OR UPDATE OF tenant_id, recording_id ON crm_actions
    FOR EACH ROW EXECUTE FUNCTION crm_actions_reject_duplicates();

-- ---------------------------------------------------------------------------
-- Work queue. Exactly one job per recording; it carries the step to run next.
-- Claimed with FOR UPDATE SKIP LOCKED, held by a lease (locked_until) that the worker
-- extends with heartbeats. A lease that runs out (worker SIGKILLed) is reclaimable.
-- lease_token is the fencing token: a worker can only finish a job it still owns.
CREATE TABLE jobs (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       text NOT NULL,
    recording_id    text NOT NULL,
    step            pipeline_step NOT NULL,
    state           text NOT NULL DEFAULT 'queued'
                        CHECK (state IN ('queued', 'running', 'done', 'failed')),
    attempts        integer NOT NULL DEFAULT 0,     -- attempts of the CURRENT step
    max_attempts    integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    run_at          timestamptz NOT NULL DEFAULT now(),
    locked_by       text,
    locked_until    timestamptz,
    lease_token     uuid,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, recording_id),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id),
    CHECK ((state = 'running') = (lease_token IS NOT NULL AND locked_until IS NOT NULL))
);

CREATE INDEX jobs_ready_idx   ON jobs (run_at, id)     WHERE state = 'queued';
CREATE INDEX jobs_running_idx ON jobs (locked_until)   WHERE state = 'running';

-- ---------------------------------------------------------------------------
-- Append-only history: every transition, retry and error, with the worker that did it.
CREATE TABLE recording_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       text NOT NULL,
    recording_id    text NOT NULL,
    at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    event           text NOT NULL,
    step            pipeline_step,
    from_status     recording_status,
    to_status       recording_status,
    detail          text,
    worker          text,
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id)
);

CREATE INDEX recording_events_idx ON recording_events (tenant_id, recording_id, id);
