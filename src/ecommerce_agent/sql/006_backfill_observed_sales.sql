-- Preserve already completed observations when upgrading an existing local checkpoint.
INSERT INTO app.observed_sales
    (tenant_id,day,sku,date,sales_units,price_cents,ended_without_stock)
SELECT s.tenant_id,(r->>'day')::integer,r->>'sku',(r->>'date')::date,
    (r->>'sales_units')::integer,(r->>'price_cents')::integer,
    (r->>'ended_without_stock')::boolean
FROM evaluator.sandbox_state s,
    LATERAL jsonb_array_elements(s.checkpoint->'sales_observations') r
ON CONFLICT(tenant_id,day,sku) DO NOTHING;
