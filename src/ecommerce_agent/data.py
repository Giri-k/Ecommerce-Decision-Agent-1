"""Stream M5 inputs into a small, auditable historical panel.

SKU eligibility and volume strata use training days only. Historical prices in
the exported panel are observations, not known future forecast inputs.
"""

import csv
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

TRAIN_END = 1801
VALIDATION_END = 1885
TEST_END = 1941
PIPELINE_VERSION = "m5-curation-0.1.0"
SOURCE_URL = "https://www.kaggle.com/c/m5-forecasting-accuracy/data"
INPUT_FILES = ("sales_train_evaluation.csv", "calendar.csv", "sell_prices.csv")


@dataclass(frozen=True)
class SelectionRule:
    seed: int = 20261118
    product_count: int = 10
    minimum_active_days: int = 365
    minimum_price_coverage: str = "0.95"

    def __post_init__(self):
        for name in ("seed", "product_count", "minimum_active_days"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "seed" else 1):
                raise ValueError(f"invalid {name}")
        if self.product_count > 10:
            raise ValueError("core scope permits at most ten products")
        try:
            coverage = Decimal(self.minimum_price_coverage)
        except InvalidOperation as exc:
            raise ValueError("invalid price coverage") from exc
        if not coverage.is_finite() or not 0 < coverage <= 1:
            raise ValueError("price coverage must be in (0, 1]")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(path: Path, required: set[str]):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not required <= set(reader.fieldnames or ()):
            raise ValueError(f"{path.name}: missing required columns")
        yield from reader


def _integer(value: str, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: invalid integer") from exc
    if str(result) != value or result < 0:
        raise ValueError(f"{label}: expected a nonnegative integer")
    return result


def _price(value: str) -> int:
    try:
        amount = Decimal(value) * 100
    except InvalidOperation as exc:
        raise ValueError("invalid sell_price") from exc
    if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
        raise ValueError("sell_price must be positive whole cents")
    return int(amount)


def _calendar(source: Path) -> dict[int, dict]:
    result = {}
    for row in _rows(source, {"d", "date", "wm_yr_wk", "weekday"}):
        day = _integer(row["d"].removeprefix("d_"), "calendar day")
        if not 1 <= day <= TEST_END:
            continue
        if day in result:
            raise ValueError("duplicate calendar day")
        parsed_date = date.fromisoformat(row["date"])
        if parsed_date.strftime("%A") != row["weekday"]:
            raise ValueError("calendar weekday disagrees with date")
        result[day] = row
    if set(result) != set(range(1, TEST_END + 1)):
        raise ValueError("calendar must cover d_1 through d_1941")
    origin = date.fromisoformat(result[1]["date"])
    for day, row in result.items():
        if date.fromisoformat(row["date"]) != origin + timedelta(days=day - 1):
            raise ValueError("calendar dates are not consecutive")
    return result


def _sales(source: Path) -> dict[str, dict]:
    result = {}
    required = {"item_id", "store_id", "cat_id", "dept_id"} | {
        f"d_{day}" for day in range(1, TEST_END + 1)
    }
    for row in _rows(source, required):
        if row["store_id"] != "CA_3" or row["cat_id"] not in {"HOUSEHOLD", "HOBBIES"}:
            continue
        sku = row["item_id"]
        if sku in result:
            raise ValueError("duplicate item-store sales row")
        units = tuple(_integer(row[f"d_{d}"], f"{sku} d_{d}") for d in range(1, TEST_END + 1))
        result[sku] = {
            "sku": sku,
            "store_id": row["store_id"],
            "category": row["cat_id"],
            "department": row["dept_id"],
            "units": units,
        }
    if not result:
        raise ValueError("no CA_3 household or hobby products found")
    return result


def _prices(source: Path, skus: set[str]) -> dict[tuple[str, str], int]:
    result = {}
    for row in _rows(source, {"store_id", "item_id", "wm_yr_wk", "sell_price"}):
        if row["store_id"] != "CA_3" or row["item_id"] not in skus:
            continue
        key = row["item_id"], row["wm_yr_wk"]
        if key in result:
            raise ValueError("duplicate item-store-week price")
        result[key] = _price(row["sell_price"])
    return result


def _history_prices(sku, first_active, calendar, prices):
    previous = None
    result = []
    for day in range(1, TEST_END + 1):
        observed = prices.get((sku, calendar[day]["wm_yr_wk"]))
        if first_active is None or day < first_active:
            result.append((None, False))
            continue
        if observed is not None:
            previous = observed
        result.append((previous, observed is None and previous is not None))
    return result


def _balanced_sizes(count):
    sizes = [count // 3] * 3
    for index in (1, 0, 2)[: count % 3]:
        sizes[index] += 1
    return sizes


def select_products(sales, calendar, prices, rule: SelectionRule) -> tuple[list[dict], int]:
    eligible = []
    threshold = Decimal(rule.minimum_price_coverage)
    for sku, item in sorted(sales.items()):
        training = item["units"][:TRAIN_END]
        first_active = next((i + 1 for i, value in enumerate(training) if value > 0), None)
        if first_active is None:
            continue
        active_days = TRAIN_END - first_active + 1
        if active_days < rule.minimum_active_days:
            continue
        history_prices = _history_prices(sku, first_active, calendar, prices)
        covered = sum(
            value is not None for value, _ in history_prices[first_active - 1 : TRAIN_END]
        )
        if Decimal(covered) / active_days < threshold:
            continue
        last_price = next(
            value for value, _ in reversed(history_prices[:TRAIN_END]) if value is not None
        )
        eligible.append(
            {
                "sku": sku,
                "store_id": "CA_3",
                "category": item["category"],
                "department": item["department"],
                "first_active_day_proxy": first_active,
                "training_sales_units": sum(training),
                "training_mean_sales_per_day": str(
                    (Decimal(sum(training)) / TRAIN_END).quantize(Decimal("0.000001"))
                ),
                "training_active_days": active_days,
                "training_price_covered_days": covered,
                "reference_price_cents": last_price,
            }
        )
    if len(eligible) < rule.product_count:
        raise ValueError(
            f"only {len(eligible)} eligible products; need {rule.product_count}. "
            "Do not relax thresholds using validation/test performance."
        )
    eligible.sort(key=lambda item: (item["training_sales_units"], item["sku"]))
    rng = random.Random(rule.seed)
    selected, offset = [], 0
    for label, size, wanted in zip(
        ("low", "medium", "high"),
        _balanced_sizes(len(eligible)),
        _balanced_sizes(rule.product_count),
        strict=True,
    ):
        group = eligible[offset : offset + size]
        selected.extend({**item, "volume_stratum": label} for item in rng.sample(group, wanted))
        offset += size
    return sorted(selected, key=lambda item: item["sku"]), len(eligible)


def split_manifest(calendar):
    partitions = {}
    for name, start, end in (
        ("training", 1, TRAIN_END),
        ("validation", TRAIN_END + 1, VALIDATION_END),
        ("test", VALIDATION_END + 1, TEST_END),
    ):
        entry = {
            "start_day": start,
            "end_day": end,
            "days": end - start + 1,
            "start_date": calendar[start]["date"],
            "end_date": calendar[end]["date"],
        }
        if name != "training":
            entry["blocks"] = [
                {"forecast_cutoff_day": d - 1, "start_day": d, "end_day": d + 13}
                for d in range(start, end + 1, 14)
            ]
        partitions[name] = entry
    return {"schema_version": "0.1.0", "selection_cutoff_day": TRAIN_END, "partitions": partitions}


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_m5(source_dir: Path, output: Path, rule: SelectionRule = SelectionRule()) -> dict:
    source_dir, output = source_dir.resolve(), output.resolve()
    for name in INPUT_FILES:
        if not (source_dir / name).is_file():
            raise FileNotFoundError(f"missing M5 input: {name}")
    if output == source_dir or output.is_relative_to(source_dir):
        raise ValueError("write curated data outside the raw input directory")
    calendar = _calendar(source_dir / "calendar.csv")
    sales = _sales(source_dir / "sales_train_evaluation.csv")
    prices = _prices(source_dir / "sell_prices.csv", set(sales))
    selected, eligible_count = select_products(sales, calendar, prices, rule)
    output.mkdir(parents=True, exist_ok=True)
    fields = [
        "sku",
        "store_id",
        "category",
        "day",
        "date",
        "weekday",
        "wm_yr_wk",
        "event_name_1",
        "event_name_2",
        "sales_units",
        "price_cents",
        "active",
        "price_imputed",
        "partition",
        "provenance",
    ]
    missing, imputed, before_active, row_count = 0, 0, 0, 0
    with (output / "sales_history.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for item in selected:
            sku, first = item["sku"], item["first_active_day_proxy"]
            resolved = _history_prices(sku, first, calendar, prices)
            for day in range(1, TEST_END + 1):
                cal = calendar[day]
                price, is_imputed = resolved[day - 1]
                partition = (
                    "training"
                    if day <= TRAIN_END
                    else ("validation" if day <= VALIDATION_END else "test")
                )
                writer.writerow(
                    {
                        "sku": sku,
                        "store_id": "CA_3",
                        "category": item["category"],
                        "day": day,
                        "date": cal["date"],
                        "weekday": cal["weekday"],
                        "wm_yr_wk": cal["wm_yr_wk"],
                        "event_name_1": cal.get("event_name_1", ""),
                        "event_name_2": cal.get("event_name_2", ""),
                        "sales_units": sales[sku]["units"][day - 1],
                        "price_cents": "" if price is None else price,
                        "active": int(day >= first),
                        "price_imputed": int(is_imputed),
                        "partition": partition,
                        "provenance": "m5_historical_observation",
                    }
                )
                missing += price is None
                imputed += is_imputed
                before_active += day < first
                row_count += 1
    _write_json(
        output / "product_manifest.json",
        {
            "pipeline_version": PIPELINE_VERSION,
            "selection_rule": asdict(rule),
            "selection_cutoff_day": TRAIN_END,
            "products": selected,
            "active_start_convention": (
                "first positive training sale is a proxy, not verified launch"
            ),
            "reference_price_convention": "last available training price, no future backfill",
        },
    )
    _write_json(output / "split_manifest.json", split_manifest(calendar))
    _write_json(
        output / "data_quality.json",
        {
            "rows": row_count,
            "selected_products": len(selected),
            "candidate_products": len(sales),
            "eligible_products": eligible_count,
            "duplicate_daily_keys": 0,
            "negative_sales": 0,
            "calendar_gaps": 0,
            "missing_price_rows": missing,
            "forward_filled_price_rows": imputed,
            "rows_before_active_proxy": before_active,
            "unresolved_active_price_rows": missing - before_active,
            "checks": [
                "unique daily keys by construction",
                "nonnegative integer sales",
                "unique weekly prices",
                "consecutive calendar",
                "training-only selection",
            ],
            "limitations": [
                "Observed sales are not uncensored demand.",
                "Prices are historical observations, not known future forecast inputs.",
                "Synthetic operating costs, inventory, and supplier records are not in this panel.",
                "Forward filling cannot reveal true prices during missing weeks.",
            ],
        },
    )
    artifact_names = (
        "sales_history.csv",
        "product_manifest.json",
        "split_manifest.json",
        "data_quality.json",
    )
    manifest = {
        "schema_version": "0.1.0",
        "pipeline_version": PIPELINE_VERSION,
        "source": "M5 Forecasting Accuracy",
        "source_url": SOURCE_URL,
        "source_access": "user-provided local files",
        "original_access_date": None,
        "currency": "USD",
        "historical_days": TEST_END,
        "product_count": len(selected),
        "rows": row_count,
        "model_calls": 0,
        "source_checksums": {name: checksum(source_dir / name) for name in INPUT_FILES},
        "artifacts": {name: checksum(output / name) for name in artifact_names},
    }
    _write_json(output / "manifest.json", manifest)
    return manifest
