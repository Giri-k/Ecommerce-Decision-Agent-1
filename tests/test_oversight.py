"""Real PostgreSQL integration tests. No simulated database or skipped security checks."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from ecommerce_agent.api import create_app
from ecommerce_agent.config import load_config
from ecommerce_agent.contracts import CreatePurchaseOrderAction, SetPriceAction
from ecommerce_agent.durable_simulator import checkpoint, restore
from ecommerce_agent.fixtures import build_fixture
from ecommerce_agent.local_database import start_database
from ecommerce_agent.local_setup import seed_identities
from ecommerce_agent.oversight import Oversight
from ecommerce_agent.persistence import (
    ServiceError,
    create_database,
    migrate,
    provision_runtime_roles,
    seed_store,
    transaction,
)
from ecommerce_agent.simulation import Initialization, Scenario, Simulator, World
from ecommerce_agent.workers import Worker


@pytest.fixture(scope="session")
def postgres_server():
    return start_database()


@pytest.fixture
def db_env(postgres_server, tmp_path, request):
    database = "ec_test_" + uuid4().hex[:12]
    admin = create_database(postgres_server, database)
    migrate(admin)
    config = load_config()
    parameters = getattr(request, "param", {})
    for tenant in ("store-a", "store-b"):
        data = build_fixture(config=config).model_dump()
        data.update(tenant_id=tenant, cash_cents=parameters.get("cash", 10000))
        for p in data["products"]:
            p.update(price_cents=200, reference_price_cents=200, cost_cents=100)
        for row in data["inventory"]:
            row["stock"] = parameters.get("stock", 10)
        from ecommerce_agent.contracts import StoreSnapshot

        snapshot = StoreSnapshot.model_validate(data)
        world = World(
            Scenario(scenario_id=f"test-{tenant}"),
            date(2026, 1, 1),
            {p.sku: (1.0,) * 7 for p in snapshot.products},
            {p.sku: -1.0 for p in snapshot.products},
        )
        initial = Initialization(snapshot, world)
        engine = Simulator(initial, config)
        seed_store(admin, replace(initial, snapshot=engine.observe()), config, checkpoint(engine))
    services = provision_runtime_roles(admin, ["store-a", "store-b"], tmp_path / "services.json")
    tokens = seed_identities(admin)
    env = {
        "admin": admin,
        "services": services,
        "tokens": tokens,
        "api": Oversight(services["api"]),
        "agent": Oversight(services["agent_0"]),
        "config": config,
    }
    try:
        yield env
    finally:
        with psycopg.connect(postgres_server, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
            for dsn in services.values():
                conn.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(conninfo_to_dict(dsn)["user"]))
                )


def proposal(env, actions=None, token=None):
    token = token or env["tokens"]["merchant-a"]
    snapshot = env["api"].get_store(token, "store-a")["snapshot"]
    run = env["api"].investigate(token, "store-a", "Inspect the current stock", str(uuid4()))
    actions = actions or [CreatePurchaseOrderAction(sku=snapshot["products"][0]["sku"], quantity=5)]
    result = env["agent"].propose(
        "store-a",
        run["run_id"],
        actions,
        ["test:inventory"],
        "Integration fixture",
        str(uuid4()),
        expected_snapshot_version=snapshot["snapshot_version"],
        expected_policy_version=snapshot["policy_version"],
    )
    return result


def approve(env, plan, token=None):
    return env["api"].decide(
        token or env["tokens"]["merchant-a"],
        "store-a",
        plan["proposal_id"],
        plan["payload_hash"],
        str(uuid4()),
    )


def enable(env):
    env["api"].set_execution(env["tokens"]["merchant-a"], "store-a", True)


def executor(env, **kwargs):
    return Worker(env["services"]["executor_0"], "store-a", **kwargs)


def snapshot(env):
    return env["api"].get_store(env["tokens"]["merchant-a"], "store-a")["snapshot"]


def count(env, table):
    with transaction(env["admin"]) as conn:
        return conn.execute(
            sql.SQL("SELECT count(*) AS n FROM app.{}").format(sql.Identifier(table))
        ).fetchone()["n"]


def test_authenticated_api_create_approve_execute_and_read_receipt(db_env):
    e = db_env
    plan = proposal(e)
    headers = {
        "Authorization": "Bearer " + e["tokens"]["merchant-a"],
        "X-Tenant-ID": "store-a",
        "Idempotency-Key": "approve-1",
    }
    with TestClient(create_app(e["services"]["api"])) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/me", headers=headers).json()["user_id"] == "merchant-a"
        assert client.get("/proposals/" + plan["proposal_id"], headers=headers).status_code == 200
        response = client.post(
            "/proposals/" + plan["proposal_id"] + "/approve",
            headers=headers,
            json={"payload_hash": plan["payload_hash"]},
        )
        assert response.status_code == 202
        assert (
            client.post(
                "/proposals/" + plan["proposal_id"] + "/approve",
                headers=headers,
                json={"payload_hash": plan["payload_hash"]},
            ).json()
            == response.json()
        )
        enable(e)
        receipt = executor(e).run_once()
        assert receipt["approver_user_id"] == "merchant-a"
        got = client.get("/executions/" + receipt["execution_id"], headers=headers)
        assert got.status_code == 200 and got.json() == receipt
    assert count(e, "executions") == count(e, "purchase_orders") == 1


def test_default_kill_switch_and_pause_after_lease_are_enforced(db_env):
    e = db_env
    plan = proposal(e)
    approve(e, plan)
    assert executor(e).claim() is None
    enable(e)
    worker = executor(e)
    lease = worker.claim()
    e["api"].set_execution(e["tokens"]["merchant-a"], "store-a", False)
    before = snapshot(e)
    with pytest.raises(ServiceError, match="paused"):
        worker.execute(**lease)
    assert snapshot(e) == before and count(e, "executions") == 0


def test_forged_identity_hash_extra_approval_fields_and_viewer_denied(db_env):
    e = db_env
    plan = proposal(e)
    with TestClient(create_app(e["services"]["api"])) as client:
        assert client.get("/me").status_code == 401
        assert client.get("/me", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
        headers = {
            "Authorization": "Bearer " + e["tokens"]["viewer-a"],
            "X-Tenant-ID": "store-a",
            "Idempotency-Key": "x",
        }
        url = "/proposals/" + plan["proposal_id"] + "/approve"
        assert (
            client.post(
                url, headers=headers, json={"payload_hash": plan["payload_hash"]}
            ).status_code
            == 403
        )
        headers["Authorization"] = "Bearer " + e["tokens"]["merchant-a"]
        assert client.post(url, headers=headers, json={"payload_hash": "0" * 64}).status_code == 409
        assert (
            client.post(
                url,
                headers=headers,
                json={
                    "payload_hash": plan["payload_hash"],
                    "approved": True,
                    "approver": "merchant-a",
                },
            ).status_code
            == 422
        )
    assert count(e, "approvals") == count(e, "execution_jobs") == 0


def test_cross_tenant_reads_and_approvals_are_denied(db_env):
    e = db_env
    plan = proposal(e)
    token = e["tokens"]["merchant-b"]
    with pytest.raises(ServiceError) as error:
        e["api"].get_proposal(token, "store-b", plan["proposal_id"])
    assert error.value.status == 403
    with pytest.raises(ServiceError):
        e["api"].get_store(token, "store-a")
    with pytest.raises(ServiceError):
        e["api"].decide(token, "store-a", plan["proposal_id"], plan["payload_hash"], "cross")


def test_actual_agent_role_cannot_mutate_approve_read_truth_or_escalate(db_env):
    e = db_env
    for query in (
        "UPDATE app.stores SET snapshot=snapshot",
        "INSERT INTO app.approvals SELECT * FROM app.approvals",
        "SELECT * FROM evaluator.sandbox_state",
        "SET ROLE ec_executor",
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            transaction(e["services"]["agent_0"]) as conn,
        ):
            conn.execute(query)
    with transaction(e["services"]["agent_0"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id','store-b',true)")
        conn.execute("SELECT set_config('app.user_id','merchant-b',true)")
        from ecommerce_agent.persistence import token_hash

        conn.execute(
            "SELECT set_config('app.token_hash',%s,true)", (token_hash(e["tokens"]["merchant-b"]),)
        )
        rows = conn.execute("SELECT tenant_id FROM app.stores").fetchall()
        assert [r["tenant_id"] for r in rows] == ["store-a"]


def test_roles_are_nonowner_nonsuperuser_and_cannot_change_execution_switch(db_env):
    e = db_env
    for dsn in e["services"].values():
        with transaction(dsn) as conn:
            role = conn.execute(
                "SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole "
                "FROM pg_roles WHERE rolname=session_user"
            ).fetchone()
            assert not any(role.values())
    for role in ("api", "executor_0", "simulator_0"):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            transaction(e["services"][role]) as conn,
        ):
            conn.execute("UPDATE app.stores SET execution_enabled=true")


def test_database_payload_and_audit_immutability(db_env):
    e = db_env
    plan = proposal(e)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), transaction(e["admin"]) as conn:
        conn.execute(
            "UPDATE app.proposals SET payload=payload || '{\"forged\":true}'::jsonb "
            "WHERE proposal_id=%s",
            (UUID(plan["proposal_id"]),),
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), transaction(e["admin"]) as conn:
        conn.execute("UPDATE app.audit_events SET kind='forged'")


def test_idempotency_conflict_and_concurrent_duplicate_approval(db_env):
    e = db_env
    token = e["tokens"]["merchant-a"]
    a = e["api"].investigate(token, "store-a", "one", "same-key")
    assert e["api"].investigate(token, "store-a", "one", "same-key") == a
    with pytest.raises(ServiceError, match="different input"):
        e["api"].investigate(token, "store-a", "two", "same-key")
    plan = proposal(e)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: approve(e, plan), range(2)))
    assert results[0]["job_id"] == results[1]["job_id"]
    assert count(e, "approvals") == count(e, "execution_jobs") == 1


def test_concurrent_approve_reject_has_one_winning_decision(db_env):
    e = db_env
    plan = proposal(e)
    token = e["tokens"]["merchant-a"]

    def decide(approve_flag):
        try:
            return e["api"].decide(
                token,
                "store-a",
                plan["proposal_id"],
                plan["payload_hash"],
                str(uuid4()),
                approve_flag,
            )["status"]
        except ServiceError:
            return "CONFLICT"

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(decide, [True, False]))
    assert result.count("CONFLICT") == 1
    winner = (set(result) - {"CONFLICT"}).pop()
    assert count(e, "execution_jobs") == int(winner == "APPROVED")


def test_duplicate_execution_returns_one_durable_receipt(db_env):
    e = db_env
    approve(e, proposal(e))
    enable(e)
    lease = executor(e).claim()
    with ThreadPoolExecutor(2) as pool:
        receipts = list(pool.map(lambda _: executor(e).execute(**lease), range(2)))
    assert receipts[0] == receipts[1]
    assert count(e, "executions") == count(e, "purchase_orders") == 1


@pytest.mark.parametrize("db_env", [{"cash": 1100, "stock": 0}], indirect=True)
def test_two_orders_contending_for_cash_do_not_overspend(db_env):
    e = db_env
    first = proposal(e)
    second = proposal(e)
    approve(e, first)
    approve(e, second)
    enable(e)
    executor(e).run_once()
    with pytest.raises(ServiceError, match="stale"):
        executor(e).run_once()
    assert count(e, "executions") == 1
    assert snapshot(e)["cash_cents"] == 100


def test_expired_proposal_and_revoked_session_do_not_execute(db_env, monkeypatch):
    e = db_env
    plan = proposal(e)
    approve(e, plan)
    enable(e)

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(minutes=16)

    import ecommerce_agent.oversight as oversight_module

    monkeypatch.setattr(oversight_module, "datetime", Future)
    with pytest.raises(ServiceError, match="expired"):
        executor(e).run_once()
    assert count(e, "executions") == 0
    assert (
        e["api"].get_proposal(e["tokens"]["merchant-a"], "store-a", plan["proposal_id"])["status"]
        == "EXPIRED"
    )


def test_membership_revocation_rechecked_at_execution(db_env):
    e = db_env
    plan = proposal(e, token=e["tokens"]["approver-a"])
    approve(e, plan, token=e["tokens"]["approver-a"])
    enable(e)
    with transaction(e["admin"]) as conn:
        conn.execute("UPDATE authn.memberships SET role='viewer' WHERE user_id='approver-a'")
    with pytest.raises(ServiceError, match="no longer authorized"):
        executor(e).run_once()
    assert count(e, "executions") == 0


def test_revoked_bearer_session_is_denied(db_env):
    e = db_env
    with transaction(e["admin"]) as conn:
        conn.execute("UPDATE authn.sessions SET revoked=true WHERE user_id='merchant-a'")
    with pytest.raises(ServiceError) as error:
        e["api"].me(e["tokens"]["merchant-a"])
    assert error.value.status == 401


def test_crash_before_commit_rolls_back_and_reclaimed_job_succeeds(db_env):
    e = db_env
    approve(e, proposal(e))
    enable(e)
    before = snapshot(e)

    def crash(stage):
        if stage == "before_commit":
            raise RuntimeError("simulated process death")

    worker = executor(e, failpoint=crash)
    lease = worker.claim()
    with pytest.raises(RuntimeError, match="death"):
        worker.execute(**lease)
    assert snapshot(e) == before and count(e, "executions") == count(e, "purchase_orders") == 0
    with transaction(e["admin"]) as conn:
        conn.execute(
            "UPDATE app.execution_jobs SET lease_expires_at=clock_timestamp()-interval '1 second'"
        )
    assert executor(e).run_once()["effects"]["new_orders"]
    assert count(e, "executions") == 1


def test_crash_after_commit_retry_returns_recorded_receipt(db_env):
    e = db_env
    approve(e, proposal(e))
    enable(e)

    def crash(stage):
        if stage == "after_commit":
            raise RuntimeError("notification lost")

    worker = executor(e, failpoint=crash)
    lease = worker.claim()
    with pytest.raises(RuntimeError, match="lost"):
        worker.execute(**lease)
    receipt = executor(e).execute(**lease)
    assert receipt["effects"]["new_orders"]
    assert count(e, "executions") == count(e, "purchase_orders") == 1
    assert executor(e).claim() is None


def test_reclaimed_lease_fences_old_worker_and_heartbeat(db_env):
    e = db_env
    approve(e, proposal(e))
    enable(e)
    a = executor(e)
    old = a.claim()
    a.heartbeat(**old)
    with transaction(e["admin"]) as conn:
        conn.execute(
            "UPDATE app.execution_jobs SET lease_expires_at=clock_timestamp()-interval '1 second'"
        )
    b = executor(e)
    current = b.claim()
    with pytest.raises(ServiceError, match="lease"):
        a.execute(**old)
    a.release(**old, error=ServiceError("stale", "old worker"))
    assert b.execute(**current)["effects"]["new_orders"]
    with pytest.raises(ServiceError, match="lease"):
        a.heartbeat(**old)


def test_retry_limit_leaves_explicit_failed_job(db_env):
    e = db_env
    approve(e, proposal(e))
    enable(e)
    worker = executor(e)
    worker.claim()
    with transaction(e["admin"]) as conn:
        conn.execute(
            "UPDATE app.execution_jobs SET attempts=max_attempts,"
            "lease_expires_at=clock_timestamp()-interval '1 second'"
        )
    assert worker.claim() is None
    with transaction(e["admin"]) as conn:
        row = conn.execute("SELECT state,last_error FROM app.execution_jobs").fetchone()
    assert row == {"state": "FAILED", "last_error": "retry_limit"}


def test_store_advance_invalidates_old_approval_and_uses_private_checkpoint(db_env):
    e = db_env
    plan = proposal(e)
    approve(e, plan)
    enable(e)
    token = e["tokens"]["merchant-a"]
    queued = e["api"].request_advance(token, "store-a", snapshot(e)["snapshot_version"], "advance")
    worker = Worker(e["services"]["simulator_0"], "store-a", "simulator")
    lease = worker.claim()
    result = worker.execute(**lease)
    assert result["day"] == 2 and result["job_id"] == queued["job_id"]
    assert worker.execute(**lease) == result
    with pytest.raises(ServiceError, match="day changed"):
        executor(e).run_once()
    assert count(e, "executions") == 0


def test_approved_order_reconciles_into_durable_simulation_once(db_env):
    e = db_env
    approve(e, proposal(e))
    enable(e)
    receipt = executor(e).run_once()
    token = e["tokens"]["merchant-a"]
    for step in range(3):
        e["api"].request_advance(token, "store-a", snapshot(e)["snapshot_version"], str(step))
        Worker(e["services"]["simulator_0"], "store-a", "simulator").run_once()
    with transaction(e["admin"]) as conn:
        private = conn.execute(
            "SELECT * FROM evaluator.sandbox_state WHERE tenant_id='store-a'"
        ).fetchone()
        orders = conn.execute(
            "SELECT data FROM app.purchase_orders WHERE tenant_id='store-a'"
        ).fetchall()
    engine = restore(private["checkpoint"])
    assert engine._day == 4
    assert engine._received[receipt["effects"]["new_orders"][0]["sku"]] == 5
    assert sum(r["purchase_principal_cents"] for r in engine.rows) == 500
    assert sum(r["order_fee_cents"] for r in engine.rows) == 500
    assert len(private["applied_execution_ids"]) == 1
    assert orders[0]["data"]["status"] == "received"


def test_cross_tenant_trace_and_checkpoint_reads_are_denied(db_env):
    e = db_env
    token = e["tokens"]["merchant-b"]
    run = e["api"].investigate(token, "store-b", "other scope", "run")
    with transaction(e["services"]["agent_1"]) as conn:
        for kind in ("trace", "checkpoint"):
            conn.execute(
                "INSERT INTO app.thread_records VALUES(%s,%s,%s,%s,%s)",
                ("store-b", run["run_id"], 1, kind, Jsonb({"marker": "other-tenant"})),
            )
    with transaction(e["services"]["agent_0"]) as conn:
        assert not conn.execute(
            "SELECT * FROM app.thread_records WHERE run_id=%s", (run["run_id"],)
        ).fetchall()
    with pytest.raises(ServiceError):
        e["api"].get_investigation(e["tokens"]["merchant-a"], "store-a", run["run_id"])


def test_changed_policy_rejects_entire_approved_order_price_bundle(db_env):
    e = db_env
    before = snapshot(e)
    sku = before["products"][0]["sku"]
    plan = proposal(
        e,
        [CreatePurchaseOrderAction(sku=sku, quantity=5), SetPriceAction(sku=sku, price_cents=210)],
    )
    approve(e, plan)
    enable(e)
    with transaction(e["admin"]) as conn:
        config = e["config"].model_dump(mode="json")
        conn.execute(
            "INSERT INTO app.policy_versions VALUES('store-a','new-policy',%s)", (Jsonb(config),)
        )
        changed = before | {
            "policy_version": "new-policy",
            "snapshot_version": before["snapshot_version"] + 1,
        }
        conn.execute(
            "UPDATE app.stores SET policy_version='new-policy',snapshot=%s "
            "WHERE tenant_id='store-a'",
            (Jsonb(changed),),
        )
    with pytest.raises(ServiceError, match="stale"):
        executor(e).run_once()
    assert snapshot(e) == changed and count(e, "purchase_orders") == 0


def test_sandbox_advance_and_execution_race_have_no_lost_updates(db_env):
    e = db_env
    approve(e, proposal(e))
    enable(e)
    e["api"].request_advance(e["tokens"]["merchant-a"], "store-a", 1, "advance")
    workers = [executor(e), Worker(e["services"]["simulator_0"], "store-a", "simulator")]
    leases = [w.claim() for w in workers]

    def execute(pair):
        w, lease = pair
        try:
            return w.execute(**lease)
        except ServiceError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(execute, zip(workers, leases, strict=True)))
    assert sum(r is not None for r in results) == 1
    assert (snapshot(e)["day"], count(e, "executions")) in {(2, 0), (1, 1)}


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_simulator_crash_recovery_publishes_only_completed_sales_once(db_env, stage):
    e = db_env
    enable(e)
    token = e["tokens"]["merchant-a"]
    e["api"].request_advance(token, "store-a", 1, "day-1")

    def crash(point):
        if point == stage:
            raise RuntimeError("injected process interruption")

    worker = Worker(e["services"]["simulator_0"], "store-a", "simulator", failpoint=crash)
    lease = worker.claim()
    with pytest.raises(RuntimeError, match="interruption"):
        worker.execute(**lease)
    assert count(e, "observed_sales") == (0 if stage == "before_commit" else 10)
    if stage == "before_commit":
        assert snapshot(e)["day"] == 1
        with transaction(e["admin"]) as conn:
            conn.execute(
                "UPDATE app.sandbox_jobs SET lease_expires_at=clock_timestamp()-interval '1 second'"
            )
        recovered = Worker(e["services"]["simulator_0"], "store-a", "simulator").run_once()
    else:
        recovered = Worker(e["services"]["simulator_0"], "store-a", "simulator").execute(**lease)
    assert recovered["day"] == snapshot(e)["day"] == 2
    public = e["api"].observed_sales(token, "store-a")
    assert public["through_day"] == 1 and len(public["records"]) == 10
    assert all(row["day"] == 1 and row["sales_units"] >= 0 for row in public["records"])
    with transaction(e["services"]["agent_1"]) as conn:
        assert not conn.execute(
            "SELECT * FROM app.observed_sales WHERE tenant_id='store-a'"
        ).fetchall()
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        transaction(e["services"]["agent_0"]) as conn,
    ):
        conn.execute("DELETE FROM app.observed_sales")
