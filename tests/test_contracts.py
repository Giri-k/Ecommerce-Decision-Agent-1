"""Contract boundary checks, independent of a database or live model."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ecommerce_agent.contracts import (
    CreatePurchaseOrderAction,
    InventoryItem,
    Product,
    ProposalPayload,
    PurchaseOrder,
    SetPriceAction,
    StoreSnapshot,
    Supplier,
    payload_hash,
)


def snapshot_data() -> dict:
    return {
        "tenant_id": "tenant-a",
        "day": 1,
        "snapshot_version": 1,
        "policy_version": "policy-v1",
        "cash_cents": 200_000,
        "suppliers": [{"supplier_id": "supplier-a", "lead_time_days": 3}],
        "products": [
            {
                "sku": "SKU-1",
                "name": "Synthetic notebook",
                "supplier_id": "supplier-a",
                "reference_price_cents": 1000,
                "price_cents": 1000,
                "cost_cents": 600,
            }
        ],
        "inventory": [{"sku": "SKU-1", "stock": 10, "version": 1}],
    }


def proposal_data() -> dict:
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    return {
        "tenant_id": "tenant-a",
        "run_id": "run-1",
        "snapshot_version": 1,
        "policy_version": "policy-v1",
        "simulated_day": 1,
        "created_at": now,
        "expires_at": now + timedelta(minutes=15),
        "evidence_ids": ["forecast-1", "comparison-1"],
        "actions": [
            {"action_type": "create_purchase_order", "sku": "SKU-1", "quantity": 10},
            {"action_type": "set_price", "sku": "SKU-1", "price_cents": 1050},
        ],
    }


@pytest.mark.parametrize("value", [-1, True, "10", 1.5])
def test_inventory_rejects_invalid_or_coerced_quantities(value: object) -> None:
    with pytest.raises(ValidationError):
        InventoryItem(sku="SKU-1", stock=value, version=1)


@pytest.mark.parametrize("field", ["stock", "version"])
def test_inventory_integer_boundaries(field: str) -> None:
    values = {"sku": "SKU-1", "stock": 0, "version": 1}
    assert InventoryItem(**values).stock == 0
    values[field] = -1 if field == "stock" else 0
    with pytest.raises(ValidationError):
        InventoryItem(**values)


@pytest.mark.parametrize("extra", [{"future_demand": 100}, {"true_elasticity": -1.5}])
def test_snapshot_forbids_private_or_unknown_fields(extra: dict) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        StoreSnapshot.model_validate(snapshot_data() | extra)


def test_snapshot_checks_unique_ids_and_inventory_membership() -> None:
    data = snapshot_data()
    data["products"].append(data["products"][0].copy())
    with pytest.raises(ValidationError, match="duplicate product SKU"):
        StoreSnapshot.model_validate(data)

    data = snapshot_data()
    data["inventory"] = []
    with pytest.raises(ValidationError, match="exactly one entry"):
        StoreSnapshot.model_validate(data)

    data = snapshot_data()
    data["inventory"][0]["sku"] = "OTHER-SKU"
    with pytest.raises(ValidationError, match="exactly one entry"):
        StoreSnapshot.model_validate(data)


def test_snapshot_checks_supplier_and_order_references() -> None:
    data = snapshot_data()
    data["products"][0]["supplier_id"] = "unknown-supplier"
    with pytest.raises(ValidationError, match="unknown supplier"):
        StoreSnapshot.model_validate(data)

    order = {
        "order_id": "order-1",
        "sku": "UNKNOWN-SKU",
        "supplier_id": "supplier-a",
        "quantity": 10,
        "created_day": 1,
        "visible_eta_day": 4,
        "status": "pending",
    }
    data = snapshot_data() | {"open_purchase_orders": [order]}
    with pytest.raises(ValidationError, match="unknown SKU"):
        StoreSnapshot.model_validate(data)

    order["sku"] = "SKU-1"
    order["supplier_id"] = "supplier-b"
    data["suppliers"].append({"supplier_id": "supplier-b", "lead_time_days": 7})
    with pytest.raises(ValidationError, match="differs from the product supplier"):
        StoreSnapshot.model_validate(data)


def test_snapshot_nested_state_is_frozen_and_json_round_trips() -> None:
    snapshot = StoreSnapshot.model_validate(snapshot_data())
    assert isinstance(snapshot.inventory, tuple)
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.inventory[0].stock = 100
    assert StoreSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


@pytest.mark.parametrize(
    "factory,values",
    [
        (Supplier, {"supplier_id": "", "lead_time_days": 3}),
        (Supplier, {"supplier_id": "supplier-a", "lead_time_days": True}),
        (SetPriceAction, {"sku": "SKU-1", "price_cents": 0}),
        (SetPriceAction, {"sku": "SKU-1", "price_cents": "1000"}),
        (CreatePurchaseOrderAction, {"sku": "SKU-1", "quantity": -5}),
        (CreatePurchaseOrderAction, {"sku": "SKU-1", "quantity": True}),
    ],
)
def test_identifiers_and_action_values_are_validated(factory: type, values: dict) -> None:
    with pytest.raises(ValidationError):
        factory(**values)


def test_order_eta_cannot_precede_creation() -> None:
    with pytest.raises(ValidationError, match="must not precede"):
        PurchaseOrder(
            order_id="order-1",
            sku="SKU-1",
            supplier_id="supplier-a",
            quantity=5,
            created_day=4,
            visible_eta_day=3,
            status="pending",
        )


def test_proposal_does_not_accept_approval_or_unknown_actions() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProposalPayload.model_validate(proposal_data() | {"approved": True})
    data = proposal_data()
    data["actions"][0]["action_type"] = "delete_inventory"
    with pytest.raises(ValidationError):
        ProposalPayload.model_validate(data)


def test_proposal_requires_expiry_and_aware_timestamps() -> None:
    data = proposal_data()
    data["expires_at"] = data["created_at"]
    with pytest.raises(ValidationError, match="later than"):
        ProposalPayload.model_validate(data)
    data = proposal_data()
    data["created_at"] = datetime(2026, 11, 18, 12)
    with pytest.raises(ValidationError, match="timezone"):
        ProposalPayload.model_validate(data)


def test_proposal_rejects_empty_or_duplicate_action_bundles() -> None:
    data = proposal_data()
    data["actions"] = []
    with pytest.raises(ValidationError):
        ProposalPayload.model_validate(data)
    data = proposal_data()
    data["actions"].append(data["actions"][0].copy())
    with pytest.raises(ValidationError, match="duplicate action"):
        ProposalPayload.model_validate(data)


def test_hash_is_stable_across_json_and_equivalent_timezone_offsets() -> None:
    payload = ProposalPayload.model_validate(proposal_data())
    assert payload_hash(payload) == payload_hash(
        ProposalPayload.model_validate_json(payload.model_dump_json())
    )
    data = proposal_data()
    offset = timezone(timedelta(hours=-6))
    data["created_at"] = data["created_at"].astimezone(offset)
    data["expires_at"] = data["expires_at"].astimezone(offset)
    assert payload_hash(ProposalPayload.model_validate(data)) == payload_hash(payload)
    assert len(payload_hash(payload)) == 64


@pytest.mark.parametrize("field,value", [("tenant_id", "tenant-b"), ("snapshot_version", 2)])
def test_hash_binds_scope_and_versions(field: str, value: object) -> None:
    data = proposal_data()
    original = payload_hash(ProposalPayload.model_validate(data))
    data[field] = value
    assert payload_hash(ProposalPayload.model_validate(data)) != original


def test_hash_binds_exact_actions_and_order() -> None:
    data = proposal_data()
    original = payload_hash(ProposalPayload.model_validate(data))
    data["actions"][0]["quantity"] = 15
    assert payload_hash(ProposalPayload.model_validate(data)) != original
    data = proposal_data()
    data["actions"].reverse()
    assert payload_hash(ProposalPayload.model_validate(data)) != original


def test_proposal_actions_are_frozen() -> None:
    payload = ProposalPayload.model_validate(proposal_data())
    assert isinstance(payload.actions[0], CreatePurchaseOrderAction)
    assert isinstance(payload.actions[1], SetPriceAction)
    with pytest.raises(ValidationError, match="frozen"):
        payload.actions[0].quantity = 15


def test_product_money_uses_positive_integer_cents() -> None:
    values = snapshot_data()["products"][0]
    for invalid in (0, -1, 10.5, True, "1000"):
        with pytest.raises(ValidationError):
            Product.model_validate(values | {"price_cents": invalid})
