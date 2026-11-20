-- 004: CRM delivery.
--
-- crm_actions (001) is the outbox: one row per (recording, action), written before any
-- HTTP call. Bookings can need several slots (409 -> next slot), and the CRM rejects a
-- reused Idempotency-Key with a different body (422), so every slot tried is its own
-- attempt row with its own derived key. The attempt is committed BEFORE the request is
-- sent; after a crash or a lost response the same row (same key, same body) is resent.

-- Granular failure location for the CRM step (job.step stays 'CRM', so reprocess
-- resumes at PROCESSING_CRM).
ALTER TYPE pipeline_step ADD VALUE IF NOT EXISTS 'CRM_BOOKING';
ALTER TYPE pipeline_step ADD VALUE IF NOT EXISTS 'CRM_COMPLAINT';

-- What the CRM step understood from the masked transcript. Written once, before any
-- CRM call, so retries and reprocess never re-derive (and never change) the request.
ALTER TABLE transcripts
    ADD COLUMN extraction   jsonb,
    ADD COLUMN extracted_at timestamptz;
GRANT SELECT (extraction, extracted_at) ON transcripts TO app_user;

ALTER TABLE crm_actions
    ADD COLUMN requested_window jsonb,     -- parsed constraints: exact / dates / weekdays / hours
    ADD COLUMN followup_reason  text,
    ADD CONSTRAINT crm_actions_followup_has_reason
        CHECK (status <> 'NEEDS_FOLLOWUP' OR followup_reason IS NOT NULL),
    ADD CONSTRAINT crm_actions_booked_has_slot
        CHECK (action_type <> 'booking' OR status <> 'SUCCEEDED' OR booked_slot_start IS NOT NULL);

CREATE TABLE crm_booking_attempts (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id           text NOT NULL,
    recording_id        text NOT NULL,
    action_id           bigint NOT NULL REFERENCES crm_actions (id),
    slot_start          timestamptz NOT NULL,
    slot_start_text     text NOT NULL,              -- exactly as the CRM lists it
    -- <tenant_id>:booking:<recording_id>:<slot_start>; derived, cannot be set by hand.
    idempotency_key     text GENERATED ALWAYS AS
                            (tenant_id || ':booking:' || recording_id || ':' || slot_start_text)
                            STORED,
    request_body        jsonb NOT NULL,             -- resent byte-for-byte on every retry
    status              text NOT NULL DEFAULT 'PLANNED'
                            CHECK (status IN ('PLANNED', 'SUCCEEDED', 'CONFLICT', 'REJECTED',
                                              'ADOPTED')),
    http_status         integer,
    response            jsonb,
    sends               integer NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, recording_id) REFERENCES recordings (tenant_id, id),
    UNIQUE (idempotency_key),
    UNIQUE (action_id, slot_start_text)
);

-- At most one in-flight attempt per booking: no way to have two slots "being booked".
CREATE UNIQUE INDEX crm_booking_attempts_one_planned
    ON crm_booking_attempts (action_id) WHERE status = 'PLANNED';
-- At most one successful booking per recording.
CREATE UNIQUE INDEX crm_booking_attempts_one_success
    ON crm_booking_attempts (tenant_id, recording_id) WHERE status IN ('SUCCEEDED', 'ADOPTED');

ALTER TABLE crm_booking_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_booking_attempts FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON crm_booking_attempts
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

REVOKE ALL ON crm_booking_attempts FROM PUBLIC;
GRANT SELECT ON crm_booking_attempts TO app_user;
GRANT SELECT, INSERT, UPDATE ON crm_booking_attempts TO app_worker;
