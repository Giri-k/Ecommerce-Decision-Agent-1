"""Privileged local simulator, separate from policy observations.

This module is an evaluator harness, not an authorized production executor.
The durable simulator worker wraps this engine with the Phase 2 database boundary.
"""

import csv
import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Literal

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
from ecommerce_agent.sampling import negative_binomial_quantile, uniform
from ecommerce_agent.validation import holding_reserve, require_feasible

SIMULATOR_VERSION = "daily-store-0.2.0"
MILLI = 1000  # Integer millicents: 100,000 per USD.


@dataclass(frozen=True)
class Scenario:
    scenario_id: str = "dev-normal-001"
    family: Literal["normal", "demand_surge", "supplier_delay"] = "normal"
    seed: int = 20261119
    replicate: int = 0
    onset_day: int = 4
    duration_days: int = 5
    demand_multiplier: float = 2.0
    delay_days: int = 3

    def __post_init__(self):
        if self.family not in {"normal", "demand_surge", "supplier_delay"}:
            raise ValueError("unknown development scenario family")
        if not self.scenario_id:
            raise ValueError("scenario ID is required")
        for field, minimum in (
            ("seed", 0),
            ("replicate", 0),
            ("onset_day", 1),
            ("duration_days", 1),
            ("delay_days", 0),
        ):
            value = getattr(self, field)
            if type(value) is not int or value < minimum:
                raise ValueError(f"invalid scenario {field}")
        if not math.isfinite(self.demand_multiplier) or not 0 < self.demand_multiplier <= 10:
            raise ValueError("demand multiplier must be in (0,10]")


@dataclass(frozen=True)
class PrivateOrder:
    visible: PurchaseOrder
    actual_arrival_day: int
    notice_day: int | None = None


@dataclass(frozen=True)
class World:
    scenario: Scenario
    first_date: date
    weekday_rates: dict[str, tuple[float, ...]]
    elasticities: dict[str, float]


@dataclass(frozen=True)
class Initialization:
    snapshot: StoreSnapshot
    world: World
    orders: tuple[PrivateOrder, ...] = ()
    opening_paid_principal_cents: int = 0
    opening_paid_fees_cents: int = 0


def initialize_store(
    data_dir: Path, scenario: Scenario = Scenario(), config: ProjectConfig | None = None
) -> Initialization:
    settings = config or load_config()
    manifest = json.loads((data_dir / "manifest.json").read_text())
    for name in (
        "sales_history.csv",
        "product_manifest.json",
        "split_manifest.json",
        "data_quality.json",
    ):
        if checksum(data_dir / name) != manifest["artifacts"][name]:
            raise ValueError(f"curated data checksum mismatch: {name}")
    product_manifest = json.loads((data_dir / "product_manifest.json").read_text())
    if product_manifest["selection_cutoff_day"] != TRAIN_END:
        raise ValueError("unexpected historical cutoff")
    selected = sorted(product_manifest["products"], key=lambda x: x["sku"])
    if len(selected) != 10:
        raise ValueError("simulator core requires ten products")
    skus = {item["sku"] for item in selected}
    if len(skus) != len(selected):
        raise ValueError("duplicate selected product")
    totals = {sku: [0] * 7 for sku in skus}
    counts = {sku: [0] * 7 for sku in skus}
    seen, cutoff_date = set(), None
    with (data_dir / "sales_history.csv").open(newline="") as stream:
        for row in csv.DictReader(stream):
            day = int(row["day"])
            if day > TRAIN_END:
                continue  # Do not parse held-out sales or prices.
            sku = row["sku"]
            if sku not in skus or not 1 <= day <= TRAIN_END or (sku, day) in seen:
                raise ValueError("invalid or duplicate training daily key")
            seen.add((sku, day))
            current = date.fromisoformat(row["date"])
            units = int(row["sales_units"])
            if units < 0:
                raise ValueError("negative training sales")
            totals[sku][current.weekday()] += units
            counts[sku][current.weekday()] += 1
            if day == TRAIN_END:
                if cutoff_date is not None and cutoff_date != current:
                    raise ValueError("inconsistent cutoff date")
                cutoff_date = current
    if len(seen) != len(skus) * TRAIN_END or cutoff_date is None:
        raise ValueError("incomplete training panel")
    suppliers = (
        Supplier(supplier_id="supplier-fast", lead_time_days=3),
        Supplier(supplier_id="supplier-standard", lead_time_days=7),
    )
    products, inventory, elasticities, rates = [], [], {}, {}
    sim = settings.simulation
    for index, item in enumerate(selected):
        sku, reference = item["sku"], item["reference_price_cents"]

        # Initialization streams are fixed by seed/SKU, not chosen policy or order count.
        def draw(event, sku=sku):
            return uniform(scenario.seed, scenario.scenario_id, scenario.replicate, sku, event)

        cost_percent = sim.unit_cost_percent_min + int(
            draw("cost") * (sim.unit_cost_percent_max - sim.unit_cost_percent_min + 1)
        )
        coverage = sim.initial_inventory_days_min + int(
            draw("stock") * (sim.initial_inventory_days_max - sim.initial_inventory_days_min + 1)
        )
        mean = Decimal(sum(totals[sku])) / TRAIN_END
        stock = max(1, int((mean * coverage).to_integral_value(rounding=ROUND_CEILING)))
        products.append(
            Product(
                sku=sku,
                name=f"Synthetic label {index + 1} ({item['category'].lower()})",
                supplier_id=suppliers[index % 2].supplier_id,
                reference_price_cents=reference,
                price_cents=reference,
                cost_cents=max(1, reference * cost_percent // 100),
            )
        )
        inventory.append(InventoryItem(sku=sku, stock=stock, version=1))
        rates[sku] = tuple(
            total / count for total, count in zip(totals[sku], counts[sku], strict=True)
        )
        elasticities[sku] = float(sim.elasticity_min) + draw("elasticity") * float(
            sim.elasticity_max - sim.elasticity_min
        )
    policy_hash = hashlib.sha256(settings.model_dump_json().encode()).hexdigest()[:16]
    cash = settings.operating.initial_cash_cents
    orders, paid, fees = [], 0, 0
    if scenario.family == "supplier_delay":
        product = products[0]  # Assigned to the three-day supplier.
        qty = settings.operating.minimum_order_units
        paid, fees = qty * product.cost_cents, settings.operating.order_fee_cents
        cash -= paid + fees
        if cash < 0:
            raise ValueError("opening prepaid order exceeds initial funding")
        visible = PurchaseOrder(
            order_id="opening-po-001",
            sku=product.sku,
            supplier_id=product.supplier_id,
            quantity=qty,
            created_day=1,
            visible_eta_day=4,
            status="pending",
        )
        delay = scenario.delay_days if 4 >= scenario.onset_day else 0
        orders = [PrivateOrder(visible, 4 + delay, 4 if delay else None)]
    snapshot = StoreSnapshot(
        tenant_id="development-m5-store",
        day=1,
        snapshot_version=1,
        policy_version=f"development-{policy_hash}",
        cash_cents=cash,
        products=tuple(products),
        inventory=tuple(inventory),
        suppliers=suppliers,
        open_purchase_orders=tuple(order.visible for order in orders),
    )
    return Initialization(
        snapshot,
        World(scenario, cutoff_date + timedelta(days=1), rates, elasticities),
        tuple(orders),
        paid,
        fees,
    )


class Simulator:
    """Evaluator-owned engine. Policy callbacks receive only observe() results."""

    def __init__(self, initial: Initialization, config: ProjectConfig | None = None):
        self.config = config or load_config()
        if initial.snapshot.day != 1:
            raise ValueError("a new episode must start on day one")
        self.initial = initial
        self._world = initial.world
        self._day = initial.snapshot.day
        self._version = initial.snapshot.snapshot_version
        self._products = {p.sku: p for p in initial.snapshot.products}
        if not self._products:
            raise ValueError("an episode requires at least one product")
        policy = self.config.operating
        if any(
            p.cost_cents * 100 > p.price_cents * (100 - policy.minimum_gross_margin_percent)
            or not p.reference_price_cents * policy.minimum_price_percent
            <= p.price_cents * 100
            <= p.reference_price_cents * policy.maximum_price_percent
            for p in self._products.values()
        ):
            raise ValueError("initial prices violate policy")
        self._stock = {i.sku: i.stock for i in initial.snapshot.inventory}
        self._inventory_versions = {i.sku: i.version for i in initial.snapshot.inventory}
        self._orders = {o.visible.order_id: o for o in initial.orders}
        if set(self._orders) != {o.order_id for o in initial.snapshot.open_purchase_orders}:
            raise ValueError("private and visible opening orders disagree")
        for order in initial.orders:
            if order.visible.status != "pending":
                raise ValueError("opening purchase orders must be pending and already paid")
            if order.visible != next(
                o
                for o in initial.snapshot.open_purchase_orders
                if o.order_id == order.visible.order_id
            ):
                raise ValueError("opening order payload mismatch")
            lead = next(
                s.lead_time_days
                for s in initial.snapshot.suppliers
                if s.supplier_id == order.visible.supplier_id
            )
            if order.actual_arrival_day < order.visible.created_day + lead:
                raise ValueError("order arrives before allowed lead time")
            if order.actual_arrival_day < order.visible.visible_eta_day:
                raise ValueError("core simulator permits delays, not early delivery")
            if (
                order.notice_day is not None
                and not 1 <= order.notice_day <= order.actual_arrival_day
            ):
                raise ValueError("invalid supplier notice day")
        if set(self._products) != set(self._world.weekday_rates) or set(self._products) != set(
            self._world.elasticities
        ):
            raise ValueError("private calibration does not match visible SKUs")
        if any(
            len(rates) != 7 or any(not math.isfinite(r) or r < 0 for r in rates)
            for rates in self._world.weekday_rates.values()
        ):
            raise ValueError("invalid private weekday rates")
        if any(not math.isfinite(e) or not -5 <= e <= 0 for e in self._world.elasticities.values()):
            raise ValueError("invalid private elasticity")
        self._opening_cash = (
            initial.snapshot.cash_millicents
            if initial.snapshot.cash_millicents is not None
            else initial.snapshot.cash_cents * MILLI
        )
        self._cash = self._opening_cash
        self._begun = False
        self.rows, self.days, self.sales_observations, self.observations = [], [], [], []
        self._received = {sku: 0 for sku in self._stock}
        self._sold = {sku: 0 for sku in self._stock}
        self._updates = []
        self._receipts_today = {sku: 0 for sku in self._stock}
        if self._cash < self._holding_reserve():
            raise ValueError("initial cash cannot cover committed holding costs")

    @property
    def horizon(self):
        return self.config.simulation.episode_days + self.config.simulation.continuation_days

    @property
    def complete(self):
        return self._day > self.horizon

    def _holding(self, sku, quantity):
        rate = int(self.config.simulation.daily_holding_rate * MILLI)
        return self._products[sku].cost_cents * quantity * rate

    def _holding_reserve(self, proposed=()):
        return holding_reserve(self._snapshot(), self.config, proposed)

    def _supplier(self, supplier_id):
        return next(s for s in self.initial.snapshot.suppliers if s.supplier_id == supplier_id)

    def observe(self) -> StoreSnapshot:
        if self.complete:
            raise ValueError("episode has finished")
        if not self._begun:
            self._receipts_today = {sku: 0 for sku in self._stock}
            changed = False
            for key, order in list(self._orders.items()):
                visible = order.visible
                if visible.status != "pending":
                    continue
                if order.actual_arrival_day <= self._day:
                    self._stock[visible.sku] += visible.quantity
                    self._received[visible.sku] += visible.quantity
                    self._receipts_today[visible.sku] += visible.quantity
                    self._inventory_versions[visible.sku] += 1
                    self._orders[key] = replace(
                        order, visible=visible.model_copy(update={"status": "received"})
                    )
                    changed = True
                elif order.notice_day is not None and order.notice_day == self._day:
                    revised = visible.model_copy(
                        update={"visible_eta_day": order.actual_arrival_day}
                    )
                    self._orders[key] = replace(order, visible=revised)
                    self._updates.append(
                        {
                            "day": self._day,
                            "order_id": key,
                            "published_eta_day": order.actual_arrival_day,
                        }
                    )
                    changed = True
            if changed:
                self._version += 1
            self._begun = True
        return self._snapshot()

    def _snapshot(self):
        return StoreSnapshot(
            tenant_id=self.initial.snapshot.tenant_id,
            day=self._day,
            snapshot_version=self._version,
            policy_version=self.initial.snapshot.policy_version,
            cash_cents=self._cash // MILLI,
            cash_millicents=self._cash,
            products=tuple(self._products.values()),
            suppliers=self.initial.snapshot.suppliers,
            inventory=tuple(
                InventoryItem(sku=sku, stock=qty, version=self._inventory_versions[sku])
                for sku, qty in self._stock.items()
            ),
            open_purchase_orders=tuple(
                o.visible for o in self._orders.values() if o.visible.status == "pending"
            ),
        )

    def _validate(self, actions, expected_version):
        return require_feasible(
            self._snapshot(), actions, self.config, expected_snapshot_version=expected_version
        )

    def _demand(self, sku):
        world, scenario = self._world, self._world.scenario
        weekday = (world.first_date + timedelta(days=self._day - 1)).weekday()
        product = self._products[sku]
        mean = (
            world.weekday_rates[sku][weekday]
            * (product.price_cents / product.reference_price_cents) ** world.elasticities[sku]
        )
        if scenario.family == "demand_surge" and scenario.onset_day <= self._day < (
            scenario.onset_day + scenario.duration_days
        ):
            mean *= scenario.demand_multiplier
        draw = uniform(
            scenario.seed, scenario.scenario_id, scenario.replicate, sku, self._day, "demand"
        )
        return negative_binomial_quantile(
            draw, mean, float(self.config.simulation.demand_dispersion)
        )

    def step(self, actions=(), *, expected_snapshot_version=None):
        snapshot = self.observe()  # Receipts and public notices precede decisions.
        actions = tuple(actions)
        actions = self._validate(
            actions, self._version if not actions else expected_snapshot_version
        )
        # Generate outcomes before mutating the bundle, using candidate prices.
        original_products = self._products.copy()
        for action in actions:
            if isinstance(action, SetPriceAction):
                self._products[action.sku] = self._products[action.sku].model_copy(
                    update={"price_cents": action.price_cents}
                )
        try:
            demands = {sku: self._demand(sku) for sku in self._products}
        finally:
            self._products = original_products
        external = getattr(self, "_external_action_totals", {})
        purchases = external.get("purchases", {}).copy()
        fees = external.get("fees", {}).copy()
        ordered = external.get("ordered", {}).copy()
        for action in actions:
            sku = action.sku
            product = self._products[sku]
            if isinstance(action, SetPriceAction):
                if action.price_cents != product.price_cents:
                    product = product.model_copy(update={"last_price_change_day": self._day})
                self._products[sku] = product.model_copy(update={"price_cents": action.price_cents})
            else:
                principal = action.quantity * product.cost_cents
                fee = self.config.operating.order_fee_cents
                self._cash -= (principal + fee) * MILLI
                purchases[sku], fees[sku], ordered[sku] = principal, fee, action.quantity
                expected = self._day + self._supplier(product.supplier_id).lead_time_days
                scenario = self._world.scenario
                # One keyed supplier/placement-day shock, independent of order count.
                delay_draw = uniform(
                    scenario.seed,
                    scenario.scenario_id,
                    scenario.replicate,
                    product.supplier_id,
                    self._day,
                    "supplier_delay",
                )
                delayed = (
                    scenario.family == "supplier_delay"
                    and product.supplier_id == "supplier-fast"
                    and expected >= scenario.onset_day
                    and delay_draw < 1.0
                )
                actual = expected + (scenario.delay_days if delayed else 0)
                order_id = f"po-{self._day}-{hashlib.sha256(sku.encode()).hexdigest()[:12]}"
                visible = PurchaseOrder(
                    order_id=order_id,
                    sku=sku,
                    supplier_id=product.supplier_id,
                    quantity=action.quantity,
                    created_day=self._day,
                    visible_eta_day=expected,
                    status="pending",
                )
                self._orders[order_id] = PrivateOrder(
                    visible, actual, expected if actual != expected else None
                )
        today = []
        before = {i.sku: i.stock for i in snapshot.inventory}
        for sku, product in self._products.items():
            available = self._stock[sku]
            sold = min(available, demands[sku])
            self._stock[sku] -= sold
            self._sold[sku] += sold
            self._inventory_versions[sku] += 1
            revenue, cogs = sold * product.price_cents, sold * product.cost_cents
            holding = self._holding(sku, self._stock[sku])
            self._cash += revenue * MILLI - holding
            row = {
                "day": self._day,
                "sku": sku,
                "opening_stock": before[sku] - self._receipts_today[sku],
                "received_units": self._receipts_today[sku],
                "available_stock": before[sku],
                "latent_demand": demands[sku],
                "fulfilled_units": sold,
                "lost_units": demands[sku] - sold,
                "end_stock": self._stock[sku],
                "price_cents": product.price_cents,
                "revenue_cents": revenue,
                "cogs_cents": cogs,
                "purchase_principal_cents": purchases.get(sku, 0),
                "order_fee_cents": fees.get(sku, 0),
                "ordered_units": ordered.get(sku, 0),
                "holding_millicents": holding,
                "profit_millicents": (revenue - cogs - fees.get(sku, 0)) * MILLI - holding,
            }
            today.append(row)
            self.sales_observations.append(
                {
                    "day": self._day,
                    "date": (self._world.first_date + timedelta(days=self._day - 1)).isoformat(),
                    "sku": sku,
                    "sales_units": sold,
                    "price_cents": product.price_cents,
                    "ended_without_stock": self._stock[sku] == 0,
                }
            )
        self.rows.extend(today)
        self.days.append(
            {
                "day": self._day,
                "cash_millicents": self._cash,
                "profit_millicents": sum(row["profit_millicents"] for row in today),
                "inventory_value_cents": sum(
                    p.cost_cents * self._stock[sku] for sku, p in self._products.items()
                ),
                "prepaid_pipeline_value_cents": sum(
                    self._products[o.visible.sku].cost_cents * o.visible.quantity
                    for o in self._orders.values()
                    if o.visible.status == "pending"
                ),
            }
        )
        self.observations.append(
            {
                "as_of_date": (self._world.first_date + timedelta(days=self._day - 1)).isoformat(),
                "snapshot": snapshot.model_dump(mode="json"),
                "supplier_updates": [u for u in self._updates if u["day"] == self._day],
            }
        )
        self._day += 1
        self._version += 1
        self._begun = False
        self._external_action_totals = {}
        self.check_invariants()

    def check_invariants(self):
        for item in self.initial.snapshot.inventory:
            if (
                self._stock[item.sku]
                != item.stock + self._received[item.sku] - self._sold[item.sku]
            ):
                raise RuntimeError("inventory conservation failure")
            if type(self._stock[item.sku]) is not int or self._stock[item.sku] < 0:
                raise RuntimeError("invalid stock")
        cash = self._opening_cash + sum(
            (r["revenue_cents"] - r["purchase_principal_cents"] - r["order_fee_cents"]) * MILLI
            - r["holding_millicents"]
            for r in self.rows
        )
        if self._cash != cash or self._cash < 0:
            raise RuntimeError("cash reconciliation failure")
        for sku in self._stock:
            received = sum(
                o.visible.quantity
                for o in self._orders.values()
                if o.visible.sku == sku and o.visible.status == "received"
            )
            if received != self._received[sku]:
                raise RuntimeError("purchase receipt reconciliation failure")
        opening_assets = (
            self._opening_cash
            + sum(
                self._products[i.sku].cost_cents * i.stock * MILLI
                for i in self.initial.snapshot.inventory
            )
            + sum(
                self._products[o.visible.sku].cost_cents * o.visible.quantity * MILLI
                for o in self.initial.orders
            )
        )
        closing_assets = (
            self._cash
            + sum(self._products[sku].cost_cents * qty * MILLI for sku, qty in self._stock.items())
            + sum(
                self._products[o.visible.sku].cost_cents * o.visible.quantity * MILLI
                for o in self._orders.values()
                if o.visible.status == "pending"
            )
        )
        if closing_assets - opening_assets != sum(r["profit_millicents"] for r in self.rows):
            raise RuntimeError("profit and asset reconciliation failure")
        if any(r["fulfilled_units"] + r["lost_units"] != r["latent_demand"] for r in self.rows):
            raise RuntimeError("demand conservation failure")
        if any(
            r["opening_stock"] + r["received_units"] != r["available_stock"]
            or r["available_stock"] - r["fulfilled_units"] != r["end_stock"]
            for r in self.rows
        ):
            raise RuntimeError("daily inventory reconciliation failure")

    def metrics(self):
        if not self.complete:
            raise ValueError("diagnostic metrics release only after the episode")

        def summarize(rows):
            demand = sum(r["latent_demand"] for r in rows)
            return {
                "operating_profit_millicents": sum(r["profit_millicents"] for r in rows),
                "revenue_cents": sum(r["revenue_cents"] for r in rows),
                "fulfilled_units": sum(r["fulfilled_units"] for r in rows),
                "latent_demand_units": demand,
                "lost_units": sum(r["lost_units"] for r in rows),
                "fill_rate": None
                if not demand
                else sum(r["fulfilled_units"] for r in rows) / demand,
                "stockout_sku_days": sum(r["lost_units"] > 0 for r in rows),
                "holding_millicents": sum(r["holding_millicents"] for r in rows),
                "purchase_principal_cents": sum(r["purchase_principal_cents"] for r in rows),
                "order_fee_cents": sum(r["order_fee_cents"] for r in rows),
            }

        pending = [o.visible for o in self._orders.values() if o.visible.status == "pending"]
        return {
            "core": summarize(
                [r for r in self.rows if r["day"] <= self.config.simulation.episode_days]
            ),
            "extended": summarize(self.rows),
            "ending_cash_millicents": self._cash,
            "ending_inventory_value_cents": sum(
                p.cost_cents * self._stock[sku] for sku, p in self._products.items()
            ),
            "outstanding_order_count": len(pending),
            "outstanding_order_units": sum(o.quantity for o in pending),
            "prepaid_pipeline_value_cents": sum(
                self._products[o.sku].cost_cents * o.quantity for o in pending
            ),
            "unpaid_committed_spend_cents": 0,
            "core_ending_cash_millicents": self.days[self.config.simulation.episode_days - 1][
                "cash_millicents"
            ],
            "core_ending_inventory_value_cents": self.days[self.config.simulation.episode_days - 1][
                "inventory_value_cents"
            ],
            "core_prepaid_pipeline_value_cents": self.days[self.config.simulation.episode_days - 1][
                "prepaid_pipeline_value_cents"
            ],
            "committed_procurement_cents": sum(
                r["purchase_principal_cents"] + r["order_fee_cents"] for r in self.rows
            )
            + self.initial.opening_paid_principal_cents
            + self.initial.opening_paid_fees_cents,
        }


def run_episode(
    data_dir: Path,
    output: Path,
    scenario: Scenario = Scenario(),
    policy: str = "no-op",
    config: ProjectConfig | None = None,
) -> dict:
    """Run a development fixture or deterministic control, without an LLM."""
    if policy not in {"no-op", "scripted", "rules", "fixed"}:
        raise ValueError("unknown development policy")
    data_dir, output = data_dir.resolve(), output.resolve()
    if output == data_dir or output.is_relative_to(data_dir) or data_dir.is_relative_to(output):
        raise ValueError("simulation output must be separate from curated inputs")
    settings = config or load_config()
    initial = initialize_store(data_dir, scenario, settings)
    engine = Simulator(initial, settings)
    controller = None
    if policy in {"rules", "fixed"}:
        from ecommerce_agent.controls import DeterministicControl
        from ecommerce_agent.forecasting import load_history

        controller = DeterministicControl(
            policy, load_history(data_dir, tenant_id=initial.snapshot.tenant_id), settings
        )
    while not engine.complete:
        visible = engine.observe()
        actions = ()
        if policy == "scripted" and visible.day == 1:
            product = visible.products[0]
            proposed_price = min(
                product.reference_price_cents * settings.operating.maximum_price_percent // 100,
                (product.reference_price_cents * 105 + 50) // 100,
            )
            actions = (
                CreatePurchaseOrderAction(
                    sku=product.sku, quantity=settings.operating.minimum_order_units
                ),
                SetPriceAction(sku=product.sku, price_cents=proposed_price),
            )
        if controller is not None and visible.day in settings.simulation.review_days:
            actions = controller.decide(visible)
        engine.step(actions, expected_snapshot_version=visible.snapshot_version)
        if controller is not None:
            rows = engine.sales_observations[-len(visible.products) :]
            controller.record_completed_sales(rows, date.fromisoformat(rows[0]["date"]))
    metrics = engine.metrics()
    public, private = output / "public", output / "private"
    for directory in (public, private):
        directory.mkdir(parents=True, exist_ok=True)
    private.chmod(0o700)

    def write_json(path, content):
        path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n")

    def write_csv(path, rows):
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    write_json(public / "initial_snapshot.json", initial.snapshot.model_dump(mode="json"))
    write_json(
        public / "store_metadata.json",
        {
            "currency": settings.operating.currency,
            "first_simulated_date": initial.world.first_date.isoformat(),
            "historical_cutoff_day": TRAIN_END,
            "operating_fields_provenance": (
                "synthetic costs, inventory, suppliers and purchase orders"
            ),
            "source_product_ids": "selected M5 CA_3 products",
        },
    )
    write_json(public / "final_snapshot.json", engine._snapshot().model_dump(mode="json"))
    if controller is not None:
        write_json(public / "control_decisions.json", controller.decisions)
    else:
        (public / "control_decisions.json").unlink(missing_ok=True)

    (public / "observations.jsonl").write_text(
        "".join(json.dumps(o, sort_keys=True) + "\n" for o in engine.observations)
    )
    write_csv(public / "sales_observations.csv", engine.sales_observations)
    write_csv(private / "daily_ledger.csv", engine.rows)
    write_json(private / "daily_totals.json", engine.days)
    write_json(private / "metrics.json", metrics)
    write_json(
        private / "scenario_truth.json",
        {
            "scenario": vars(scenario),
            "first_date": initial.world.first_date.isoformat(),
            "weekday_rates": initial.world.weekday_rates,
            "elasticities": initial.world.elasticities,
            "orders": [
                {
                    "visible": o.visible.model_dump(mode="json"),
                    "actual_arrival_day": o.actual_arrival_day,
                    "notice_day": o.notice_day,
                }
                for o in engine._orders.values()
            ],
            "opening_paid_principal_cents": initial.opening_paid_principal_cents,
            "opening_paid_fees_cents": initial.opening_paid_fees_cents,
        },
    )
    for artifact in private.iterdir():
        if artifact.is_file():
            artifact.chmod(0o600)
    artifact_names = [
        "public/initial_snapshot.json",
        "public/final_snapshot.json",
        "public/store_metadata.json",
        "public/observations.jsonl",
        "public/sales_observations.csv",
        "private/daily_ledger.csv",
        "private/daily_totals.json",
        "private/metrics.json",
        "private/scenario_truth.json",
    ]
    if controller is not None:
        artifact_names.append("public/control_decisions.json")
    artifacts = {name: checksum(output / name) for name in sorted(artifact_names)}
    manifest = {
        "simulator_version": SIMULATOR_VERSION,
        "usage": "development_only",
        "policy": policy,
        "approval_mode": "privileged numerical development harness; not the approval demo",
        "scenario_id": scenario.scenario_id,
        "scenario_family": scenario.family,
        "core_days": settings.simulation.episode_days,
        "continuation_days": settings.simulation.continuation_days,
        "money_unit": "integer millicents (100000 per USD)",
        "data_manifest_sha256": checksum(data_dir / "manifest.json"),
        "config_sha256": hashlib.sha256(settings.model_dump_json().encode()).hexdigest(),
        "config": settings.model_dump(mode="json"),
        "artifacts": artifacts,
        "model_calls": 0,
    }
    write_json(output / "manifest.json", manifest)
    return {
        "status": "completed",
        "simulator_version": SIMULATOR_VERSION,
        "days": engine.horizon,
        "products": len(initial.snapshot.products),
        "policy": policy,
        "scenario": scenario.family,
        "output": str(output),
        "model_calls": 0,
        "metrics": metrics,
    }
