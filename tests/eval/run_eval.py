"""Automated golden-dataset regression harness for DermGuard.

Validates the golden evaluation dataset (`dermguard_golden.evalset.json`) both
statically (schema + deterministic toxicology assertions) and, when
`RUN_LIVE_ADK_EVAL=true` with valid Gemini credentials, dynamically via
`google.adk.evaluation.agent_evaluator.AgentEvaluator`.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dermguard.mcp_server.ingredient_safety_mcp import analyze_routine_safety

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN_EVALSET_PATH = EVAL_DIR / "evalsets" / "dermguard_golden.evalset.json"
EVAL_CONFIG_PATH = EVAL_DIR / "eval_config.json"


def verify_golden_dataset_contracts() -> dict[str, int | str]:
    """Statically validates golden evalset structure and deterministic rule parity."""
    evalset = json.loads(GOLDEN_EVALSET_PATH.read_text(encoding="utf-8"))
    config = json.loads(EVAL_CONFIG_PATH.read_text(encoding="utf-8"))

    cases = evalset.get("eval_cases", [])
    assert len(cases) >= 5, "Golden dataset must include all 5 core evaluation scenarios."
    assert "criteria" in config, "eval_config.json must define evaluation criteria."

    # Verify deterministic toxicology engine matches golden case expectations
    conflict_check = analyze_routine_safety(
        routine_products={
            "The Ordinary Glycolic Acid 7% Toning Solution": ["Glycolic Acid 7%"],
            "CeraVe Resurfacing Retinol Serum": ["Retinol", "Niacinamide", "Ceramides"],
        },
        user_sensitivities=["sensitive_skin"],
    )
    assert len(conflict_check["pairwise_conflicts"]) >= 1

    pregnancy_check = analyze_routine_safety(
        routine_products={"Retinol Serum": ["Retinol 0.5%"]},
        user_sensitivities=["pregnant"],
    )
    assert pregnancy_check["overall_risk_level"] == "CRITICAL"

    return {
        "eval_set_id": evalset["eval_set_id"],
        "verified_cases": len(cases),
        "status": "passed",
    }


async def run_optional_live_eval() -> None:
    """Runs live LLM-as-a-judge ADK evaluation when `RUN_LIVE_ADK_EVAL=true`."""
    if os.getenv("RUN_LIVE_ADK_EVAL", "false").lower() != "true":
        return
    from google.adk.evaluation.agent_evaluator import AgentEvaluator

    await AgentEvaluator.evaluate(
        agent_module="dermguard",
        eval_dataset_file_path_or_dir=str(GOLDEN_EVALSET_PATH),
        num_runs=1,
    )


if __name__ == "__main__":
    summary = verify_golden_dataset_contracts()
    asyncio.run(run_optional_live_eval())
    raise SystemExit(0 if summary["status"] == "passed" else 1)
