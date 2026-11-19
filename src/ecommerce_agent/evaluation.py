"""Development-only matched deterministic controls; not the locked final experiment."""

import json
from pathlib import Path

from ecommerce_agent.config import load_config
from ecommerce_agent.data import checksum
from ecommerce_agent.simulation import Scenario, run_episode


def compare_controls(data_dir: Path, output: Path, *, seed=20261119, config=None):
    data_dir, output = data_dir.resolve(), output.resolve()
    if output == data_dir or output.is_relative_to(data_dir) or data_dir.is_relative_to(output):
        raise ValueError("control outputs must be separate from curated inputs")
    settings = config or load_config()
    runs = []
    for family in ("normal", "demand_surge", "supplier_delay"):
        scenario = Scenario(scenario_id=f"dev-{family}-001", family=family, seed=seed)
        snapshots = []
        for policy in ("rules", "fixed"):
            target = output / family / policy
            result = run_episode(data_dir, target, scenario, policy, settings)
            decisions = json.loads((target / "public/control_decisions.json").read_text())
            snapshots.append(checksum(target / "public/initial_snapshot.json"))
            runs.append(
                {
                    "scenario": family,
                    "policy": policy,
                    "metrics": result["metrics"],
                    "review_days": [d["day"] for d in decisions],
                    "tool_calls": sum(len(d["tool_traces"]) for d in decisions),
                    "proposals": sum(d["outcome"] == "proposal" for d in decisions),
                    "no_ops": sum(d["outcome"] == "justified_no_op" for d in decisions),
                    "manifest_sha256": checksum(target / "manifest.json"),
                }
            )
        if len(set(snapshots)) != 1:
            raise RuntimeError("matched policies have different initial state")
    report = {
        "usage": "development_only",
        "scenario_count": 3,
        "episodes": 6,
        "policies": ["rules", "fixed"],
        "model_calls": 0,
        "seed": seed,
        "pairing": "identical initial state and keyed exogenous draws",
        "approval_mode": "privileged numerical development harness; not the approval demo",
        "data_manifest_sha256": checksum(data_dir / "manifest.json"),
        "runs": runs,
        "limitations": [
            "One development instance per family, no uncertainty/significance claims.",
            "These are simulator results, not real merchant gains or final-test results.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return {
        "status": "completed",
        "episodes": 6,
        "scenarios": 3,
        "model_calls": 0,
        "output": str(output),
        "report": str(output / "report.json"),
        "usage": "development_only",
    }
