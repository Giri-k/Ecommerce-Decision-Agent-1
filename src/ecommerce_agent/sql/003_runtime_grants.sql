GRANT SELECT ON app.idempotency TO ec_agent;
REVOKE UPDATE ON app.stores FROM ec_executor,ec_simulator;
GRANT UPDATE(snapshot) ON app.stores TO ec_executor,ec_simulator;
REVOKE UPDATE ON app.purchase_orders FROM ec_simulator;
GRANT UPDATE(data) ON app.purchase_orders TO ec_simulator;
REVOKE UPDATE ON app.execution_jobs FROM ec_executor;
GRANT UPDATE(state,lease_owner,lease_expires_at,heartbeat_at,attempts,ready_at,last_error)
    ON app.execution_jobs TO ec_executor;
REVOKE UPDATE ON app.sandbox_jobs FROM ec_simulator;
GRANT UPDATE(state,lease_owner,lease_expires_at,heartbeat_at,attempts,ready_at,last_error,result)
    ON app.sandbox_jobs TO ec_simulator;
ALTER TABLE app.purchase_orders ADD CHECK(data->>'sku'=sku AND data->>'order_id'=order_id);
ALTER TABLE app.thread_records ADD CHECK(sequence>0);
ALTER TABLE authn.sessions ADD CHECK(length(token_hash)=64);
