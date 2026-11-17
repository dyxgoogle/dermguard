"""Local FastMCP server for deterministic cosmetic ingredient safety evaluation.

Exposes two read-only Model Context Protocol (MCP) tools over stdio:
1. `analyze_routine_safety`: Evaluates a multi-product routine and user
   sensitivities for pairwise active conflicts, cumulative multi-product
   stacking, high-dose cautions, photosensitivity, and pregnancy risks.
2. `get_ingredient_caution_profile`: Returns the toxicology and concentration
   limits for a specific cosmetic active ingredient.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover - mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]

from dermguard.data.seed_ingredients import DEFAULT_RULES_PATH, generate_ingredient_rules
from dermguard.observability.structured_logger import log_agent_event
from dermguard.observability.tracing import configure_tracing
from dermguard.tools.schemas import (
    CautionFlag,
    CumulativeStackingWarning,
    PairwiseConflictFinding,
    RoutineSafetyAnalysisOutput,
)

mcp = FastMCP("dermguard_ingredient_safety_mcp")
_TRACER = configure_tracing("dermguard-mcp-server")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _load_rules(rules_path: Path | None = None) -> dict[str, Any]:
    """Loads `ingredient_rules.json`, seeding it automatically if missing."""
    target = rules_path or DEFAULT_RULES_PATH
    if not target.exists():
        return generate_ingredient_rules(output_path=target)
    return json.loads(target.read_text(encoding="utf-8"))


def _normalize_text(raw: str) -> str:
    """Lowercases and standardizes punctuation for alias matching."""
    cleaned = raw.lower().strip()
    cleaned = _PCT_RE.sub("", cleaned)
    cleaned = re.sub(r"[^a-z0-9\s_-]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_percentage(raw: str) -> float | None:
    """Extracts explicit numeric percentage (e.g., 'Glycolic Acid 7%' -> 7.0)."""
    match = _PCT_RE.search(raw)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _match_canonical_active(
    raw_ingredient: str, ingredients_db: dict[str, dict[str, Any]]
) -> list[str]:
    """Maps a raw INCI token or phrase to canonical active keys in the dataset."""
    norm = _normalize_text(raw_ingredient)
    underscore_norm = norm.replace(" ", "_")
    if not norm:
        return []

    if underscore_norm in ingredients_db:
        return [underscore_norm]

    matched: list[str] = []
    for canonical_key, profile in ingredients_db.items():
        for alias in profile.get("aliases", []):
            alias_norm = alias.lower().strip()
            if alias_norm == norm or re.search(rf"\b{re.escape(alias_norm)}\b", norm):
                matched.append(canonical_key)
                break
    # Prefer more specific derivatives (e.g., sodium_ascorbyl_phosphate over generic vitamin c)
    if "sodium_ascorbyl_phosphate" in matched and "vitamin_c_l_ascorbic_acid" in matched:
        matched.remove("vitamin_c_l_ascorbic_acid")
    if "alpha_arbutin" in matched and "hydroquinone" in matched:
        matched.remove("hydroquinone")
    return matched


@mcp.tool()
def analyze_routine_safety(
    routine_products: dict[str, list[str]],
    user_sensitivities: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministically evaluates a multi-product skincare routine for chemical conflicts, cumulative stacking, and dosage cautions.

    Args:
        routine_products: Mapping of product names to their list of INCI or active
            ingredient strings (e.g., `{"Toner": ["Glycolic Acid 7%"], "Serum": ["Retinol"]}`).
        user_sensitivities: Optional list of user sensitivity tags such as
            `["sensitive_skin", "rosacea", "pregnant", "fragrance_allergy", "eczema"]`.

    Returns:
        Dictionary conforming to `RoutineSafetyAnalysisOutput` with pairwise
        conflicts, cumulative stacking warnings, caution flags, SPF requirement,
        and overall risk rating, or guided recovery instructions on invalid input.
    """
    with _TRACER.start_as_current_span("mcp.analyze_routine_safety") as span:
        log_agent_event(
            event_phase="INTENT",
            action_name="analyze_routine_safety",
            details={
                "product_count": len(routine_products) if isinstance(routine_products, dict) else -1,
                "sensitivities_count": len(user_sensitivities or []),
            },
        )

        if not isinstance(routine_products, dict) or not routine_products:
            error_payload = {
                "status": "error",
                "overall_risk_level": "LOW",
                "evaluated_product_count": 0,
                "resolved_actives_by_product": {},
                "pairwise_conflicts": [],
                "cumulative_stacking_warnings": [],
                "caution_flags": [],
                "requires_daily_spf": False,
                "summary": "No routine products were provided for safety analysis.",
                "recovery_instructions": (
                    "Pass a non-empty `routine_products` dictionary mapping each product name "
                    "to a list of ingredient strings. First call `manage_routine_state(action='list')` "
                    "or resolve product INCI ingredients with Google Search."
                ),
            }
            log_agent_event(
                event_phase="OUTCOME",
                action_name="analyze_routine_safety",
                details={"status": "error", "reason": "empty_routine_products"},
            )
            return error_payload

        rules = _load_rules()
        ingredients_db: dict[str, dict[str, Any]] = rules["ingredients"]
        conflict_rules: list[dict[str, Any]] = rules["pairwise_conflicts"]

        normalized_sensitivities = {
            _normalize_text(s).replace(" ", "_")
            for s in (user_sensitivities or [])
            if isinstance(s, str) and s.strip()
        }
        # Expand composite phrases (e.g. "sensitive_skin" or "pregnant_trimester_2")
        expanded_sensitivities: set[str] = set(normalized_sensitivities)
        for item in normalized_sensitivities:
            for known in (
                "sensitive_skin",
                "rosacea",
                "pregnant",
                "eczema",
                "fragrance_allergy",
                "retinoid_intolerance",
            ):
                if known in item or (known == "sensitive_skin" and "sensitive" in item):
                    expanded_sensitivities.add(known)

        resolved_by_product: dict[str, list[str]] = {}
        active_to_products: dict[str, list[str]] = {}
        caution_flags: list[CautionFlag] = []
        requires_spf = False

        for product_name, raw_ing_list in routine_products.items():
            if not isinstance(raw_ing_list, list):
                continue
            product_actives: list[str] = []
            # Also check percentage mentioned in the product name itself (e.g., "2% BHA", "7% Toning")
            product_name_pct = _extract_percentage(product_name)

            for raw_ing in raw_ing_list:
                if not isinstance(raw_ing, str):
                    continue
                detected_pct = _extract_percentage(raw_ing)
                if detected_pct is None and product_name_pct is not None:
                    detected_pct = product_name_pct

                matched_keys = _match_canonical_active(raw_ing, ingredients_db)
                for key in matched_keys:
                    if key not in product_actives:
                        product_actives.append(key)
                    if product_name not in active_to_products.setdefault(key, []):
                        active_to_products[key].append(product_name)

                    profile = ingredients_db[key]
                    max_pct = float(profile["safe_max_concentration_pct"])
                    caution_pct = float(profile["caution_threshold_pct"])

                    if profile.get("photosensitivity"):
                        requires_spf = True
                        caution_flags.append(
                            CautionFlag(
                                caution_type="PHOTOSENSITIVITY_SPF_REQUIRED",
                                ingredient=key,
                                display_name=profile["display_name"],
                                product_name=product_name,
                                detected_concentration_pct=detected_pct,
                                safe_max_concentration_pct=max_pct,
                                details=f"{profile['display_name']} increases UV sun sensitivity.",
                                recommended_action="Apply broad-spectrum SPF 30+ every morning while using this product.",
                            )
                        )

                    if detected_pct is not None and detected_pct >= caution_pct:
                        caution_flags.append(
                            CautionFlag(
                                caution_type="HIGH_DOSE_CAUTION",
                                ingredient=key,
                                display_name=profile["display_name"],
                                product_name=product_name,
                                detected_concentration_pct=detected_pct,
                                safe_max_concentration_pct=max_pct,
                                details=(
                                    f"Detected {detected_pct}% {profile['display_name']} meets/exceeds "
                                    f"caution threshold ({caution_pct}%; max cosmetic guideline {max_pct}%). "
                                    f"{profile['high_dose_adverse_effects']}"
                                ),
                                recommended_action=profile["usage_guidance"],
                            )
                        )

                    if "pregnant" in expanded_sensitivities and profile.get("pregnancy_contraindicated"):
                        caution_flags.append(
                            CautionFlag(
                                caution_type="PREGNANCY_CONTRAINDICATION",
                                ingredient=key,
                                display_name=profile["display_name"],
                                product_name=product_name,
                                detected_concentration_pct=detected_pct,
                                safe_max_concentration_pct=max_pct,
                                details=f"{profile['display_name']} is contraindicated or advised against during pregnancy.",
                                recommended_action="Discontinue cosmetic use during pregnancy and consult an obstetrician/dermatologist for pregnancy-compatible alternatives (e.g., azelaic acid, ceramides).",
                            )
                        )

                    overlap_sens = expanded_sensitivities.intersection(
                        set(profile.get("contraindicated_sensitivities", [])) - {"pregnant"}
                    )
                    if overlap_sens:
                        sens_str = ", ".join(sorted(overlap_sens))
                        caution_flags.append(
                            CautionFlag(
                                caution_type="SENSITIVITY_CONTRAINDICATION",
                                ingredient=key,
                                display_name=profile["display_name"],
                                product_name=product_name,
                                detected_concentration_pct=detected_pct,
                                safe_max_concentration_pct=max_pct,
                                details=(
                                    f"{profile['display_name']} may aggravate user sensitivity profile "
                                    f"({sens_str}). {profile['high_dose_adverse_effects']}"
                                ),
                                recommended_action=profile["usage_guidance"],
                            )
                        )

            resolved_by_product[product_name] = product_actives

        # Cumulative multi-product active stacking checks
        stacking_warnings: list[CumulativeStackingWarning] = []
        for key, prod_list in sorted(active_to_products.items()):
            profile = ingredients_db[key]
            if len(prod_list) >= 2 and profile.get("stacking_caution", False):
                stacking_warnings.append(
                    CumulativeStackingWarning(
                        ingredient=key,
                        display_name=profile["display_name"],
                        product_count=len(prod_list),
                        products=prod_list,
                        guidance=(
                            f"{profile['display_name']} appears across {len(prod_list)} products "
                            f"({', '.join(prod_list)}). Cumulative active stacking increases irritation "
                            f"and barrier disruption risk. {profile['usage_guidance']}"
                        ),
                    )
                )

        # Pairwise conflict matrix evaluation across all resolved actives
        present_actives = set(active_to_products.keys())
        pairwise_findings: list[PairwiseConflictFinding] = []
        for rule in conflict_rules:
            ing_a = rule["ingredient_a"]
            ing_b = rule["ingredient_b"]
            if ing_a in present_actives and ing_b in present_actives:
                involved = sorted(set(active_to_products[ing_a] + active_to_products[ing_b]))
                pairwise_findings.append(
                    PairwiseConflictFinding(
                        ingredient_a=ing_a,
                        ingredient_b=ing_b,
                        products_involved=involved,
                        severity=rule["severity"],
                        mechanism=rule["mechanism"],
                        resolution=rule["resolution"],
                    )
                )

        # Deduplicate caution flags per (caution_type, ingredient, product_name)
        seen_cautions: set[tuple[str, str, str]] = set()
        unique_cautions: list[CautionFlag] = []
        for flag in caution_flags:
            dedup_key = (flag.caution_type, flag.ingredient, flag.product_name)
            if dedup_key not in seen_cautions:
                seen_cautions.add(dedup_key)
                unique_cautions.append(flag)

        # Compute overall risk level
        overall_risk: str = "LOW"
        if any(c.severity == "CRITICAL" for c in pairwise_findings) or any(
            f.caution_type == "PREGNANCY_CONTRAINDICATION" for f in unique_cautions
        ):
            overall_risk = "CRITICAL"
        elif (
            any(c.severity == "HIGH" for c in pairwise_findings)
            or stacking_warnings
            or any(
                f.caution_type in {"HIGH_DOSE_CAUTION", "SENSITIVITY_CONTRAINDICATION"}
                for f in unique_cautions
            )
        ):
            overall_risk = "HIGH"
        elif pairwise_findings or unique_cautions:
            overall_risk = "MODERATE"

        output = RoutineSafetyAnalysisOutput(
            status="success",
            overall_risk_level=overall_risk,  # type: ignore[arg-type]
            evaluated_product_count=len(routine_products),
            resolved_actives_by_product=resolved_by_product,
            pairwise_conflicts=pairwise_findings,
            cumulative_stacking_warnings=stacking_warnings,
            caution_flags=unique_cautions,
            requires_daily_spf=requires_spf,
            summary=(
                f"Evaluated {len(routine_products)} product(s): "
                f"{len(pairwise_findings)} pairwise conflict(s), "
                f"{len(stacking_warnings)} cumulative stacking warning(s), and "
                f"{len(unique_cautions)} caution flag(s). Overall risk: {overall_risk}."
            ),
        )
        span.set_attribute("dermguard.risk_level", overall_risk)
        span.set_attribute("dermguard.conflicts", len(pairwise_findings))
        log_agent_event(
            event_phase="OUTCOME",
            action_name="analyze_routine_safety",
            details={
                "status": "success",
                "overall_risk_level": overall_risk,
                "pairwise_conflicts": len(pairwise_findings),
                "stacking_warnings": len(stacking_warnings),
            },
        )
        return output.model_dump()


@mcp.tool()
def get_ingredient_caution_profile(ingredient_name: str) -> dict[str, Any]:
    """Retrieves the cosmetic toxicology profile, safe concentration cap, and caution guidance for a single active ingredient.

    Args:
        ingredient_name: Common or INCI name of the skincare ingredient (e.g.,
            `'glycolic_acid'`, `'Retinol'`, `'Salicylic Acid'`, `'Benzoyl Peroxide'`).

    Returns:
        Dictionary containing chemical class, max concentration %, photosensitivity,
        pregnancy contraindication, and adverse effects, or guided recovery
        instructions if the ingredient is not in the seeded active database.
    """
    with _TRACER.start_as_current_span("mcp.get_ingredient_caution_profile"):
        log_agent_event(
            event_phase="INTENT",
            action_name="get_ingredient_caution_profile",
            details={"ingredient_name": ingredient_name},
        )
        if not isinstance(ingredient_name, str) or not ingredient_name.strip():
            return {
                "status": "error",
                "message": "Ingredient name cannot be empty.",
                "recovery_instructions": "Provide a non-empty INCI or active ingredient name such as 'retinol' or 'glycolic_acid'.",
            }

        rules = _load_rules()
        ingredients_db: dict[str, dict[str, Any]] = rules["ingredients"]
        matches = _match_canonical_active(ingredient_name, ingredients_db)

        if not matches:
            available = ", ".join(sorted(ingredients_db.keys())[:15])
            log_agent_event(
                event_phase="OUTCOME",
                action_name="get_ingredient_caution_profile",
                details={"status": "not_found", "query": ingredient_name},
            )
            return {
                "status": "not_found",
                "queried_ingredient": ingredient_name,
                "message": f"'{ingredient_name}' did not match a seeded high-activity cosmetic active.",
                "recovery_instructions": (
                    f"If '{ingredient_name}' is a base excipient (e.g., water, glycerin), no active "
                    f"conflict rule applies. To query a known active, pass a standard INCI active name "
                    f"such as: {available}."
                ),
            }

        canonical = matches[0]
        profile = ingredients_db[canonical]
        result = {
            "status": "success",
            "canonical_key": canonical,
            **profile,
        }
        log_agent_event(
            event_phase="OUTCOME",
            action_name="get_ingredient_caution_profile",
            details={"status": "success", "canonical_key": canonical},
        )
        return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
