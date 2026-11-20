-- 003: transcript lifecycle columns.
-- raw_text_enc (from 001) holds the AES-GCM encrypted JSON {text, segments, meta} of the
-- unmasked transcript; masked_text / segments hold only masked content.

ALTER TABLE transcripts
    ADD COLUMN stt_meta       jsonb,         -- channels, speaker roles, duration (no text)
    ADD COLUMN pii_summary    jsonb,         -- {"PHONE": 1, "NAME": 3, ...}
    ADD COLUMN transcribed_at timestamptz,
    ADD COLUMN masked_at      timestamptz,
    ADD CONSTRAINT transcripts_masked_needs_raw
        CHECK (masked_text IS NULL OR raw_text_enc IS NOT NULL);

-- The read side (app_user / API) never sees the raw transcript column, not even
-- encrypted: column-level grant instead of table-level SELECT.
REVOKE SELECT ON transcripts FROM app_user;
GRANT SELECT (tenant_id, recording_id, provider, model, language, masked_text, segments,
              stt_meta, pii_summary, transcribed_at, masked_at, created_at, updated_at)
    ON transcripts TO app_user;
