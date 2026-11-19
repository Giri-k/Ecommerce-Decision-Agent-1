"""Cutoff-safe seasonal forecasts and validation-only historical backtesting."""

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictInt

from ecommerce_agent.contracts import FrozenContract, Identifier
from ecommerce_agent.data import TRAIN_END, VALIDATION_END, checksum

FORECAST_VERSION = "seasonal-0.1.0"
MODELS = ("seasonal_naive", "weekday_mean_4")


class SalesRecord(FrozenContract):
    sku: Identifier
    date: date
    sales_units: Annotated[StrictInt, Field(ge=0)]
    price_cents: Annotated[StrictInt, Field(gt=0)] | None = None
    ended_without_stock: bool | None = None
    provenance: Literal["m5", "simulated"] = "m5"


class ForecastPoint(FrozenContract):
    date: date
    mean_units: float = Field(ge=0, allow_inf_nan=False)
    lower_80_units: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    upper_80_units: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Forecast(FrozenContract):
    sku: Identifier
    cutoff: date
    model_id: Literal["seasonal_naive", "weekday_mean_4"]
    version: str = FORECAST_VERSION
    points: tuple[ForecastPoint, ...]
    source_id: str
    warnings: tuple[str, ...] = ()
    interval_method: str = (
        "past-only rolling one-step absolute residuals, empirical 80th percentile"
    )


@dataclass(frozen=True)
class SalesHistory:
    tenant_id: str
    records: tuple[SalesRecord, ...]
    source_id: str

    def __post_init__(self):
        if not self.tenant_id or not self.records:
            raise ValueError("scoped, nonempty sales history required")
        keys = [(r.sku, r.date) for r in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate history SKU/date")

    def append_completed(self, rows, *, completed_date: date) -> "SalesHistory":
        skus = {r.sku for r in self.records}
        observed = tuple(
            SalesRecord(
                sku=r["sku"],
                date=r["date"],
                sales_units=r["sales_units"],
                price_cents=r["price_cents"],
                ended_without_stock=r["ended_without_stock"],
                provenance="simulated",
            )
            for r in rows
        )
        if len(observed) != len(skus) or {r.sku for r in observed} != skus:
            raise ValueError("completed sales must cover each visible SKU exactly once")
        latest = max(r.date for r in self.records)
        if completed_date != latest + timedelta(days=1) or any(
            r.date != completed_date for r in observed
        ):
            raise ValueError("sales append must be the next completed day")
        digest = hashlib.sha256(
            (self.source_id + "".join(r.model_dump_json() for r in observed)).encode()
        ).hexdigest()
        return SalesHistory(self.tenant_id, self.records + observed, f"history:{digest}")

    def read(self, skus, cutoff: date, *, lookback_days=56) -> tuple[SalesRecord, ...]:
        known = {r.sku for r in self.records}
        if not skus or len(skus) > 10 or len(set(skus)) != len(skus) or not set(skus) <= known:
            raise ValueError("request one to ten unique known SKUs")
        if type(lookback_days) is not int or not 1 <= lookback_days <= 365:
            raise ValueError("history lookback must be 1-365 days")
        if cutoff > max(r.date for r in self.records):
            raise ValueError("history cutoff is not yet observed")
        start = cutoff - timedelta(days=lookback_days - 1)
        rows = tuple(r for r in self.records if r.sku in skus and start <= r.date <= cutoff)
        if {r.sku for r in rows} != set(skus):
            raise ValueError("missing history for requested cutoff")
        return rows


def load_history(
    data_dir: Path, *, max_day=TRAIN_END, tenant_id="development-m5-store"
) -> SalesHistory:
    if type(max_day) is not int or not 1 <= max_day <= VALIDATION_END:
        raise ValueError("final-test history loading is disabled in Phase 1C")
    manifest = json.loads((data_dir / "manifest.json").read_text())
    path = data_dir / "sales_history.csv"
    if checksum(path) != manifest["artifacts"]["sales_history.csv"]:
        raise ValueError("historical panel checksum mismatch")
    rows = []
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            if int(row["day"]) > max_day:
                continue
            rows.append(
                SalesRecord(
                    sku=row["sku"],
                    date=row["date"],
                    sales_units=int(row["sales_units"]),
                    price_cents=int(row["price_cents"]) if row.get("price_cents") else None,
                )
            )
    return SalesHistory(
        tenant_id,
        tuple(sorted(rows, key=lambda r: (r.sku, r.date))),
        f"m5:{manifest['artifacts']['sales_history.csv']}:through-d{max_day}",
    )


def _point(series, target: date, model):
    count = 1 if model == "seasonal_naive" else 4
    matching = [
        r.sales_units for r in series if r.date < target and r.date.weekday() == target.weekday()
    ]
    if not matching:
        raise ValueError("insufficient same-weekday history")
    return sum(matching[-count:]) / len(matching[-count:])


def forecast_demand(
    history: SalesHistory, skus, cutoff: date, horizon=14, model_id="weekday_mean_4"
) -> tuple[Forecast, ...]:
    if type(horizon) is not int or not 1 <= horizon <= 14:
        raise ValueError("forecast horizon must be 1-14 days")
    if model_id not in MODELS:
        raise ValueError("unsupported forecast model")
    rows = history.read(skus, cutoff, lookback_days=112)
    output = []
    for sku in skus:
        series = sorted((r for r in rows if r.sku == sku), key=lambda r: r.date)
        warnings = []
        if len(series) < 28:
            warnings.append("short_history")
        if any(r.ended_without_stock for r in series):
            warnings.append("sales_may_be_stockout_censored")
        if any((b.date - a.date).days != 1 for a, b in zip(series, series[1:], strict=False)):
            warnings.append("history_has_calendar_gaps")
        residuals = []
        for observed in series[-84:]:
            past = [r for r in series if r.date < observed.date]
            if len(past) >= 28:
                residuals.append(abs(observed.sales_units - _point(past, observed.date, model_id)))
        radius = sorted(residuals)[math.ceil(0.8 * len(residuals)) - 1] if residuals else None
        if not residuals:
            warnings.append("interval_uncalibrated")
        points = []
        for offset in range(1, horizon + 1):
            target = cutoff + timedelta(days=offset)
            # Both seasonal methods repeat weekday estimates at every lead; no future actuals.
            value = _point(series, target, model_id)
            points.append(
                ForecastPoint(
                    date=target,
                    mean_units=value,
                    lower_80_units=max(0, value - radius) if radius is not None else None,
                    upper_80_units=value + radius if radius is not None else None,
                )
            )
        digest = hashlib.sha256("".join(r.model_dump_json() for r in series).encode()).hexdigest()
        output.append(
            Forecast(
                sku=sku,
                cutoff=cutoff,
                model_id=model_id,
                points=tuple(points),
                source_id=f"history:{sku}:{cutoff}:{digest}",
                warnings=tuple(warnings),
            )
        )
    return tuple(output)


def validate_forecasts(data_dir: Path, output: Path) -> dict:
    """Six expanding-window validation blocks; never reads final-test sales."""
    data_dir, output = data_dir.resolve(), output.resolve()
    if output == data_dir or output.is_relative_to(data_dir) or data_dir.is_relative_to(output):
        raise ValueError("forecast outputs must be separate from curated inputs")
    history = load_history(data_dir, max_day=VALIDATION_END, tenant_id="historical-CA_3")
    split = json.loads((data_dir / "split_manifest.json").read_text())["partitions"]
    skus = sorted({r.sku for r in history.records})
    by_key = {(r.sku, r.date): r for r in history.records}
    training_end = date.fromisoformat(split["training"]["end_date"])
    scales = {}
    for sku in skus:
        series = sorted(
            (r for r in history.records if r.sku == sku and r.date <= training_end),
            key=lambda r: r.date,
        )
        changes = [
            abs(r.sales_units - by_key[(sku, r.date - timedelta(days=7))].sales_units)
            for r in series[7:]
        ]
        scales[sku] = sum(changes) / len(changes) if changes else 0
    predictions, metrics = [], {}
    for model in MODELS:
        model_rows = []
        for block in split["validation"]["blocks"]:
            cutoff = training_end + timedelta(days=block["forecast_cutoff_day"] - TRAIN_END)
            for forecast in forecast_demand(history, skus, cutoff, model_id=model):
                for point in forecast.points:
                    actual = by_key[(forecast.sku, point.date)].sales_units
                    model_rows.append(
                        {
                            "model_id": model,
                            "sku": forecast.sku,
                            "cutoff": str(cutoff),
                            "date": str(point.date),
                            "actual_units": actual,
                            "forecast_units": point.mean_units,
                            "lower_80_units": point.lower_80_units,
                            "upper_80_units": point.upper_80_units,
                            "absolute_error": abs(actual - point.mean_units),
                        }
                    )
        errors = sum(r["absolute_error"] for r in model_rows)
        actuals = sum(r["actual_units"] for r in model_rows)
        per_sku = {}
        for sku in skus:
            values = [r for r in model_rows if r["sku"] == sku]
            mae = sum(r["absolute_error"] for r in values) / len(values)
            per_sku[sku] = {"mae": mae, "mase_lag7": mae / scales[sku] if scales[sku] else None}
        metrics[model] = {
            "mae": errors / len(model_rows),
            "wape": errors / actuals if actuals else None,
            "interval_80_coverage": sum(
                r["lower_80_units"] <= r["actual_units"] <= r["upper_80_units"] for r in model_rows
            )
            / len(model_rows),
            "mean_interval_width": sum(
                r["upper_80_units"] - r["lower_80_units"] for r in model_rows
            )
            / len(model_rows),
            "per_sku": per_sku,
        }
        predictions.extend(model_rows)
    # Residual artifact may be used for later test intervals only after validation is observed.
    calibration = {
        "usable_after": split["validation"]["end_date"],
        "nominal_coverage": 0.8,
        "method": "absolute validation residual 80th percentile",
        "models": {},
    }
    for model in MODELS:
        calibration["models"][model] = {}
        for sku in skus:
            errors = sorted(
                r["absolute_error"]
                for r in predictions
                if r["model_id"] == model and r["sku"] == sku
            )
            calibration["models"][model][sku] = errors[math.ceil(0.8 * len(errors)) - 1]
    report = {
        "forecast_version": FORECAST_VERSION,
        "partition": "validation",
        "blocks": 6,
        "default_control_model": "weekday_mean_4",
        "default_model_selected_before_this_report": True,
        "final_test_evaluated": False,
        "model_calls": 0,
        "metrics": metrics,
        "data_manifest_sha256": checksum(data_dir / "manifest.json"),
        "warnings": [
            "Intervals are empirical and do not guarantee nominal coverage.",
            "Validation calibration is not available to economic episodes starting at d_1801.",
            "No price features or actual future prices enter these seasonal forecasts.",
            "Undefined WAPE or MASE denominators produce null, not zero.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (("report.json", report), ("validation_calibration.json", calibration)):
        (output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    with (output / "predictions.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(predictions[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(predictions)
    manifest = {
        "forecast_version": FORECAST_VERSION,
        "partition": "validation",
        "final_test_evaluated": False,
        "data_manifest_sha256": checksum(data_dir / "manifest.json"),
        "artifacts": {
            name: checksum(output / name)
            for name in ("report.json", "predictions.csv", "validation_calibration.json")
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return report
