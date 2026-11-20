-- 002: tenant isolation with Row Level Security.
--
-- * ENABLE + FORCE on every tenant table: FORCE makes the policies apply to the table
--   owner (app_owner) too, so "connected as the owner by mistake" is not a bypass.
-- * Policies call app_current_tenant(), which raises when no tenant context is set.
-- * USING filters reads/updates/deletes; WITH CHECK rejects writes for another tenant.
-- * Application roles are NOSUPERUSER NOBYPASSRLS and own nothing (see app/migrate.py).
-- * Nobody gets DELETE: a recording (or its history) is never removed by the app.

ALTER TABLE tenants           ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants           FORCE  ROW LEVEL SECURITY;
ALTER TABLE recordings        ENABLE ROW LEVEL SECURITY;
ALTER TABLE recordings        FORCE  ROW LEVEL SECURITY;
ALTER TABLE transcripts       ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcripts       FORCE  ROW LEVEL SECURITY;
ALTER TABLE crm_actions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_actions       FORCE  ROW LEVEL SECURITY;
ALTER TABLE recording_events  ENABLE ROW LEVEL SECURITY;
ALTER TABLE recording_events  FORCE  ROW LEVEL SECURITY;
ALTER TABLE jobs              ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs              FORCE  ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON tenants
    USING (id = app_current_tenant())
    WITH CHECK (id = app_current_tenant());

CREATE POLICY tenant_isolation ON recordings
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

CREATE POLICY tenant_isolation ON transcripts
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

CREATE POLICY tenant_isolation ON crm_actions
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

CREATE POLICY tenant_isolation ON recording_events
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- The queue is the one deliberate cross-tenant surface: a worker must find work before
-- it knows whose work it is. It holds ids, step and lease data only, no transcript or
-- personal data, and only app_worker gets this policy. Everything a worker does after
-- claiming a job runs under that job's tenant context (app/queue.py).
CREATE POLICY worker_queue ON jobs TO app_worker
    USING (true)
    WITH CHECK (true);

CREATE POLICY tenant_isolation ON jobs TO app_owner, app_user
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

-- ---------------------------------------------------------------------------
-- Grants. Default PUBLIC privileges on these objects are none; be explicit.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;

-- API / read side: read-only, and no access to the queue at all.
GRANT SELECT ON tenants, recordings, transcripts, crm_actions, recording_events TO app_user;

-- Worker / ingest / reprocess: read + write, never DELETE.
GRANT SELECT, INSERT, UPDATE ON tenants, recordings, transcripts, crm_actions,
                                recording_events, jobs TO app_worker;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_worker;

GRANT EXECUTE ON FUNCTION app_current_tenant() TO app_user, app_worker;
