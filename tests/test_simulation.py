import csv
import json
import math
import stat
from dataclasses import replace
from datetime import date, timedelta
from fractions import Fraction

import pytest

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
from ecommerce_agent.data import TRAIN_END, checksum
from ecommerce_agent.simulation import (
    MILLI,
    Initialization,
    PrivateOrder,
    Scenario,
    Simulator,
    World,
    initialize_store,
    negative_binomial_quantile,
    run_episode,
    uniform,
)


def settings(**changes):
    data = load_config().model_dump()
    for key, value in changes.items():
        section, name = key.split("__")
        data[section][name] = value
    return ProjectConfig.model_validate(data)


def initial(stock=10, cash=10000, rates=0, scenario=None, skus=("A",), orders=(), cost=100):
    scenario = scenario or Scenario()
    products = tuple(
        Product(
            sku=sku,
            name=sku,
            supplier_id="supplier-fast",
            reference_price_cents=200,
            price_cents=200,
            cost_cents=cost,
        )
        for sku in skus
    )
    snapshot = StoreSnapshot(
        tenant_id="test-store",
        day=1,
        snapshot_version=1,
        policy_version="test-policy",
        cash_cents=cash,
        products=products,
        inventory=tuple(InventoryItem(sku=sku, stock=stock, version=1) for sku in skus),
        suppliers=(Supplier(supplier_id="supplier-fast", lead_time_days=3),),
        open_purchase_orders=tuple(o.visible for o in orders),
    )
    world = World(
        scenario, date(2026, 1, 1), {sku: (rates,) * 7 for sku in skus}, {sku: -1.0 for sku in skus}
    )
    return Initialization(snapshot, world, tuple(orders))


def finish(engine):
    while not engine.complete:
        engine.step()
    return engine.metrics()


def act(engine, actions):
    version = engine.observe().snapshot_version
    engine.step(actions, expected_snapshot_version=version)


def test_hand_calculated_sales_cash_cogs_and_subcent_holding(monkeypatch):
    engine = Simulator(initial())
    monkeypatch.setattr(engine, "_demand", lambda sku: 5)
    engine.step()
    row = engine.rows[0]
    assert row["fulfilled_units"] == 5
    assert row["end_stock"] == 5
    assert row["revenue_cents"] == 1000
    assert row["cogs_cents"] == 500
    assert row["holding_millicents"] == 500
    assert row["profit_millicents"] == 499500
    assert engine.days[0]["cash_millicents"] == 10999500


def test_order_principal_not_double_counted_and_receipt_applied_once():
    engine = Simulator(initial())
    act(engine, [CreatePurchaseOrderAction(sku="A", quantity=5)])
    assert engine.rows[0]["profit_millicents"] == -501000  # Fee and holding, no principal.
    assert engine.days[0]["cash_millicents"] == 8999000
    engine.step()
    engine.step()
    assert engine.observe().day == 4
    assert engine.observe().inventory[0].stock == 15
    assert engine.observe().inventory[0].stock == 15
    result = finish(engine)
    assert sum(r["received_units"] for r in engine.rows) == 5
    assert result["extended"]["purchase_principal_cents"] == 500
    assert result["extended"]["order_fee_cents"] == 500
    assert result["extended"]["holding_millicents"] == 40500
    assert result["extended"]["operating_profit_millicents"] == -540500
    assert result["ending_inventory_value_cents"] == 1500
    # Change in cash + stock value equals profit (all ordered units have arrived).
    assert result["ending_cash_millicents"] + 1500 * MILLI - 11000 * MILLI == -540500


def test_unmet_demand_is_lost_and_never_becomes_negative_stock(monkeypatch):
    engine = Simulator(initial(stock=2))
    monkeypatch.setattr(engine, "_demand", lambda sku: 5)
    engine.step()
    engine.step()
    assert [(r["fulfilled_units"], r["lost_units"], r["end_stock"]) for r in engine.rows] == [
        (2, 3, 0),
        (0, 5, 0),
    ]


def test_atomic_bundle_and_shared_cash_contention():
    engine = Simulator(initial(stock=0, cash=1100, skus=("A", "B")))
    snapshot = engine.observe()
    actions = [
        SetPriceAction(sku="A", price_cents=210),
        CreatePurchaseOrderAction(sku="A", quantity=5),
        CreatePurchaseOrderAction(sku="B", quantity=5),
    ]
    with pytest.raises(ValueError, match="insufficient cash"):
        engine.step(actions, expected_snapshot_version=snapshot.snapshot_version)
    assert engine.observe() == snapshot
    assert not engine.rows
    act(engine, [CreatePurchaseOrderAction(sku="A", quantity=5)])
    engine.step()
    engine.step()
    before = engine.observe()
    with pytest.raises(ValueError, match="insufficient cash"):
        engine.step(
            [CreatePurchaseOrderAction(sku="B", quantity=5)],
            expected_snapshot_version=before.snapshot_version,
        )
    assert engine.observe() == before


@pytest.mark.parametrize(
    "action,message",
    [
        (CreatePurchaseOrderAction(sku="missing", quantity=5), "unknown SKU"),
        (CreatePurchaseOrderAction(sku="A", quantity=6), "case packs"),
        (CreatePurchaseOrderAction(sku="A", quantity=205), "limits"),
        (SetPriceAction(sku="A", price_cents=300), "range"),
    ],
)
def test_policy_violation_has_no_action_effects(action, message):
    engine = Simulator(initial())
    before = engine.observe()
    with pytest.raises(ValueError, match=message):
        engine.step([action], expected_snapshot_version=before.snapshot_version)
    assert engine.observe() == before


def test_duplicate_stale_and_forged_actions_rejected():
    engine = Simulator(initial())
    action = CreatePurchaseOrderAction(sku="A", quantity=5)
    with pytest.raises(ValueError, match="stale"):
        engine.step([action], expected_snapshot_version=0)
    with pytest.raises(ValueError, match="duplicate"):
        act(engine, [action, action])
    forged = CreatePurchaseOrderAction.model_construct(sku="A", quantity=True)
    with pytest.raises(ValueError):
        act(engine, [forged])
    assert not engine.rows


def test_margin_and_cooldown_enforced():
    engine = Simulator(initial(cost=150))
    with pytest.raises(ValueError, match="margin"):
        act(engine, [SetPriceAction(sku="A", price_cents=180)])
    engine = Simulator(initial(), settings(simulation__review_days=(1, 2, 4)))
    act(engine, [SetPriceAction(sku="A", price_cents=210)])
    with pytest.raises(ValueError, match="cooldown"):
        act(engine, [SetPriceAction(sku="A", price_cents=200)])
    engine.step()
    engine.step()
    act(engine, [SetPriceAction(sku="A", price_cents=200)])


def test_continuation_keeps_prices_and_forbids_new_decisions():
    engine = Simulator(initial())
    act(engine, [SetPriceAction(sku="A", price_cents=210)])
    for _ in range(13):
        engine.step()
    assert engine.observe().day == 15
    with pytest.raises(ValueError, match="review days"):
        act(engine, [CreatePurchaseOrderAction(sku="A", quantity=5)])
    finish(engine)
    assert len(engine.rows) == 28
    assert all(r["price_cents"] == 210 for r in engine.rows)
    with pytest.raises(ValueError, match="finished"):
        engine.step()


def test_delay_hidden_until_notice_and_receipt_on_actual_day():
    order = PrivateOrder(
        PurchaseOrder(
            order_id="opening",
            sku="A",
            supplier_id="supplier-fast",
            quantity=5,
            created_day=1,
            visible_eta_day=4,
            status="pending",
        ),
        actual_arrival_day=7,
        notice_day=4,
    )
    engine = Simulator(initial(orders=[order]))
    for _ in range(3):
        assert engine.observe().open_purchase_orders[0].visible_eta_day == 4
        engine.step()
    assert engine.observe().open_purchase_orders[0].visible_eta_day == 7
    assert engine.observe().inventory[0].stock == 10
    for _ in range(3):
        engine.step()
    assert engine.observe().day == 7
    assert engine.observe().inventory[0].stock == 15
    assert not engine.observe().open_purchase_orders
    finish(engine)
    assert engine.rows[6]["received_units"] == 5
    assert sum(r["received_units"] for r in engine.rows) == 5


def test_pending_prepaid_inventory_has_no_arbitrary_liquidation_gain():
    order = PrivateOrder(
        PurchaseOrder(
            order_id="opening",
            sku="A",
            supplier_id="supplier-fast",
            quantity=5,
            created_day=1,
            visible_eta_day=40,
            status="pending",
        ),
        40,
    )
    result = finish(Simulator(initial(stock=0, orders=[order])))
    assert result["outstanding_order_count"] == 1
    assert result["prepaid_pipeline_value_cents"] == 500
    assert result["unpaid_committed_spend_cents"] == 0
    assert result["extended"]["operating_profit_millicents"] == 0


def test_keyed_randomness_and_quantile_against_geometric_closed_form():
    u = uniform(123, "sku", 1, "demand")
    assert 0 < u < 1
    assert uniform(123, "sku", 1, "demand") == u
    assert uniform(123, "sku", 2, "demand") != u
    for mean in (0.1, 1, 10, 100):
        for u in (0.01, 0.4, 0.9, 0.999):
            expected = math.floor(math.log1p(-u) / math.log(mean / (1 + mean)))
            assert negative_binomial_quantile(u, mean, 1) == expected
    assert negative_binomial_quantile(0.99, 0, 10) == 0


@pytest.mark.parametrize(
    "u,mean,k", [(0, 1, 10), (1, 1, 10), (0.5, -1, 10), (0.5, 1, 0), (0.5, float("nan"), 10)]
)
def test_invalid_distribution_parameters_fail_explicitly(u, mean, k):
    with pytest.raises(ValueError):
        negative_binomial_quantile(u, mean, k)


def test_default_and_stress_quantiles_against_exact_closed_form_cdf():
    # An independent rational-arithmetic oracle, rather than the production recurrence.
    for k in (3, 10, 30):
        for mean in (Fraction(1, 2), Fraction(4), Fraction(20)):
            p = Fraction(k) / (k + mean)
            for u in (Fraction(1, 100), Fraction(1, 2), Fraction(95, 100)):
                q = negative_binomial_quantile(float(u), float(mean), k)
                cdf = sum(
                    Fraction(math.comb(n + k - 1, n)) * p**k * (1 - p) ** n for n in range(q + 1)
                )
                pmf_q = Fraction(math.comb(q + k - 1, q)) * p**k * (1 - p) ** q
                assert cdf - pmf_q < u <= cdf


def test_policies_share_latent_draws_despite_different_order_counts():
    a, b = Simulator(initial(rates=5)), Simulator(initial(rates=5))
    act(a, [CreatePurchaseOrderAction(sku="A", quantity=5)])
    b.step()
    finish(a)
    finish(b)
    assert [r["latent_demand"] for r in a.rows] == [r["latent_demand"] for r in b.rows]


def test_price_response_uses_same_uniform_coupling_across_seeded_runs():
    for seed in range(30):
        a = Simulator(initial(stock=100, rates=10, scenario=Scenario(seed=seed)))
        b = Simulator(initial(stock=100, rates=10, scenario=Scenario(seed=seed)))
        a.step()
        act(b, [SetPriceAction(sku="A", price_cents=210)])
        assert b.rows[0]["latent_demand"] <= a.rows[0]["latent_demand"]


def test_holding_reserve_and_early_metrics_release_blocked():
    with pytest.raises(ValueError, match="holding costs"):
        Simulator(initial(cash=0))
    engine = Simulator(initial(stock=0, cash=1000))
    with pytest.raises(ValueError, match="holding reserve"):
        act(engine, [CreatePurchaseOrderAction(sku="A", quantity=5)])
    with pytest.raises(ValueError, match="after the episode"):
        engine.metrics()


@pytest.fixture
def curated(tmp_path):
    root = tmp_path / "curated"
    root.mkdir()
    products = [
        {"sku": f"SKU-{i:02d}", "category": "HOUSEHOLD", "reference_price_cents": 200}
        for i in range(10)
    ]
    (root / "product_manifest.json").write_text(
        json.dumps({"selection_cutoff_day": TRAIN_END, "products": products})
    )
    origin = date(2011, 1, 29)
    with (root / "sales_history.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["sku", "day", "date", "sales_units"])
        writer.writeheader()
        for product in products:
            for day in range(1, TRAIN_END + 1):
                writer.writerow(
                    {
                        "sku": product["sku"],
                        "day": day,
                        "date": str(origin + timedelta(days=day - 1)),
                        "sales_units": 1,
                    }
                )
            writer.writerow(
                {
                    "sku": product["sku"],
                    "day": 1802,
                    "date": "future",
                    "sales_units": "not-a-number",
                }
            )
    for name in ("split_manifest.json", "data_quality.json"):
        (root / name).write_text("{}")
    names = (
        "product_manifest.json",
        "sales_history.csv",
        "split_manifest.json",
        "data_quality.json",
    )
    (root / "manifest.json").write_text(
        json.dumps({"artifacts": {n: checksum(root / n) for n in names}})
    )
    return root


def test_initialization_uses_training_only_and_checks_source_integrity(curated):
    first = initialize_store(curated)
    assert initialize_store(curated) == first
    assert all(rates == (1.0,) * 7 for rates in first.world.weekday_rates.values())
    assert len(first.snapshot.products) == 10
    assert all(7 <= i.stock <= 21 for i in first.snapshot.inventory)
    assert all(100 <= p.cost_cents <= 140 for p in first.snapshot.products)
    p = curated / "sales_history.csv"
    p.write_text(p.read_text() + "tampered")
    with pytest.raises(ValueError, match="checksum"):
        initialize_store(curated)


def test_opening_orders_paid_once_and_normal_has_no_opening_orders(curated):
    normal = initialize_store(curated)
    assert normal.snapshot.cash_cents == 200000
    assert not normal.orders
    delay = initialize_store(curated, Scenario(family="supplier_delay"))
    assert (
        delay.snapshot.cash_cents
        + delay.opening_paid_principal_cents
        + delay.opening_paid_fees_cents
        == 200000
    )
    result = finish(Simulator(delay))
    assert result["extended"]["purchase_principal_cents"] == 0
    assert result["extended"]["order_fee_cents"] == 0


@pytest.mark.parametrize("family", ["normal", "demand_surge", "supplier_delay"])
def test_episode_artifacts_reproduce_and_public_data_has_no_hidden_truth(curated, tmp_path, family):
    a, b = tmp_path / "a", tmp_path / "b"
    scenario = Scenario(family=family)
    run_episode(curated, a, scenario, policy="scripted")
    run_episode(curated, b, scenario, policy="scripted")
    for path in a.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (b / path.relative_to(a)).read_bytes()
    public_content = "".join(p.read_text() for p in (a / "public").iterdir())
    for forbidden in (
        "seed",
        "latent_demand",
        "lost_units",
        "elasticities",
        "weekday_rates",
        "actual_arrival_day",
    ):
        assert forbidden not in public_content
    assert stat.S_IMODE((a / "private").stat().st_mode) == 0o700
    metrics = json.loads((a / "private/metrics.json").read_text())
    assert metrics["extended"]["purchase_principal_cents"] > 0
    assert metrics["extended"]["order_fee_cents"] == 500
    final = StoreSnapshot.model_validate_json((a / "public/final_snapshot.json").read_text())
    assert final.day == 29
    with pytest.raises(ValueError, match="separate"):
        run_episode(curated, curated)


def test_broken_private_opening_order_and_config_ranges_rejected():
    order = PrivateOrder(
        PurchaseOrder(
            order_id="bad",
            sku="A",
            supplier_id="supplier-fast",
            quantity=5,
            created_day=1,
            visible_eta_day=4,
            status="pending",
        ),
        2,
    )
    with pytest.raises(ValueError, match="lead time"):
        Simulator(initial(orders=[order]))
    data = initial()
    with pytest.raises(ValueError, match="calibration"):
        Simulator(replace(data, world=replace(data.world, weekday_rates={})))
    for kwargs in (
        {"simulation__elasticity_min": "-0.1"},
        {"simulation__initial_inventory_days_min": 30},
        {"simulation__unit_cost_percent_max": 90},
    ):
        with pytest.raises(ValueError):
            settings(**kwargs)


def test_exact_fractional_cash_is_preserved_by_shared_execution_validator():
    data = initial(stock=0, cash=1012)
    snapshot = data.snapshot.model_copy(update={"cash_millicents": 1012500})
    engine = Simulator(replace(data, snapshot=snapshot))
    act(engine, [CreatePurchaseOrderAction(sku="A", quantity=5)])
    result = finish(engine)
    assert result["ending_cash_millicents"] == 0
    assert result["extended"]["operating_profit_millicents"] == -512500


@pytest.mark.parametrize("family", ["normal", "demand_surge", "supplier_delay"])
def test_both_controls_finish_matched_episodes_with_public_decision_evidence(
    curated, tmp_path, family
):
    scenario = Scenario(scenario_id=f"dev-{family}", family=family)
    snapshots = []
    for policy, calls in (("rules", 3), ("fixed", 4)):
        target = tmp_path / policy
        result = run_episode(curated, target, scenario, policy)
        assert result["days"] == 28
        snapshots.append((target / "public/initial_snapshot.json").read_bytes())
        decisions = json.loads((target / "public/control_decisions.json").read_text())
        assert [d["day"] for d in decisions] == [1, 4, 7, 10]
        assert all(len(d["tool_traces"]) == calls for d in decisions)
        assert all(d["tool_traces"][-1]["result"]["feasible"] for d in decisions)
        public = "".join(p.read_text() for p in (target / "public").iterdir())
        for forbidden in ("actual_arrival_day", "latent_demand", "elasticities", '"seed"'):
            assert forbidden not in public
    assert snapshots[0] == snapshots[1]


def test_control_cannot_see_changes_to_private_future_state(curated):
    from ecommerce_agent.controls import DeterministicControl
    from ecommerce_agent.forecasting import load_history

    first = initialize_store(curated)
    changed_world = replace(
        first.world,
        scenario=Scenario(seed=999),
        elasticities={sku: -3.0 for sku in first.world.elasticities},
    )
    other = replace(first, world=changed_world)
    source = load_history(curated, tenant_id=first.snapshot.tenant_id)
    for policy in ("rules", "fixed"):
        a = DeterministicControl(policy, source, load_config())
        b = DeterministicControl(policy, source, load_config())
        assert a.decide(Simulator(first).observe()) == b.decide(Simulator(other).observe())
        assert a.decisions == b.decisions


def test_reused_output_directory_does_not_retain_old_control_decisions(curated, tmp_path):
    target = tmp_path / "run"
    run_episode(curated, target, policy="fixed")
    assert (target / "public/control_decisions.json").exists()
    (target / "notes.txt").write_text("user notes")
    run_episode(curated, target, policy="no-op")
    assert not (target / "public/control_decisions.json").exists()
    manifest = json.loads((target / "manifest.json").read_text())
    assert "public/control_decisions.json" not in manifest["artifacts"]
    assert "notes.txt" not in manifest["artifacts"]
    assert (target / "notes.txt").read_text() == "user notes"
