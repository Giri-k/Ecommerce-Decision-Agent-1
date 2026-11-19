import csv
import hashlib
import json
from collections import Counter
from datetime import date, timedelta

import pytest

from ecommerce_agent.data import TEST_END, TRAIN_END, SelectionRule, prepare_m5


def write_csv(path, fields, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.fixture
def m5_source(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    origin = date(2011, 1, 29)
    calendar = []
    for day in range(1, TEST_END + 1):
        current = origin + timedelta(days=day - 1)
        calendar.append(
            {
                "d": f"d_{day}",
                "date": current.isoformat(),
                "weekday": current.strftime("%A"),
                "wm_yr_wk": str(10000 + (day - 1) // 7),
            }
        )
    write_csv(root / "calendar.csv", list(calendar[0]), calendar)
    sales = []
    prices = []
    for index in range(10):
        sku = f"HOUSEHOLD_1_{index:03d}"
        sales.append(
            {
                "item_id": sku,
                "store_id": "CA_3",
                "cat_id": "HOUSEHOLD",
                "dept_id": "HOUSEHOLD_1",
                **{f"d_{day}": index + 1 for day in range(1, TEST_END + 1)},
            }
        )
        prices.extend(
            {"store_id": "CA_3", "item_id": sku, "wm_yr_wk": str(week), "sell_price": "10.00"}
            for week in range(10000, 10000 + (TEST_END - 1) // 7 + 1)
        )
    write_csv(root / "sales_train_evaluation.csv", list(sales[0]), sales)
    write_csv(root / "sell_prices.csv", list(prices[0]), prices)
    return root


def test_curated_panel_reproducible_with_exact_splits_and_manifest(m5_source, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    manifest = prepare_m5(m5_source, first)
    assert prepare_m5(m5_source, second) == manifest
    for name in (*manifest["artifacts"], "manifest.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    for name, expected in manifest["artifacts"].items():
        assert hashlib.sha256((first / name).read_bytes()).hexdigest() == expected
    rows = read_csv(first / "sales_history.csv")
    assert len(rows) == 19410
    assert len({(r["sku"], r["day"]) for r in rows}) == 19410
    assert Counter(r["partition"] for r in rows) == {
        "training": 18010,
        "validation": 840,
        "test": 560,
    }
    assert {r["store_id"] for r in rows} == {"CA_3"}
    products = json.loads((first / "product_manifest.json").read_text())["products"]
    assert Counter(p["volume_stratum"] for p in products) == {"low": 3, "medium": 4, "high": 3}
    split = json.loads((first / "split_manifest.json").read_text())["partitions"]
    assert len(split["validation"]["blocks"]) == 6
    assert len(split["test"]["blocks"]) == 4
    assert split["validation"]["blocks"][0]["forecast_cutoff_day"] == 1801
    assert split["test"]["blocks"][-1]["end_day"] == 1941


def test_future_sales_and_prices_do_not_change_selection_or_training_fields(m5_source, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    prepare_m5(m5_source, first)
    path = m5_source / "sales_train_evaluation.csv"
    sales = read_csv(path)
    for row in sales:
        for day in range(TRAIN_END + 1, TEST_END + 1):
            row[f"d_{day}"] = "900000"
    write_csv(path, list(sales[0]), reversed(sales))
    path = m5_source / "sell_prices.csv"
    prices = read_csv(path)
    for row in prices:
        if int(row["wm_yr_wk"]) > 10000 + (TRAIN_END - 1) // 7:
            row["sell_price"] = "999.00"
    write_csv(path, list(prices[0]), reversed(prices))
    prepare_m5(m5_source, second)
    assert (first / "product_manifest.json").read_bytes() == (
        second / "product_manifest.json"
    ).read_bytes()
    training_a = [r for r in read_csv(first / "sales_history.csv") if r["partition"] == "training"]
    training_b = [r for r in read_csv(second / "sales_history.csv") if r["partition"] == "training"]
    assert training_a == training_b


def test_prices_never_backfill_and_forward_fill_is_flagged(m5_source, tmp_path):
    sku = "HOUSEHOLD_1_000"
    path = m5_source / "sales_train_evaluation.csv"
    sales = read_csv(path)
    for day in range(1, 100):
        sales[0][f"d_{day}"] = "0"
    write_csv(path, list(sales[0]), sales)
    path = m5_source / "sell_prices.csv"
    prices = read_csv(path)
    missing_week = 10000 + (200 - 1) // 7
    prices = [
        r
        for r in prices
        if not (
            r["item_id"] == sku
            and (int(r["wm_yr_wk"]) < 10018 or int(r["wm_yr_wk"]) == missing_week)
        )
    ]
    write_csv(path, list(prices[0]), prices)
    output = tmp_path / "curated"
    prepare_m5(m5_source, output)
    rows = {int(r["day"]): r for r in read_csv(output / "sales_history.csv") if r["sku"] == sku}
    assert all(rows[d]["price_cents"] == "" for d in range(1, 127))
    assert rows[99]["active"] == "0"
    assert rows[100]["active"] == "1"
    assert rows[127]["price_cents"] == "1000"
    assert rows[200]["price_cents"] == "1000"
    assert rows[200]["price_imputed"] == "1"
    assert rows[207]["price_imputed"] == "0"


def test_duplicate_weekly_prices_rejected_before_output(m5_source, tmp_path):
    path = m5_source / "sell_prices.csv"
    prices = read_csv(path)
    write_csv(path, list(prices[0]), prices + [prices[0]])
    with pytest.raises(ValueError, match="duplicate"):
        prepare_m5(m5_source, tmp_path / "curated")
    assert not (tmp_path / "curated").exists()


def test_negative_sales_and_calendar_gaps_rejected(m5_source, tmp_path):
    path = m5_source / "sales_train_evaluation.csv"
    sales = read_csv(path)
    sales[0]["d_7"] = "-1"
    write_csv(path, list(sales[0]), sales)
    with pytest.raises(ValueError, match="nonnegative"):
        prepare_m5(m5_source, tmp_path / "curated")
    sales[0]["d_7"] = "1"
    write_csv(path, list(sales[0]), sales)
    path = m5_source / "calendar.csv"
    calendar = read_csv(path)
    write_csv(path, list(calendar[0]), calendar[:-1])
    with pytest.raises(ValueError, match="cover"):
        prepare_m5(m5_source, tmp_path / "curated")


def test_insufficient_eligible_products_fails_without_relaxing_rule(m5_source, tmp_path):
    path = m5_source / "sales_train_evaluation.csv"
    sales = read_csv(path)
    for day in range(1, TRAIN_END + 1):
        sales[0][f"d_{day}"] = "0"
    write_csv(path, list(sales[0]), sales)
    with pytest.raises(ValueError, match="only 9 eligible"):
        prepare_m5(m5_source, tmp_path / "curated")


def test_missing_files_and_raw_directory_outputs_rejected(m5_source, tmp_path):
    with pytest.raises(FileNotFoundError, match="missing M5 input"):
        prepare_m5(tmp_path / "absent", tmp_path / "curated")
    with pytest.raises(ValueError, match="outside"):
        prepare_m5(m5_source, m5_source / "curated")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.1", "1.1", "nonsense"])
def test_invalid_selection_thresholds(value):
    with pytest.raises(ValueError):
        SelectionRule(minimum_price_coverage=value)


def test_forecast_validation_reads_no_final_test_targets_and_flags_zero_mase_scale(
    m5_source, tmp_path
):
    from ecommerce_agent.data import checksum
    from ecommerce_agent.forecasting import load_history, validate_forecasts

    curated = tmp_path / "curated"
    prepare_m5(m5_source, curated)
    path = curated / "sales_history.csv"
    rows = read_csv(path)
    for row in rows:
        if row["partition"] == "test":
            row["sales_units"] = "FINAL_TEST_MUST_NOT_BE_PARSED"
            row["price_cents"] = "FINAL_TEST_MUST_NOT_BE_PARSED"
    write_csv(path, list(rows[0]), rows)
    manifest = json.loads((curated / "manifest.json").read_text())
    manifest["artifacts"]["sales_history.csv"] = checksum(path)
    (curated / "manifest.json").write_text(json.dumps(manifest))
    report = validate_forecasts(curated, tmp_path / "forecasts")
    assert report["final_test_evaluated"] is False
    assert report["blocks"] == 6
    for metrics in report["metrics"].values():
        assert metrics["mae"] == 0
        assert metrics["wape"] == 0
        assert all(m["mase_lag7"] is None for m in metrics["per_sku"].values())
    with pytest.raises(ValueError, match="final-test"):
        load_history(curated, max_day=1941)
