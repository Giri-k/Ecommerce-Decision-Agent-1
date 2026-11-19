"""A small synthetic operational snapshot, not a forecasting dataset or simulator."""

import hashlib
import json
import random
from pathlib import Path

from ecommerce_agent.config import ProjectConfig, load_config
from ecommerce_agent.contracts import InventoryItem, Product, StoreSnapshot, Supplier

FIXTURE_SEED = 20261118
GENERATOR_VERSION = "operational-fixture-0.1.0"


def build_fixture(seed: int = FIXTURE_SEED, config: ProjectConfig | None = None) -> StoreSnapshot:
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    settings = config or load_config()
    rng = random.Random(seed)
    suppliers = (
        Supplier(supplier_id="supplier-fast", lead_time_days=3),
        Supplier(supplier_id="supplier-standard", lead_time_days=7),
    )
    products = []
    inventory = []
    for index in range(10):
        sku = f"SYNTH-{index + 1:03d}"
        reference_cents = rng.randrange(300, 2001)
        cost_cents = reference_cents * rng.randrange(50, 71) // 100
        products.append(
            Product(
                sku=sku,
                name=f"Synthetic household item {index + 1}",
                supplier_id=suppliers[index % 2].supplier_id,
                reference_price_cents=reference_cents,
                price_cents=reference_cents,
                cost_cents=cost_cents,
            )
        )
        inventory.append(InventoryItem(sku=sku, stock=rng.randrange(10, 81), version=1))
    return StoreSnapshot(
        tenant_id="synthetic-development-store",
        day=1,
        snapshot_version=1,
        policy_version="development-0.1.0",
        cash_cents=settings.operating.initial_cash_cents,
        products=tuple(products),
        suppliers=suppliers,
        inventory=tuple(inventory),
    )


def write_fixture(
    output: Path, seed: int = FIXTURE_SEED, config: ProjectConfig | None = None
) -> dict:
    settings = config or load_config()
    snapshot = build_fixture(seed, settings)
    content = snapshot.model_dump_json(indent=2) + "\n"
    config_content = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    manifest = {
        "schema_version": "0.1.0",
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "provenance": "fully_synthetic_operational_smoke_fixture",
        "currency": settings.operating.currency,
        "product_count": len(snapshot.products),
        "source_dataset": None,
        "historical_days": 0,
        "forecast_evaluation_eligible": False,
        "config_sha256": hashlib.sha256(config_content.encode()).hexdigest(),
        "files": {"store_snapshot.json": hashlib.sha256(content.encode()).hexdigest()},
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "store_snapshot.json").write_text(content, encoding="utf-8")
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
