"""Immutable visible-state and proposal contracts shared by project components.

These schemas validate structure and references. Business-policy validation and
authenticated approval belong to separate services; a payload hash grants no
authority to execute a proposal.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
DisplayName = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200),
]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]


class FrozenContract(BaseModel):
    """Deny undeclared fields and normal attribute mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Supplier(FrozenContract):
    supplier_id: Identifier
    lead_time_days: PositiveInt


class Product(FrozenContract):
    sku: Identifier
    name: DisplayName
    supplier_id: Identifier
    reference_price_cents: PositiveInt
    price_cents: PositiveInt
    cost_cents: PositiveInt
    last_price_change_day: PositiveInt | None = None


class InventoryItem(FrozenContract):
    sku: Identifier
    stock: NonnegativeInt
    version: PositiveInt


class PurchaseOrder(FrozenContract):
    order_id: Identifier
    sku: Identifier
    supplier_id: Identifier
    quantity: PositiveInt
    created_day: PositiveInt
    visible_eta_day: PositiveInt
    status: Literal["pending", "received"]

    @model_validator(mode="after")
    def check_visible_eta(self) -> PurchaseOrder:
        if self.visible_eta_day < self.created_day:
            raise ValueError("visible_eta_day must not precede created_day")
        return self


class StoreSnapshot(FrozenContract):
    """Only information released to the merchant and policy at this snapshot."""

    schema_version: Literal["0.1.0"] = "0.1.0"
    tenant_id: Identifier
    day: PositiveInt
    snapshot_version: PositiveInt
    policy_version: Identifier
    cash_cents: NonnegativeInt
    cash_millicents: NonnegativeInt | None = None
    products: tuple[Product, ...]
    suppliers: tuple[Supplier, ...]
    inventory: tuple[InventoryItem, ...]
    open_purchase_orders: tuple[PurchaseOrder, ...] = ()

    @model_validator(mode="after")
    def check_references(self) -> StoreSnapshot:
        if self.cash_millicents is not None and self.cash_millicents // 1000 != self.cash_cents:
            raise ValueError("cash cents must be the floor of exact millicents")
        if any(
            p.last_price_change_day is not None and p.last_price_change_day > self.day
            for p in self.products
        ):
            raise ValueError("price change day cannot be in the future")
        product_ids = [product.sku for product in self.products]
        supplier_ids = [supplier.supplier_id for supplier in self.suppliers]
        inventory_ids = [item.sku for item in self.inventory]
        order_ids = [order.order_id for order in self.open_purchase_orders]
        for label, ids in (
            ("product SKU", product_ids),
            ("supplier ID", supplier_ids),
            ("inventory SKU", inventory_ids),
            ("purchase order ID", order_ids),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label}")

        known_suppliers = set(supplier_ids)
        product_suppliers = {product.sku: product.supplier_id for product in self.products}
        if any(product.supplier_id not in known_suppliers for product in self.products):
            raise ValueError("product references an unknown supplier")
        if set(inventory_ids) != set(product_ids):
            raise ValueError("inventory must contain exactly one entry for every product SKU")
        for order in self.open_purchase_orders:
            if order.sku not in product_suppliers:
                raise ValueError("purchase order references an unknown SKU")
            if order.supplier_id not in known_suppliers:
                raise ValueError("purchase order references an unknown supplier")
            if order.supplier_id != product_suppliers[order.sku]:
                raise ValueError("purchase order supplier differs from the product supplier")
        return self


class SetPriceAction(FrozenContract):
    action_type: Literal["set_price"] = "set_price"
    sku: Identifier
    price_cents: PositiveInt


class CreatePurchaseOrderAction(FrozenContract):
    action_type: Literal["create_purchase_order"] = "create_purchase_order"
    sku: Identifier
    quantity: PositiveInt


Action = Annotated[
    SetPriceAction | CreatePurchaseOrderAction,
    Field(discriminator="action_type"),
]


class ProposalPayload(FrozenContract):
    """Exact approval subject, excluding any assertion of approval itself."""

    schema_version: Literal["0.1.0"] = "0.1.0"
    tenant_id: Identifier
    run_id: Identifier
    snapshot_version: PositiveInt
    policy_version: Identifier
    simulated_day: PositiveInt
    created_at: AwareDatetime
    expires_at: AwareDatetime
    evidence_ids: tuple[Identifier, ...]
    actions: Annotated[tuple[Action, ...], Field(min_length=1, max_length=20)]

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def check_bundle(self) -> ProposalPayload:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        keys = [(action.action_type, action.sku) for action in self.actions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate action type for the same SKU")
        return self


def payload_hash(payload: ProposalPayload) -> str:
    """Hash every proposal field with stable UTC times and exact action order.

    Object-key ordering is canonicalized; arrays retain their original order.
    This is a versioned internal serialization, not a digital signature or an
    authorization check. Callers must use validated ProposalPayload instances.
    """

    content = payload.model_dump(mode="json")
    for key in ("created_at", "expires_at"):
        timestamp = getattr(payload, key)
        content[key] = timestamp.astimezone(UTC).isoformat(timespec="microseconds")
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
