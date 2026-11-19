ALTER TABLE app.investigations ADD COLUMN request jsonb NOT NULL DEFAULT '{}';

CREATE FUNCTION authn.lock_identity(t text,u text,s uuid,admin_required boolean DEFAULT false) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authn AS $$
DECLARE session_row authn.sessions; member_row authn.memberships; user_row authn.users;
BEGIN
    SELECT * INTO user_row FROM authn.users WHERE user_id=u FOR SHARE;
    SELECT * INTO session_row FROM authn.sessions WHERE session_id=s AND user_id=u FOR SHARE;
    SELECT * INTO member_row FROM authn.memberships WHERE tenant_id=t AND user_id=u FOR SHARE;
    RETURN COALESCE(user_row.active AND session_row.session_id IS NOT NULL AND NOT session_row.revoked
       AND session_row.expires_at>clock_timestamp() AND member_row.role IN ('approver','admin')
       AND (NOT admin_required OR member_row.role='admin'),false);
END $$;
CREATE OR REPLACE FUNCTION authn.approval_valid(t text,u text,s uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,authn AS $$
    SELECT authn.lock_identity(t,u,s,false)
$$;

CREATE TABLE app.sandbox_jobs (
    tenant_id text NOT NULL REFERENCES app.stores, job_id uuid NOT NULL,
    snapshot_version integer NOT NULL, actor_id text NOT NULL REFERENCES authn.users,
    session_id uuid NOT NULL REFERENCES authn.sessions,
    state text NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','LEASED','COMPLETED','FAILED')),
    lease_owner uuid, lease_expires_at timestamptz, heartbeat_at timestamptz,
    attempts integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 5,
    ready_at timestamptz NOT NULL DEFAULT clock_timestamp(), last_error text, result jsonb,
    PRIMARY KEY(tenant_id,job_id)
);
ALTER TABLE app.sandbox_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.sandbox_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY read_scope ON app.sandbox_jobs FOR SELECT USING(authn.can_access(tenant_id));
CREATE POLICY owner_all ON app.sandbox_jobs TO ec_owner USING(true) WITH CHECK(true);
CREATE POLICY request_advance ON app.sandbox_jobs FOR INSERT TO ec_api
    WITH CHECK(authn.is_approver(tenant_id) AND authn.member_role(tenant_id)='admin'
               AND actor_id=(SELECT user_id FROM authn.session_identity())
               AND session_id=(SELECT session_id FROM authn.session_identity())
               AND state='QUEUED' AND lease_owner IS NULL AND result IS NULL AND attempts=0);
CREATE POLICY process_advance ON app.sandbox_jobs FOR UPDATE TO ec_simulator
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
REVOKE ALL ON FUNCTION authn.lock_identity(text,text,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authn.lock_identity(text,text,uuid,boolean) TO ec_executor,ec_simulator;
REVOKE ALL ON app.sandbox_jobs FROM PUBLIC;
GRANT SELECT ON app.sandbox_jobs TO ec_api,ec_agent,ec_executor,ec_simulator;
GRANT INSERT ON app.sandbox_jobs TO ec_api;
GRANT UPDATE ON app.sandbox_jobs TO ec_simulator;
