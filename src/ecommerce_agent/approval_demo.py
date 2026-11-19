"""Real local approval/execution integration using the deterministic rules control.

This is a development workflow check, not an agent or an economic evaluation.
Every run creates a separate database and preserves its receipt for inspection.
"""

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from ecommerce_agent.api import create_app
from ecommerce_agent.config import ProjectConfig, load_config
from ecommerce_agent.contracts import StoreSnapshot
from ecommerce_agent.controls import DeterministicControl
from ecommerce_agent.forecasting import load_history
from ecommerce_agent.local_setup import bootstrap
from ecommerce_agent.oversight import Oversight
from ecommerce_agent.persistence import transaction
from ecommerce_agent.workers import Worker


def run_approval_demo(data_dir=Path("var/data/m5"), output=Path("var/approval-demo")):
    run_label = uuid4().hex[:12]
    runtime_dir = output / "runs" / run_label
    settings = load_config().model_dump()
    # Declared workflow fixture: one day of inventory triggers a replenishment review.
    # Normal development scenarios and their policy comparisons remain separate.
    settings["simulation"].update(initial_inventory_days_min=1, initial_inventory_days_max=1)
    config = ProjectConfig.model_validate(settings)
    env = bootstrap("ec_demo_" + run_label, runtime_dir, data_dir, config=config)
    services, tokens = env["services"], env["sessions"]
    token, tenant = tokens["merchant-a"], "store-a"
    service = Oversight(services["api"])
    before = StoreSnapshot.model_validate(service.get_store(token, tenant)["snapshot"])
    control = DeterministicControl("rules", load_history(data_dir, tenant_id=tenant), config)
    actions = control.decide(before)
    decision = control.decisions[-1]
    if not actions:
        raise ValueError("this development scenario produced a no-op; no approval was fabricated")
    headers = {"Authorization": "Bearer " + token, "X-Tenant-ID": tenant}
    with TestClient(create_app(services["api"])) as client:
        response = client.post(
            "/investigations",
            headers=headers | {"Idempotency-Key": "demo-investigation"},
            json={"question": "Review inventory with the deterministic rules control."},
        )
        response.raise_for_status()
        investigation = response.json()
        with transaction(services["agent_0"]) as conn:
            for index, trace in enumerate(decision["tool_traces"], start=1):
                conn.execute(
                    "INSERT INTO app.thread_records VALUES(%s,%s,%s,'trace',%s)",
                    (tenant, investigation["run_id"], index, Jsonb(trace)),
                )
        proposal = Oversight(services["agent_0"]).propose(
            tenant,
            investigation["run_id"],
            actions,
            decision["evidence_ids"],
            decision["explanation"],
            "demo-proposal",
            expected_snapshot_version=before.snapshot_version,
            expected_policy_version=before.policy_version,
        )
        approval_headers = headers | {"Idempotency-Key": "demo-approval"}
        body = {"payload_hash": proposal["payload_hash"]}
        route = "/proposals/" + proposal["proposal_id"] + "/approve"
        response = client.post(route, headers=approval_headers, json=body)
        response.raise_for_status()
        approval = response.json()
        replay = client.post(route, headers=approval_headers, json=body)
        replay.raise_for_status()
        assert replay.json() == approval
        paused = Worker(services["executor_0"], tenant)
        assert paused.claim() is None
        response = client.post("/admin/execution", headers=headers, json={"enabled": True})
        response.raise_for_status()
        worker = Worker(services["executor_0"], tenant)
        lease = worker.claim()
        if lease is None:
            raise RuntimeError("approved demonstration job was not claimable")
        receipt = worker.execute(**lease)
        assert worker.execute(**lease) == receipt
        response = client.get("/executions/" + receipt["execution_id"], headers=headers)
        response.raise_for_status()
        assert response.json() == receipt
        # A new worker object and connection on every tick exercise durable restoration.
        published = []
        for index in range(3):
            visible = service.get_store(token, tenant)["snapshot"]
            response = client.post(
                "/sandbox/advance",
                headers=headers | {"Idempotency-Key": "demo-tick-" + str(index)},
                json={"snapshot_version": visible["snapshot_version"]},
            )
            response.raise_for_status()
            published.append(Worker(services["simulator_0"], tenant, "simulator").run_once())
        response = client.post("/admin/execution", headers=headers, json={"enabled": False})
        response.raise_for_status()
    report = {
        "status": "passed",
        "scope": "development_rules_approval_execution_flow",
        "fixture_initial_inventory_days": 1,
        "database": env["database"],
        "runtime_directory": env["runtime_dir"],
        "tenant_id": tenant,
        "run_id": investigation["run_id"],
        "proposal": proposal,
        "approval": approval,
        "actions": decision["actions"],
        "tool_trace_count": len(decision["tool_traces"]),
        "receipt": receipt,
        "sandbox_advances": published,
        "duplicate_approval_reused": True,
        "duplicate_execution_reused": True,
        "execution_enabled_at_end": False,
        "model_calls": 0,
        "final_test_evaluated": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "latest-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (runtime_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return {
        key: report[key]
        for key in (
            "status",
            "scope",
            "database",
            "tool_trace_count",
            "model_calls",
            "duplicate_approval_reused",
            "duplicate_execution_reused",
            "execution_enabled_at_end",
            "final_test_evaluated",
        )
    } | {"report": str(output / "latest-report.json")}
