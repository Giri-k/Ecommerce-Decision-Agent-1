"""Administrator-only local seed. Runtime services never receive this credential."""

import json
from dataclasses import replace
from pathlib import Path

from ecommerce_agent.config import load_config
from ecommerce_agent.durable_simulator import checkpoint
from ecommerce_agent.local_database import private_json, start_database
from ecommerce_agent.persistence import (
    create_database,
    issue_session,
    migrate,
    provision_runtime_roles,
    seed_store,
    transaction,
)
from ecommerce_agent.simulation import Scenario, Simulator, initialize_store


def seed_identities(admin_dsn):
    users = {
        "merchant-a": ("store-a", "admin"),
        "approver-a": ("store-a", "approver"),
        "viewer-a": ("store-a", "viewer"),
        "merchant-b": ("store-b", "admin"),
    }
    with transaction(admin_dsn) as conn:
        for user, (tenant, role) in users.items():
            conn.execute(
                (
                    "INSERT INTO authn.users(user_id,display_name) VALUES(%s,%s) "
                    "ON CONFLICT DO NOTHING"
                ),
                (user, user),
            )
            conn.execute(
                "INSERT INTO authn.memberships VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (tenant, user, role),
            )
    return {user: issue_session(admin_dsn, user) for user in users}


def bootstrap(
    database="ecommerce_agent",
    runtime_dir=Path("var/runtime"),
    data_dir=Path("var/data/m5"),
    admin_server_dsn=None,
    config=None,
):
    admin_server_dsn = admin_server_dsn or start_database()
    admin = create_database(admin_server_dsn, database)
    migrate(admin)
    config = config or load_config()
    for tenant in ("store-a", "store-b"):
        initial = initialize_store(data_dir, Scenario(scenario_id=f"dev-{tenant}"), config)
        initial = replace(
            initial, snapshot=initial.snapshot.model_copy(update={"tenant_id": tenant})
        )
        engine = Simulator(initial, config)
        visible = engine.observe()
        seed_store(admin, replace(initial, snapshot=visible), config, checkpoint(engine))
    credentials = provision_runtime_roles(
        admin, ["store-a", "store-b"], runtime_dir / "services.json"
    )
    tokens = seed_identities(admin)
    private_json(runtime_dir / "admin.json", {"dsn": admin, "database": database})
    private_json(runtime_dir / "sessions.json", tokens)
    for label, dsn in credentials.items():
        private_json(runtime_dir / (label + ".json"), {"dsn": dsn})
    env = runtime_dir / "api.env"
    env.write_text("API_DATABASE_URL=" + json.dumps(credentials["api"]) + "\n")
    env.chmod(0o600)
    return {
        "admin": admin,
        "services": credentials,
        "sessions": tokens,
        "database": database,
        "runtime_dir": str(runtime_dir.resolve()),
    }
