-- API can lock a permitted store without gaining UPDATE privileges on its state.
CREATE FUNCTION authn.lock_store(t text) RETURNS SETOF app.stores
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,authn,app AS $$
BEGIN
    IF NOT authn.can_access(t) THEN
        RAISE EXCEPTION 'store access denied' USING ERRCODE='42501';
    END IF;
    RETURN QUERY SELECT * FROM app.stores WHERE tenant_id=t FOR UPDATE;
END $$;
REVOKE ALL ON FUNCTION authn.lock_store(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authn.lock_store(text) TO ec_api,ec_agent,ec_executor,ec_simulator;
