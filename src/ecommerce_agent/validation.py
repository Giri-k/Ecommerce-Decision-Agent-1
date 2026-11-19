"""Pure feasibility rules using visible state only; no authorization or mutation."""

from pydantic import TypeAdapter, ValidationError

from ecommerce_agent.config import ProjectConfig
from ecommerce_agent.contracts import (
    Action,
    CreatePurchaseOrderAction,
    SetPriceAction,
    StoreSnapshot,
)

MILLI = 1000
_ACTIONS = TypeAdapter(tuple[Action, ...])


def normalize_actions(actions):
    values = []
    for action in actions:
        values.append(action.model_dump() if hasattr(action, "model_dump") else action)
    if len(values) > 20:
        raise ValueError("at most twenty actions per bundle")
    return _ACTIONS.validate_python(values)


def holding_reserve(snapshot: StoreSnapshot, config: ProjectConfig, actions=()) -> int:
    products = {p.sku: p for p in snapshot.products}
    suppliers = {s.supplier_id: s for s in snapshot.suppliers}
    horizon = config.simulation.episode_days + config.simulation.continuation_days
    remaining = max(0, horizon - snapshot.day + 1)
    rate = int(config.simulation.daily_holding_rate * MILLI)
    reserve = sum(
        products[i.sku].cost_cents * i.stock * rate * remaining for i in snapshot.inventory
    )
    for order in snapshot.open_purchase_orders:
        if order.status == "pending":
            days = max(0, horizon - max(snapshot.day, order.visible_eta_day) + 1)
            reserve += products[order.sku].cost_cents * order.quantity * rate * days
    for action in actions:
        if isinstance(action, CreatePurchaseOrderAction):
            product = products[action.sku]
            eta = snapshot.day + suppliers[product.supplier_id].lead_time_days
            reserve += product.cost_cents * action.quantity * rate * max(0, horizon - eta + 1)
    return reserve


def validate_plan(
    snapshot: StoreSnapshot,
    actions,
    config: ProjectConfig,
    *,
    expected_snapshot_version: int,
    expected_policy_version: str | None = None,
) -> dict:
    """Return stable violation codes and amounts, scoped to an immutable snapshot."""
    violations = []

    def fail(code, message):
        violations.append({"code": code, "message": message})

    try:
        actions = normalize_actions(actions)
    except (ValidationError, ValueError, TypeError) as exc:
        return {
            "feasible": False,
            "violations": [{"code": "invalid_schema", "message": str(exc)}],
            "procurement_spend_cents": 0,
            "holding_reserve_millicents": 0,
        }
    if (
        type(expected_snapshot_version) is not int
        or expected_snapshot_version != snapshot.snapshot_version
    ):
        fail("stale_snapshot", "stale snapshot")
    if expected_policy_version is not None and expected_policy_version != snapshot.policy_version:
        fail("stale_policy", "stale policy version")
    if actions and (
        snapshot.day > config.simulation.episode_days
        or snapshot.day not in config.simulation.review_days
    ):
        fail("outside_review", "actions are allowed only on scheduled review days")
    products = {p.sku: p for p in snapshot.products}
    policy, seen, spend = config.operating, set(), 0
    for action in actions:
        if action.sku not in products:
            fail("unknown_sku", "unknown SKU")
            continue
        key = action.action_type, action.sku
        if key in seen:
            fail("duplicate_action", "duplicate action for SKU")
        seen.add(key)
        product = products[action.sku]
        if isinstance(action, CreatePurchaseOrderAction):
            if not policy.minimum_order_units <= action.quantity <= policy.maximum_order_units:
                fail("order_cap", "order quantity outside policy limits")
            if action.quantity % policy.case_pack:
                fail("case_pack", "order quantity must use whole case packs")
            spend += action.quantity * product.cost_cents + policy.order_fee_cents
        elif isinstance(action, SetPriceAction):
            if not (
                product.reference_price_cents * policy.minimum_price_percent
                <= action.price_cents * 100
                <= product.reference_price_cents * policy.maximum_price_percent
            ):
                fail("price_range", "price outside policy range")
            if product.cost_cents * 100 > action.price_cents * (
                100 - policy.minimum_gross_margin_percent
            ):
                fail("margin", "price violates minimum gross margin")
            if (
                action.price_cents != product.price_cents
                and product.last_price_change_day is not None
                and snapshot.day - product.last_price_change_day < policy.price_cooldown_days
            ):
                fail("cooldown", "price cooldown has not elapsed")
    reserve = 0
    if not any(v["code"] == "unknown_sku" for v in violations):
        reserve = holding_reserve(snapshot, config, actions)
        cash = (
            snapshot.cash_millicents
            if snapshot.cash_millicents is not None
            else snapshot.cash_cents * MILLI
        )
        if spend * MILLI + reserve > cash:
            fail("cash", "insufficient cash after committed holding reserve")
    return {
        "feasible": not violations,
        "violations": violations,
        "procurement_spend_cents": spend,
        "holding_reserve_millicents": reserve,
        "snapshot_version": snapshot.snapshot_version,
        "policy_version": snapshot.policy_version,
    }


def require_feasible(snapshot, actions, config, *, expected_snapshot_version):
    actions = normalize_actions(actions)
    result = validate_plan(
        snapshot, actions, config, expected_snapshot_version=expected_snapshot_version
    )
    if not result["feasible"]:
        raise ValueError("; ".join(v["message"] for v in result["violations"]))
    return actions
