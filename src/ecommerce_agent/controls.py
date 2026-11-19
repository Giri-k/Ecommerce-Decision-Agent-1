"""Scoped numerical tools and deterministic policy adapters; no evaluator import."""

import hashlib
import json
from copy import deepcopy
from datetime import timedelta

from ecommerce_agent.contracts import CreatePurchaseOrderAction
from ecommerce_agent.forecasting import forecast_demand
from ecommerce_agent.planning import compare_plans, inventory_position, order_up_to
from ecommerce_agent.validation import normalize_actions, validate_plan

TOOLS_VERSION = "numeric-tools-0.1.0"


class NumericalTools:
    def __init__(self, snapshot, history, as_of_date, config):
        if snapshot.tenant_id != history.tenant_id:
            raise ValueError("history and store tenant scopes differ")
        if max(r.date for r in history.records) != as_of_date - timedelta(days=1):
            raise ValueError("tool history must end on the last completed day")
        self.snapshot, self.history, self.as_of_date, self.config = (
            snapshot,
            history,
            as_of_date,
            config,
        )
        self.traces = []
        self._forecasts = {}
        self._compare_calls = 0
        self._attempts = 0

    def _start(self, name):
        if self._attempts >= self.config.model.max_tool_calls:
            raise ValueError("tool-call budget exhausted")
        if name == "compare_plans" and self._compare_calls >= self.config.model.max_compare_calls:
            raise ValueError("compare-plans budget exhausted")
        self._attempts += 1
        if name == "compare_plans":
            self._compare_calls += 1

    def _record(
        self,
        name,
        inputs,
        payload,
        source_ids=(),
        units="mixed; explicit field suffixes",
        warnings=(),
    ):
        digest = hashlib.sha256(
            json.dumps(
                [
                    self.snapshot.tenant_id,
                    self.snapshot.snapshot_version,
                    name,
                    self._attempts,
                    inputs,
                    str(self.as_of_date),
                    source_ids,
                    payload,
                ],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        response = {
            "tool_call_id": f"tool:{digest}",
            "tool": name,
            "version": TOOLS_VERSION,
            "as_of": str(self.as_of_date),
            "tenant_id": self.snapshot.tenant_id,
            "snapshot_version": self.snapshot.snapshot_version,
            "policy_version": self.snapshot.policy_version,
            "source_ids": list(source_ids),
            "units": units,
            "warnings": list(warnings),
            "inputs": inputs,
            "result": payload,
        }
        if len(json.dumps(response, sort_keys=True).encode()) > 64000:
            raise ValueError("tool response exceeds 64000 bytes; narrow SKUs or history lookback")
        self.traces.append(response)
        return deepcopy(response)

    def _scope_skus(self, skus):
        known = {p.sku for p in self.snapshot.products}
        if (
            not isinstance(skus, (list, tuple))
            or not skus
            or len(skus) > 10
            or len(set(skus)) != len(skus)
            or not set(skus) <= known
        ):
            raise ValueError("request one to ten unique SKUs in the current store")

    def get_store_snapshot(self):
        self._start("get_store_snapshot")
        return self._record(
            "get_store_snapshot",
            {},
            self.snapshot.model_dump(mode="json"),
            [f"snapshot:{self.snapshot.tenant_id}:{self.snapshot.snapshot_version}"],
        )

    def get_sales_history(self, skus, lookback_days=56):
        self._start("get_sales_history")
        self._scope_skus(skus)
        cutoff = self.as_of_date - timedelta(days=1)
        records = self.history.read(skus, cutoff, lookback_days=lookback_days)
        return self._record(
            "get_sales_history",
            {"skus": list(skus), "lookback_days": lookback_days},
            [r.model_dump(mode="json") for r in records],
            [self.history.source_id],
            "fulfilled units/day and price cents",
        )

    def get_supplier_status(self, supplier_ids):
        self._start("get_supplier_status")
        known = {s.supplier_id for s in self.snapshot.suppliers}
        if (
            not isinstance(supplier_ids, (list, tuple))
            or not supplier_ids
            or len(set(supplier_ids)) != len(supplier_ids)
            or not set(supplier_ids) <= known
        ):
            raise ValueError("request unique suppliers in the current store")
        payload = {
            "suppliers": [
                s.model_dump() for s in self.snapshot.suppliers if s.supplier_id in supplier_ids
            ],
            "pending_orders": [
                o.model_dump()
                for o in self.snapshot.open_purchase_orders
                if o.supplier_id in supplier_ids
            ],
        }
        return self._record(
            "get_supplier_status",
            {"supplier_ids": list(supplier_ids)},
            payload,
            [
                f"supplier:{self.snapshot.tenant_id}:{s}:{self.snapshot.snapshot_version}"
                for s in supplier_ids
            ],
            "lead times and published ETA in simulated days",
        )

    def forecast(self, skus, horizon=None):
        self._start("forecast_demand")
        self._scope_skus(skus)
        horizon = self.config.planning.horizon if horizon is None else horizon
        forecasts = forecast_demand(
            self.history,
            skus,
            self.as_of_date - timedelta(days=1),
            horizon,
            self.config.planning.forecast_model,
        )
        self._forecasts.update({f.sku: f for f in forecasts})
        return self._record(
            "forecast_demand",
            {"skus": list(skus), "horizon": horizon},
            [f.model_dump(mode="json") for f in forecasts],
            [f.source_id for f in forecasts],
            "units/day",
            sorted({w for f in forecasts for w in f.warnings}),
        )

    def compare(self, skus):
        self._start("compare_plans")
        if not skus or len(set(skus)) != len(skus) or not set(skus) <= self._forecasts.keys():
            raise ValueError("compare requires unique SKUs with server-recorded forecasts")
        forecasts = tuple(self._forecasts[sku] for sku in skus)
        result = compare_plans(self.snapshot, forecasts, self.config)
        return self._record(
            "compare_plans",
            {"skus": list(skus)},
            result,
            [f.source_id for f in forecasts],
            warnings=result["warnings"],
        )

    def validate(self, actions, expected_snapshot_version=None, expected_policy_version=None):
        self._start("validate_plan")
        actions = normalize_actions(actions)
        version = (
            self.snapshot.snapshot_version
            if expected_snapshot_version is None
            else expected_snapshot_version
        )
        result = validate_plan(
            self.snapshot,
            actions,
            self.config,
            expected_snapshot_version=version,
            expected_policy_version=expected_policy_version,
        )
        return self._record(
            "validate_plan",
            {"actions": [a.model_dump() for a in actions]},
            result,
            [f"snapshot:{self.snapshot.tenant_id}:{self.snapshot.snapshot_version}"],
        )


class DeterministicControl:
    def __init__(self, name, history, config):
        if name not in {"rules", "fixed"}:
            raise ValueError("unknown deterministic control")
        self.name, self.history, self.config = name, history, config
        self.decisions = []

    def record_completed_sales(self, rows, completed_date):
        self.history = self.history.append_completed(rows, completed_date=completed_date)

    def decide(self, snapshot):
        today = max(r.date for r in self.history.records) + timedelta(days=1)
        tools = NumericalTools(snapshot, self.history, today, self.config)
        tools.get_store_snapshot()
        skus = sorted(p.sku for p in snapshot.products)
        tools.forecast(skus)
        if self.name == "fixed":
            result = tools.compare(skus)["result"]
            actions = normalize_actions(result["actions"])
            explanation = "Select the feasible tool-ranked bundle or justified no-op."
        else:
            actions = []
            products = {p.sku: p for p in snapshot.products}
            suppliers = {s.supplier_id: s for s in snapshot.suppliers}
            for sku in skus:
                forecast = tools._forecasts[sku]
                lead = suppliers[products[sku].supplier_id].lead_time_days
                coverage = sum(p.mean_units for p in forecast.points[:lead])
                if inventory_position(snapshot, sku) >= coverage:
                    continue
                q = order_up_to(snapshot, forecast, self.config)
                # Stable SKU priority; reduce orders by case packs until affordable.
                while q >= self.config.operating.minimum_order_units:
                    proposed = actions + [CreatePurchaseOrderAction(sku=sku, quantity=q)]
                    check = validate_plan(
                        snapshot,
                        proposed,
                        self.config,
                        expected_snapshot_version=snapshot.snapshot_version,
                    )
                    if check["feasible"]:
                        actions = proposed
                        break
                    q -= self.config.operating.case_pack
            actions = tuple(actions)
            explanation = "Reorder to lead time plus seven days when below lead-time coverage."
        validation = tools.validate(actions)
        if not validation["result"]["feasible"]:
            raise RuntimeError("control produced an infeasible bundle")
        self.decisions.append(
            {
                "day": snapshot.day,
                "as_of": str(today),
                "policy": self.name,
                "snapshot_version": snapshot.snapshot_version,
                "policy_version": snapshot.policy_version,
                "actions": [a.model_dump() for a in actions],
                "outcome": "proposal" if actions else "justified_no_op",
                "explanation": explanation,
                "evidence_ids": [t["tool_call_id"] for t in tools.traces],
                "tool_traces": tools.traces,
            }
        )
        return actions
