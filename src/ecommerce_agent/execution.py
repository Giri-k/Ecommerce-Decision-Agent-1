"""Public-state action application, used only inside an authorized transaction."""

from uuid import NAMESPACE_URL, uuid5

from ecommerce_agent.contracts import (
    CreatePurchaseOrderAction,
    PurchaseOrder,
    SetPriceAction,
    StoreSnapshot,
)
from ecommerce_agent.validation import MILLI, require_feasible


def apply_actions(snapshot, actions, config, proposal_id):
    actions = require_feasible(
        snapshot, actions, config, expected_snapshot_version=snapshot.snapshot_version
    )
    products = {p.sku: p for p in snapshot.products}
    suppliers = {s.supplier_id: s for s in snapshot.suppliers}
    orders = list(snapshot.open_purchase_orders)
    cash = (
        snapshot.cash_millicents
        if snapshot.cash_millicents is not None
        else snapshot.cash_cents * MILLI
    )
    new_orders, prices, spend = [], [], 0
    for action in actions:
        product = products[action.sku]
        if isinstance(action, SetPriceAction) and action.price_cents != product.price_cents:
            products[action.sku] = product.model_copy(
                update={"price_cents": action.price_cents, "last_price_change_day": snapshot.day}
            )
            prices.append(
                {
                    "sku": action.sku,
                    "before_cents": product.price_cents,
                    "after_cents": action.price_cents,
                }
            )
        elif isinstance(action, CreatePurchaseOrderAction):
            charge = product.cost_cents * action.quantity + config.operating.order_fee_cents
            spend += charge
            cash -= charge * MILLI
            order = PurchaseOrder(
                order_id="po-"
                + str(uuid5(NAMESPACE_URL, f"{snapshot.tenant_id}:{proposal_id}:{action.sku}")),
                sku=action.sku,
                supplier_id=product.supplier_id,
                quantity=action.quantity,
                created_day=snapshot.day,
                visible_eta_day=snapshot.day + suppliers[product.supplier_id].lead_time_days,
                status="pending",
            )
            new_orders.append(order)
            orders.append(order)
    updated = StoreSnapshot.model_validate(
        snapshot.model_dump()
        | {
            "products": tuple(products.values()),
            "open_purchase_orders": tuple(orders),
            "cash_cents": cash // MILLI,
            "cash_millicents": cash,
            "snapshot_version": snapshot.snapshot_version + 1,
        }
    )
    return updated, {
        "new_orders": [o.model_dump(mode="json") for o in new_orders],
        "price_changes": prices,
        "procurement_spend_cents": spend,
    }
