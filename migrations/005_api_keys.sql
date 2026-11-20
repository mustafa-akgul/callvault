-- 005: tenant API keys. The tenant a caller acts as comes from its key, never from a
-- user-supplied --tenant flag or header.
--
-- Only the SHA-256 of a key is stored. The lookup policy shows a row only when the
-- caller presents its hash (app.api_key_hash), so keys cannot be enumerated: there is
-- no way to list tenants or keys without already holding one.

CREATE TABLE api_keys (
    key_hash    text PRIMARY KEY CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    tenant_id   text NOT NULL REFERENCES tenants (id),
    label       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE  ROW LEVEL SECURITY;

CREATE POLICY key_lookup ON api_keys FOR SELECT
    USING (key_hash = current_setting('app.api_key_hash', true) AND revoked_at IS NULL);

-- Keys are issued inside the tenant's own context only.
CREATE POLICY key_issue ON api_keys FOR INSERT TO app_worker
    WITH CHECK (tenant_id = app_current_tenant());

REVOKE ALL ON api_keys FROM PUBLIC;
GRANT SELECT ON api_keys TO app_user, app_worker;
GRANT INSERT ON api_keys TO app_worker;
