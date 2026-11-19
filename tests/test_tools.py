from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from ecommerce_agent.config import ProjectConfig, load_config
from ecommerce_agent.contracts import (
    CreatePurchaseOrderAction,
    InventoryItem,
    Product,
    PurchaseOrder,
    SetPriceAction,
    StoreSnapshot,
    Supplier,
)
from ecommerce_agent.controls import DeterministicControl, NumericalTools
from ecommerce_agent.forecasting import SalesHistory, SalesRecord, forecast_demand
from ecommerce_agent.planning import (
    Candidate,
    PlanningModel,
    candidates,
    compare_plans,
    inventory_position,
)
from ecommerce_agent.validation import holding_reserve, validate_plan


def history(skus=("A",), weeks=16, trend=False):
    start = date(2025, 1, 6)
    records = tuple(
        SalesRecord(
            sku=sku,
            date=start + timedelta(days=d),
            sales_units=d // 7 + 1 if trend else (d % 7) + 1,
            price_cents=200,
        )
        for sku in skus
        for d in range(weeks * 7)
    )
    return SalesHistory("store", records, "test-history")


def snapshot(skus=("A",), stock=20, cash=10000):
    return StoreSnapshot(
        tenant_id="store",
        day=1,
        snapshot_version=1,
        policy_version="p1",
        cash_cents=cash,
        products=tuple(
            Product(
                sku=s,
                name=s,
                supplier_id="supplier",
                reference_price_cents=200,
                price_cents=200,
                cost_cents=100,
            )
            for s in skus
        ),
        inventory=tuple(InventoryItem(sku=s, stock=stock, version=1) for s in skus),
        suppliers=(Supplier(supplier_id="supplier", lead_time_days=3),),
    )


def settings(**changes):
    data = load_config().model_dump()
    for name, value in changes.items():
        group, field = name.split("__")
        data[group][field] = value
    return ProjectConfig.model_validate(data)


def test_seasonal_forecasts_repeat_known_weekdays_without_future_actuals():
    h = history()
    cutoff = max(r.date for r in h.records)
    for model in ("seasonal_naive", "weekday_mean_4"):
        result = forecast_demand(h, ["A"], cutoff, model_id=model)[0]
        assert [p.mean_units for p in result.points] == [1, 2, 3, 4, 5, 6, 7] * 2
        assert all(p.lower_80_units == p.mean_units == p.upper_80_units for p in result.points)
        assert result.cutoff == cutoff
    h = history(trend=True)
    cutoff = max(r.date for r in h.records)
    assert (
        forecast_demand(h, ["A"], cutoff, model_id="seasonal_naive")[0].points[0].mean_units == 16
    )
    assert forecast_demand(h, ["A"], cutoff)[0].points[0].mean_units == 14.5


def test_forecast_ignores_future_records_and_future_prices():
    h = history()
    cutoff = max(r.date for r in h.records)
    future = tuple(
        SalesRecord(sku="A", date=cutoff + timedelta(days=d), sales_units=99999, price_cents=1)
        for d in range(1, 15)
    )
    changed = SalesHistory(h.tenant_id, h.records + future, "changed-source")
    assert forecast_demand(changed, ["A"], cutoff) == forecast_demand(h, ["A"], cutoff)


@pytest.mark.parametrize("horizon", [0, 15, True, -1])
def test_forecast_bounds(horizon):
    h = history()
    with pytest.raises(ValueError, match="horizon"):
        forecast_demand(h, ["A"], max(r.date for r in h.records), horizon)


def test_short_history_missing_skus_and_unobserved_cutoff_fail():
    h = history(weeks=1)
    cutoff = max(r.date for r in h.records)
    result = forecast_demand(h, ["A"], cutoff)[0]
    assert "short_history" in result.warnings
    assert "interval_uncalibrated" in result.warnings
    assert all(p.lower_80_units is None and p.upper_80_units is None for p in result.points)
    with pytest.raises(ValueError, match="known SKUs"):
        forecast_demand(h, ["unknown"], cutoff)
    with pytest.raises(ValueError, match="not yet observed"):
        forecast_demand(h, ["A"], cutoff + timedelta(days=1))


def test_only_next_completed_visible_sales_can_be_appended():
    h = history()
    today = max(r.date for r in h.records) + timedelta(days=1)
    rows = [
        {
            "sku": "A",
            "date": today.isoformat(),
            "sales_units": 0,
            "price_cents": 200,
            "ended_without_stock": True,
        }
    ]
    changed = h.append_completed(rows, completed_date=today)
    assert len(h.records) + 1 == len(changed.records)
    assert changed.records[-1].provenance == "simulated"
    assert "sales_may_be_stockout_censored" in forecast_demand(changed, ["A"], today)[0].warnings
    with pytest.raises(ValueError, match="next completed"):
        changed.append_completed(rows, completed_date=today)
    with pytest.raises(ValidationError):
        h.append_completed([dict(rows[0], sales_units=-1)], completed_date=today)
    with pytest.raises(ValueError, match="exactly once"):
        h.append_completed(rows * 2, completed_date=today)


def test_exact_cash_and_public_cooldown_shared_validation():
    config = load_config()
    s = snapshot(stock=0, cash=1012)
    action = CreatePurchaseOrderAction(sku="A", quantity=5)
    assert holding_reserve(s, config, [action]) == 12500
    assert not validate_plan(s, [action], config, expected_snapshot_version=1)["feasible"]
    exact = StoreSnapshot.model_validate(s.model_dump() | {"cash_millicents": 1012500})
    assert validate_plan(exact, [action], config, expected_snapshot_version=1)["feasible"]
    with pytest.raises(ValidationError, match="floor"):
        StoreSnapshot.model_validate(s.model_dump() | {"cash_millicents": 1013000})
    changed = snapshot().model_copy(
        update={
            "day": 4,
            "products": (snapshot().products[0].model_copy(update={"last_price_change_day": 3}),),
        }
    )
    result = validate_plan(
        changed, [SetPriceAction(sku="A", price_cents=210)], config, expected_snapshot_version=1
    )
    assert [v["code"] for v in result["violations"]] == ["cooldown"]
    assert not validate_plan(s, [], config, expected_snapshot_version=True)["feasible"]
    assert not validate_plan(
        s, [], config, expected_snapshot_version=1, expected_policy_version="old"
    )["feasible"]


def test_candidate_cap_pack_constraints_and_existing_inbound():
    h = history()
    s = snapshot(stock=0)
    config = load_config()
    f = forecast_demand(h, ["A"], max(r.date for r in h.records))[0]
    options = candidates(s, f, config)
    assert len(options) <= 18
    assert any(not c.actions for c in options)
    assert all(c.order_units % 5 == 0 and c.order_units <= 200 for c in options)
    assert all(
        validate_plan(s, c.actions, config, expected_snapshot_version=1)["feasible"]
        for c in options
    )
    order = PurchaseOrder(
        order_id="po",
        sku="A",
        supplier_id="supplier",
        quantity=200,
        created_day=1,
        visible_eta_day=4,
        status="pending",
    )
    inbound = s.model_copy(update={"open_purchase_orders": (order,)})
    assert inventory_position(inbound, "A") == 200
    assert all(c.order_units == 0 for c in candidates(inbound, f, config))


def test_planning_profit_uses_holding_and_fee_not_purchase_principal():
    h = history()
    cutoff = max(r.date for r in h.records)
    f = forecast_demand(h, ["A"], cutoff)[0]
    f = f.model_copy(
        update={"points": tuple(p.model_copy(update={"mean_units": 0.0}) for p in f.points)}
    )
    model = PlanningModel(snapshot(stock=10), (f,), load_config())
    c = Candidate("A", 200, 5, (CreatePurchaseOrderAction(sku="A", quantity=5),))
    score = model.score((c,))
    assert score["expected_profit_millicents"] == -519500
    assert score["expected_fill_rate"] is None
    assert score["rollouts"] == 20


def test_planning_rollouts_repeat_and_do_not_mutate_visible_state():
    h = history()
    s = snapshot(stock=2)
    config = load_config()
    f = forecast_demand(h, ["A"], max(r.date for r in h.records))
    original = s.model_dump_json()
    result = compare_plans(s, f, config)
    assert compare_plans(s, f, config) == result
    assert s.model_dump_json() == original
    assert result["validation"]["feasible"]
    assert result["incremental_profit_millicents"] >= 0
    assert len(result["elasticity_sensitivity"]) == 2
    assert result["selected"]["rollouts"] == 20


def test_multi_sku_allocation_respects_shared_budget():
    skus = ("A", "B", "C")
    h = history(skus)
    s = snapshot(skus, stock=0, cash=2000)
    forecasts = forecast_demand(h, list(skus), max(r.date for r in h.records))
    result = compare_plans(s, forecasts, load_config())
    assert result["validation"]["feasible"]
    assert (
        result["validation"]["procurement_spend_cents"] * 1000
        + result["validation"]["holding_reserve_millicents"]
    ) <= 2000000


def test_tools_bind_scope_forecasts_and_consume_failed_attempt_budgets():
    h = history()
    today = max(r.date for r in h.records) + timedelta(days=1)
    with pytest.raises(ValueError, match="tenant"):
        NumericalTools(
            snapshot().model_copy(update={"tenant_id": "other"}), h, today, load_config()
        )
    tools = NumericalTools(snapshot(), h, today, load_config())
    for _ in range(3):
        with pytest.raises(ValueError, match="server-recorded"):
            tools.compare(["A"])
    with pytest.raises(ValueError, match="compare-plans budget"):
        tools.compare(["A"])
    tools = NumericalTools(
        snapshot(), h, today, settings(model__max_tool_calls=1, model__max_compare_calls=1)
    )
    with pytest.raises(ValueError):
        tools.forecast(["missing"])
    with pytest.raises(ValueError, match="budget"):
        tools.get_store_snapshot()


def test_tool_payload_edits_do_not_forge_server_forecasts_and_metadata_is_complete():
    h = history()
    today = max(r.date for r in h.records) + timedelta(days=1)
    tools = NumericalTools(snapshot(), h, today, load_config())
    payload = tools.forecast(["A"])
    original = tools._forecasts["A"].points[0].mean_units
    payload["result"][0]["points"][0]["mean_units"] = 99999
    assert tools._forecasts["A"].points[0].mean_units == original
    assert tools.traces[0]["result"][0]["points"][0]["mean_units"] == original
    result = tools.compare(["A"])
    for key in (
        "tool_call_id",
        "as_of",
        "source_ids",
        "units",
        "warnings",
        "snapshot_version",
        "policy_version",
    ):
        assert key in result


def test_fixed_and_rules_control_produce_valid_evidence_backed_decisions():
    h = history()
    s = snapshot(stock=0)
    for policy, expected_tools in (("rules", 3), ("fixed", 4)):
        control = DeterministicControl(policy, h, load_config())
        actions = control.decide(s)
        assert validate_plan(s, actions, load_config(), expected_snapshot_version=1)["feasible"]
        decision = control.decisions[0]
        assert len(decision["tool_traces"]) == expected_tools
        assert decision["evidence_ids"]
        if policy == "rules":
            assert all(isinstance(a, CreatePurchaseOrderAction) for a in actions)


def test_shared_validator_rejects_extra_fields_unknowns_and_duplicate_actions():
    s = snapshot()
    config = load_config()
    result = validate_plan(
        s,
        [{"action_type": "set_price", "sku": "A", "price_cents": 210, "approved": True}],
        config,
        expected_snapshot_version=1,
    )
    assert result["violations"][0]["code"] == "invalid_schema"
    action = SetPriceAction(sku="A", price_cents=210)
    assert any(
        v["code"] == "duplicate_action"
        for v in validate_plan(s, [action, action], config, expected_snapshot_version=1)[
            "violations"
        ]
    )


def test_tool_reads_are_bounded_and_scoped_to_current_store_products():
    h = history(("A", "B"))
    today = max(r.date for r in h.records) + timedelta(days=1)
    tools = NumericalTools(snapshot(), h, today, load_config())
    with pytest.raises(ValueError, match="current store"):
        tools.get_sales_history(["B"])
    skus = tuple(f"SKU-{i}" for i in range(10))
    h = history(skus)
    today = max(r.date for r in h.records) + timedelta(days=1)
    tools = NumericalTools(snapshot(skus), h, today, load_config())
    with pytest.raises(ValueError, match="64000"):
        tools.get_sales_history(list(skus), lookback_days=365)


def test_supplier_status_returns_only_visible_store_scoped_records():
    h = history()
    today = max(r.date for r in h.records) + timedelta(days=1)
    order = PurchaseOrder(
        order_id="po",
        sku="A",
        supplier_id="supplier",
        quantity=5,
        created_day=1,
        visible_eta_day=4,
        status="pending",
    )
    tools = NumericalTools(
        snapshot().model_copy(update={"open_purchase_orders": (order,)}), h, today, load_config()
    )
    response = tools.get_supplier_status(["supplier"])
    assert response["result"]["pending_orders"][0]["visible_eta_day"] == 4
    assert "actual_arrival_day" not in str(response)
    with pytest.raises(ValueError, match="current store"):
        tools.get_supplier_status(["other-store-supplier"])
