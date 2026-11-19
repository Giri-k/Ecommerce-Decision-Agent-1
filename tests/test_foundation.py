import hashlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ecommerce_agent.api import create_app
from ecommerce_agent.config import ProjectConfig, load_config
from ecommerce_agent.contracts import StoreSnapshot
from ecommerce_agent.fixtures import build_fixture, write_fixture


def test_fixture_files_reproduce_and_manifest_matches_bytes(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    manifest = write_fixture(first)
    assert write_fixture(second) == manifest
    for filename in ("store_snapshot.json", "manifest.json"):
        assert (first / filename).read_bytes() == (second / filename).read_bytes()
    raw = (first / "store_snapshot.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest["files"]["store_snapshot.json"]
    snapshot = StoreSnapshot.model_validate_json(raw)
    assert len(snapshot.products) == 10
    assert len(snapshot.suppliers) == 2
    assert all(p.cost_cents * 100 <= p.price_cents * 80 for p in snapshot.products)
    assert manifest["source_dataset"] is None
    assert manifest["forecast_evaluation_eligible"] is False


def test_seed_changes_fixture_and_config_is_recorded(tmp_path):
    assert build_fixture(seed=1) != build_fixture(seed=2)
    settings = load_config()
    changed = settings.model_dump()
    changed["operating"]["initial_cash_cents"] = 10000
    new_settings = ProjectConfig.model_validate(changed)
    assert build_fixture(config=new_settings).cash_cents == 10000
    original = write_fixture(tmp_path / "original", config=settings)
    updated = write_fixture(tmp_path / "updated", config=new_settings)
    assert original["config_sha256"] != updated["config_sha256"]


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("model", "monthly_budget_cents", -1),
        ("model", "reserve_cents", 2500),
        ("model", "max_compare_calls", 13),
        ("operating", "case_pack", True),
        ("operating", "maximum_order_units", 203),
        ("simulation", "review_days", [1, 1, 4]),
        ("simulation", "review_days", [1, 15]),
    ],
)
def test_incoherent_config_rejected(section, field, value):
    payload = load_config().model_dump()
    payload[section][field] = value
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(payload)


def test_health_and_missing_database_readiness(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}


def test_readiness_failure_redacts_connection_details():
    with TestClient(
        create_app("postgresql://private-user:private-secret@127.0.0.1:1/db")
    ) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "private" not in response.text
