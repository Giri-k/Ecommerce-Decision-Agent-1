-- Run by the migration owner, never a runtime credential.
CREATE TABLE authn.users (
    user_id text PRIMARY KEY, display_name text NOT NULL, active boolean NOT NULL DEFAULT true
);
CREATE TABLE authn.memberships (
    tenant_id text NOT NULL, user_id text NOT NULL REFERENCES authn.users,
    role text NOT NULL CHECK (role IN ('viewer','approver','admin')),
    PRIMARY KEY (tenant_id,user_id)
);
CREATE TABLE authn.sessions (
    session_id uuid PRIMARY KEY, token_hash text UNIQUE NOT NULL,
    user_id text NOT NULL REFERENCES authn.users, expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE authn.service_bindings (
    db_role text PRIMARY KEY, tenant_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('agent','executor','simulator'))
);

CREATE FUNCTION authn.session_identity() RETURNS TABLE(session_id uuid,user_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog,authn AS $$
    SELECT s.session_id,s.user_id FROM authn.sessions s JOIN authn.users u USING(user_id)
    WHERE s.token_hash = current_setting('app.token_hash',true)
    AND NOT s.revoked AND u.active AND s.expires_at > clock_timestamp()
$$;
CREATE FUNCTION authn.member_role(t text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog,authn AS $$
    SELECT m.role FROM authn.memberships m JOIN authn.session_identity() s USING(user_id)
    WHERE m.tenant_id=t
$$;
CREATE FUNCTION authn.can_access(t text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog,authn AS $$
    SELECT CASE WHEN pg_has_role(session_user,'ec_api','MEMBER')
    THEN authn.member_role(t) IS NOT NULL ELSE EXISTS(
        SELECT 1 FROM authn.service_bindings WHERE db_role=session_user AND tenant_id=t) END
$$;
CREATE FUNCTION authn.is_approver(t text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog,authn AS $$
    SELECT pg_has_role(session_user,'ec_api','MEMBER')
    AND COALESCE(authn.member_role(t) IN ('approver','admin'),false)
$$;
CREATE FUNCTION authn.approval_valid(t text,u text,s uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog,authn AS $$
    SELECT EXISTS(SELECT 1 FROM authn.sessions a JOIN authn.users b USING(user_id)
    JOIN authn.memberships m USING(user_id)
    WHERE a.session_id=s AND a.user_id=u AND m.tenant_id=t AND m.role IN ('approver','admin')
    AND b.active AND NOT a.revoked AND a.expires_at>clock_timestamp())
$$;

CREATE TABLE app.policy_versions (
    tenant_id text NOT NULL, version text NOT NULL, config jsonb NOT NULL,
    PRIMARY KEY(tenant_id,version)
);
CREATE TABLE app.stores (
    tenant_id text PRIMARY KEY, snapshot jsonb NOT NULL, policy_version text NOT NULL,
    calendar_origin date NOT NULL,
    execution_enabled boolean NOT NULL DEFAULT false,
    CHECK(snapshot->>'tenant_id'=tenant_id),
    CHECK(snapshot->>'policy_version'=policy_version),
    FOREIGN KEY(tenant_id,policy_version) REFERENCES app.policy_versions
);
CREATE TABLE app.investigations (
    tenant_id text NOT NULL REFERENCES app.stores, run_id text NOT NULL,
    state text NOT NULL DEFAULT 'QUEUED', created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,run_id)
);
CREATE TABLE app.proposals (
    tenant_id text NOT NULL, proposal_id uuid NOT NULL, run_id text NOT NULL,
    payload jsonb NOT NULL, payload_hash text NOT NULL CHECK(length(payload_hash)=64),
    status text NOT NULL DEFAULT 'PENDING' CHECK(status IN
        ('PENDING','APPROVED','REJECTED','EXECUTED','STALE','EXPIRED','FAILED_SAFE')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,proposal_id), UNIQUE(tenant_id,proposal_id,payload_hash),
    CHECK(payload->>'tenant_id'=tenant_id), CHECK(payload->>'run_id'=run_id),
    FOREIGN KEY(tenant_id,run_id) REFERENCES app.investigations
);
CREATE TABLE app.approvals (
    tenant_id text NOT NULL, proposal_id uuid NOT NULL, payload_hash text NOT NULL,
    approver_user_id text NOT NULL REFERENCES authn.users, session_id uuid NOT NULL REFERENCES authn.sessions,
    approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,proposal_id), UNIQUE(tenant_id,proposal_id,payload_hash),
    FOREIGN KEY(tenant_id,proposal_id,payload_hash) REFERENCES app.proposals(tenant_id,proposal_id,payload_hash)
);
CREATE TABLE app.execution_jobs (
    tenant_id text NOT NULL, proposal_id uuid NOT NULL, job_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','LEASED','COMPLETED','FAILED')),
    lease_owner uuid, lease_expires_at timestamptz, heartbeat_at timestamptz,
    attempts integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 5,
    ready_at timestamptz NOT NULL DEFAULT clock_timestamp(), last_error text,
    PRIMARY KEY(tenant_id,job_id), UNIQUE(tenant_id,proposal_id),
    FOREIGN KEY(tenant_id,proposal_id) REFERENCES app.approvals
);
CREATE TABLE app.executions (
    tenant_id text NOT NULL, proposal_id uuid NOT NULL, payload_hash text NOT NULL,
    execution_id uuid NOT NULL, receipt jsonb NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,execution_id), UNIQUE(tenant_id,proposal_id),
    FOREIGN KEY(tenant_id,proposal_id,payload_hash) REFERENCES app.approvals(tenant_id,proposal_id,payload_hash)
);
CREATE TABLE app.purchase_orders (
    tenant_id text NOT NULL REFERENCES app.stores, order_id text NOT NULL, sku text NOT NULL,
    proposal_id uuid, data jsonb NOT NULL,
    PRIMARY KEY(tenant_id,order_id),
    FOREIGN KEY(tenant_id,proposal_id) REFERENCES app.proposals
);
CREATE TABLE app.audit_events (
    tenant_id text NOT NULL REFERENCES app.stores, event_id uuid NOT NULL,
    kind text NOT NULL, actor_id text NOT NULL, data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(tenant_id,event_id)
);
CREATE TABLE app.idempotency (
    tenant_id text NOT NULL REFERENCES app.stores, actor_id text NOT NULL,
    operation text NOT NULL, key text NOT NULL, request_hash text NOT NULL, response jsonb NOT NULL,
    PRIMARY KEY(tenant_id,actor_id,operation,key)
);
CREATE TABLE app.thread_records (
    tenant_id text NOT NULL, run_id text NOT NULL, sequence integer NOT NULL,
    kind text NOT NULL CHECK(kind IN ('trace','checkpoint')), data jsonb NOT NULL,
    PRIMARY KEY(tenant_id,run_id,kind,sequence),
    FOREIGN KEY(tenant_id,run_id) REFERENCES app.investigations
);
CREATE TABLE evaluator.sandbox_state (
    tenant_id text PRIMARY KEY REFERENCES app.stores, checkpoint jsonb NOT NULL,
    applied_execution_ids jsonb NOT NULL DEFAULT '[]'
);
ALTER TABLE authn.memberships ADD FOREIGN KEY(tenant_id) REFERENCES app.stores;
ALTER TABLE authn.service_bindings ADD FOREIGN KEY(tenant_id) REFERENCES app.stores;
CREATE FUNCTION authn.my_memberships() RETURNS TABLE(tenant_id text,role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,authn AS $$
    SELECT m.tenant_id,m.role FROM authn.memberships m JOIN authn.session_identity() s USING(user_id)
$$;

CREATE FUNCTION app.immutable_record() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN RAISE EXCEPTION 'immutable record' USING ERRCODE='42501'; END $$;
CREATE FUNCTION app.guard_proposal() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF ROW(NEW.tenant_id,NEW.proposal_id,NEW.run_id,NEW.payload,NEW.payload_hash,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.tenant_id,OLD.proposal_id,OLD.run_id,OLD.payload,OLD.payload_hash,OLD.created_at)
    THEN RAISE EXCEPTION 'immutable proposal payload' USING ERRCODE='42501'; END IF;
    IF NEW.status<>OLD.status AND NOT (
        (OLD.status='PENDING' AND NEW.status IN ('APPROVED','REJECTED','STALE','EXPIRED','FAILED_SAFE')) OR
        (OLD.status='APPROVED' AND NEW.status IN ('EXECUTED','STALE','EXPIRED','FAILED_SAFE')))
    THEN RAISE EXCEPTION 'invalid proposal transition' USING ERRCODE='42501'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER proposal_guard BEFORE UPDATE ON app.proposals FOR EACH ROW EXECUTE FUNCTION app.guard_proposal();

DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['policy_versions','approvals','executions','audit_events','thread_records'] LOOP
        EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON app.%I FOR EACH ROW EXECUTE FUNCTION app.immutable_record()',t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['policy_versions','stores','investigations','proposals','approvals','execution_jobs',
                             'executions','purchase_orders','audit_events','idempotency','thread_records'] LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY tenant_read ON app.%I FOR SELECT USING (authn.can_access(tenant_id))',t);
        EXECUTE format('CREATE POLICY owner_all ON app.%I TO ec_owner USING(true) WITH CHECK(true)',t);
    END LOOP;
END $$;
ALTER TABLE evaluator.sandbox_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE evaluator.sandbox_state FORCE ROW LEVEL SECURITY;
CREATE POLICY simulator_all ON evaluator.sandbox_state TO ec_simulator
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY owner_all ON evaluator.sandbox_state TO ec_owner USING(true) WITH CHECK(true);

CREATE POLICY write_investigations ON app.investigations FOR INSERT TO ec_api
    WITH CHECK(authn.is_approver(tenant_id));
CREATE POLICY agent_run ON app.investigations FOR UPDATE TO ec_agent
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY insert_proposal ON app.proposals FOR INSERT TO ec_api,ec_agent
    WITH CHECK(authn.can_access(tenant_id) AND status='PENDING' AND
      (NOT pg_has_role(session_user,'ec_api','MEMBER') OR authn.is_approver(tenant_id)));
CREATE POLICY api_proposal_status ON app.proposals FOR UPDATE TO ec_api
    USING(authn.is_approver(tenant_id)) WITH CHECK(authn.is_approver(tenant_id) AND status IN ('APPROVED','REJECTED'));
CREATE POLICY executor_proposal_status ON app.proposals FOR UPDATE TO ec_executor
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY insert_approval ON app.approvals FOR INSERT TO ec_api
    WITH CHECK(authn.is_approver(tenant_id) AND approver_user_id=(SELECT user_id FROM authn.session_identity())
               AND session_id=(SELECT session_id FROM authn.session_identity()));
CREATE POLICY insert_job ON app.execution_jobs FOR INSERT TO ec_api
    WITH CHECK(authn.is_approver(tenant_id) AND state='QUEUED' AND attempts=0 AND lease_owner IS NULL);
CREATE POLICY executor_jobs ON app.execution_jobs FOR UPDATE TO ec_executor
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY executor_receipt ON app.executions FOR INSERT TO ec_executor
    WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY executor_store ON app.stores FOR UPDATE TO ec_executor,ec_simulator
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY insert_order ON app.purchase_orders FOR INSERT TO ec_executor
    WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY simulator_order ON app.purchase_orders FOR UPDATE TO ec_simulator
    USING(authn.can_access(tenant_id)) WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY insert_audit ON app.audit_events FOR INSERT TO ec_api,ec_agent,ec_executor,ec_simulator
    WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY insert_idempotency ON app.idempotency FOR INSERT TO ec_api,ec_agent
    WITH CHECK(authn.can_access(tenant_id));
CREATE POLICY agent_thread ON app.thread_records FOR INSERT TO ec_agent
    WITH CHECK(authn.can_access(tenant_id));

CREATE FUNCTION authn.set_execution(t text,enabled boolean) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authn,app AS $$
BEGIN
    IF NOT pg_has_role(session_user,'ec_api','MEMBER') OR authn.member_role(t) IS DISTINCT FROM 'admin'
    THEN RAISE EXCEPTION 'admin required' USING ERRCODE='42501'; END IF;
    UPDATE app.stores SET execution_enabled=enabled WHERE tenant_id=t;
    INSERT INTO app.audit_events(tenant_id,event_id,kind,actor_id,data)
    VALUES(t,gen_random_uuid(),'execution_switch',(SELECT user_id FROM authn.session_identity()),jsonb_build_object('enabled',enabled));
    RETURN enabled;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA authn,app,evaluator FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA authn,app FROM PUBLIC;
GRANT USAGE ON SCHEMA app,authn TO ec_api,ec_agent,ec_executor,ec_simulator;
GRANT USAGE ON SCHEMA evaluator TO ec_simulator;
GRANT EXECUTE ON FUNCTION authn.session_identity(),authn.member_role(text),authn.can_access(text),authn.is_approver(text)
    TO ec_api,ec_agent,ec_executor,ec_simulator;
GRANT EXECUTE ON FUNCTION authn.approval_valid(text,text,uuid) TO ec_executor;
GRANT EXECUTE ON FUNCTION authn.set_execution(text,boolean) TO ec_api;
GRANT EXECUTE ON FUNCTION authn.my_memberships() TO ec_api;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO ec_api,ec_agent,ec_executor,ec_simulator;
REVOKE SELECT ON app.approvals,app.idempotency FROM ec_agent;
GRANT INSERT ON app.investigations,app.approvals,app.execution_jobs TO ec_api;
GRANT INSERT ON app.proposals,app.idempotency TO ec_api,ec_agent;
GRANT UPDATE(status) ON app.proposals TO ec_api,ec_executor;
GRANT UPDATE(state) ON app.investigations TO ec_agent;
GRANT UPDATE ON app.stores TO ec_executor,ec_simulator;
GRANT INSERT ON app.executions,app.purchase_orders TO ec_executor;
GRANT UPDATE ON app.purchase_orders TO ec_simulator;
GRANT UPDATE ON app.execution_jobs TO ec_executor;
GRANT INSERT ON app.audit_events TO ec_api,ec_agent,ec_executor,ec_simulator;
GRANT INSERT ON app.thread_records TO ec_agent;
GRANT SELECT,INSERT,UPDATE ON evaluator.sandbox_state TO ec_simulator;
