"""Evaluator-only JSON checkpoints and replay of already committed action receipts."""

from datetime import date

from ecommerce_agent.config import ProjectConfig
from ecommerce_agent.contracts import Product, PurchaseOrder, StoreSnapshot
from ecommerce_agent.sampling import uniform
from ecommerce_agent.simulation import Initialization, PrivateOrder, Scenario, Simulator, World


def encode_order(order):
    return {
        "visible": order.visible.model_dump(mode="json"),
        "actual_arrival_day": order.actual_arrival_day,
        "notice_day": order.notice_day,
    }


def decode_order(value):
    return PrivateOrder(
        PurchaseOrder.model_validate(value["visible"]),
        value["actual_arrival_day"],
        value["notice_day"],
    )


def checkpoint(engine):
    return {
        "version": "simulator-checkpoint-1",
        "config": engine.config.model_dump(mode="json"),
        "initial": {
            "snapshot": engine.initial.snapshot.model_dump(mode="json"),
            "orders": [encode_order(o) for o in engine.initial.orders],
            "opening_paid_principal_cents": engine.initial.opening_paid_principal_cents,
            "opening_paid_fees_cents": engine.initial.opening_paid_fees_cents,
        },
        "world": {
            "scenario": vars(engine._world.scenario),
            "first_date": str(engine._world.first_date),
            "weekday_rates": engine._world.weekday_rates,
            "elasticities": engine._world.elasticities,
        },
        "current": {
            "day": engine._day,
            "version": engine._version,
            "cash": engine._cash,
            "products": [p.model_dump(mode="json") for p in engine._products.values()],
            "stock": engine._stock,
            "inventory_versions": engine._inventory_versions,
            "orders": [encode_order(o) for o in engine._orders.values()],
            "begun": engine._begun,
            "received": engine._received,
            "sold": engine._sold,
            "updates": engine._updates,
            "receipts_today": engine._receipts_today,
        },
        "rows": engine.rows,
        "days": engine.days,
        "sales_observations": engine.sales_observations,
        "observations": engine.observations,
    }


def restore(value):
    if value["version"] != "simulator-checkpoint-1":
        raise ValueError("unsupported private checkpoint")
    raw = value["world"]
    world = World(
        Scenario(**raw["scenario"]),
        date.fromisoformat(raw["first_date"]),
        {sku: tuple(rates) for sku, rates in raw["weekday_rates"].items()},
        raw["elasticities"],
    )
    raw = value["initial"]
    initial = Initialization(
        StoreSnapshot.model_validate(raw["snapshot"]),
        world,
        tuple(decode_order(o) for o in raw["orders"]),
        raw["opening_paid_principal_cents"],
        raw["opening_paid_fees_cents"],
    )
    engine = Simulator(initial, ProjectConfig.model_validate(value["config"]))
    raw = value["current"]
    engine._day, engine._version, engine._cash = raw["day"], raw["version"], raw["cash"]
    engine._products = {p["sku"]: Product.model_validate(p) for p in raw["products"]}
    engine._stock, engine._inventory_versions = (
        raw["stock"].copy(),
        raw["inventory_versions"].copy(),
    )
    engine._orders = {o["visible"]["order_id"]: decode_order(o) for o in raw["orders"]}
    engine._begun, engine._received, engine._sold = (
        raw["begun"],
        raw["received"].copy(),
        raw["sold"].copy(),
    )
    engine._updates, engine._receipts_today = list(raw["updates"]), raw["receipts_today"].copy()
    for field in ("rows", "days", "sales_observations", "observations"):
        setattr(engine, field, list(value[field]))
    engine.check_invariants()
    return engine


def reconcile_receipts(engine, receipts):
    """Bring the private engine up to the authoritative committed public snapshot."""
    totals = {"purchases": {}, "fees": {}, "ordered": {}}
    for receipt in receipts:
        before = StoreSnapshot.model_validate(receipt["before"])
        after = StoreSnapshot.model_validate(receipt["after"])
        if engine._snapshot() != before or after.day != before.day:
            raise ValueError("receipt replay does not match private checkpoint")
        for raw in receipt["effects"]["new_orders"]:
            visible = PurchaseOrder.model_validate(raw)
            scenario = engine._world.scenario
            draw = uniform(
                scenario.seed,
                scenario.scenario_id,
                scenario.replicate,
                visible.supplier_id,
                visible.created_day,
                "supplier_delay",
            )
            delayed = (
                scenario.family == "supplier_delay"
                and visible.supplier_id == "supplier-fast"
                and visible.visible_eta_day >= scenario.onset_day
                and draw < 1
            )
            actual = visible.visible_eta_day + (scenario.delay_days if delayed else 0)
            engine._orders[visible.order_id] = PrivateOrder(
                visible,
                actual,
                visible.visible_eta_day if actual != visible.visible_eta_day else None,
            )
            cost = engine._products[visible.sku].cost_cents
            for name, amount in (
                ("purchases", visible.quantity * cost),
                ("fees", engine.config.operating.order_fee_cents),
                ("ordered", visible.quantity),
            ):
                totals[name][visible.sku] = totals[name].get(visible.sku, 0) + amount
        engine._products = {p.sku: p for p in after.products}
        engine._cash = (
            after.cash_millicents if after.cash_millicents is not None else after.cash_cents * 1000
        )
        engine._version = after.snapshot_version
        if {i.sku: i.stock for i in after.inventory} != engine._stock:
            raise ValueError("execution unexpectedly changed physical stock")
    engine._external_action_totals = totals
