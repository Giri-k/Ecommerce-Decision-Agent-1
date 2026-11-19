-- Only released, fulfilled sales are operational observations. Latent demand stays private.
CREATE TABLE app.observed_sales (
    tenant_id text NOT NULL REFERENCES app.stores,
    day integer NOT NULL CHECK(day>0), sku text NOT NULL, date date NOT NULL,
    sales_units integer NOT NULL CHECK(sales_units>=0),
    price_cents integer NOT NULL CHECK(price_cents>0), ended_without_stock boolean NOT NULL,
    PRIMARY KEY(tenant_id,day,sku)
);
ALTER TABLE app.observed_sales ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.observed_sales FORCE ROW LEVEL SECURITY;
CREATE POLICY read_scope ON app.observed_sales FOR SELECT USING(authn.can_access(tenant_id));
CREATE POLICY owner_all ON app.observed_sales TO ec_owner USING(true) WITH CHECK(true);
CREATE POLICY simulator_release ON app.observed_sales FOR INSERT TO ec_simulator
    WITH CHECK(authn.can_access(tenant_id));
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON app.observed_sales
    FOR EACH ROW EXECUTE FUNCTION app.immutable_record();
REVOKE ALL ON app.observed_sales FROM PUBLIC;
GRANT SELECT ON app.observed_sales TO ec_api,ec_agent,ec_executor,ec_simulator;
GRANT INSERT ON app.observed_sales TO ec_simulator;
