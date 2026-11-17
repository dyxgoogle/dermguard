# DESIGN_SPEC.md — DermGuard: Skincare Ingredient & Conflict Analyst

## Overview
**DermGuard** is a conversational cosmetic toxicology and skincare routine safety agent built on the **Google Agent Development Kit (ADK)**. Consumers routinely layer exfoliating acids, retinoids, antioxidants, and cleansers without checking exact INCI ingredient lists or cross-product active interactions. DermGuard discovers official product INCI lists via live Google Search grounding, tracks the user's evolving multi-product routine in persistent session state, remembers personal skin sensitivities across sessions, and deterministically evaluates pairwise chemical conflicts, cumulative active stacking, and high-concentration cautions via a Model Context Protocol (`FastMCP`) server.

## Example Use Cases
1. **Single-Product Lookup & Sensitivity Check**
   - *Input:* `"I have rosacea and sensitive skin. Is The Ordinary Glycolic Acid 7% Toning Solution safe for me?"`
   - *Output:* Persists `["rosacea", "sensitive_skin"]`, looks up the INCI list via `GoogleSearchTool`, records the product in `active_routine_products`, runs `analyze_routine_safety`, and warns about 7% free glycolic acid irritation, rosacea contraindication, and mandatory daily broad-spectrum SPF.
2. **Incremental Multi-Turn Product Addition**
   - *Input (Turn 2):* `"What if I also add CeraVe Resurfacing Retinol Serum at night?"`
   - *Output:* Resolves INCI (`Retinol`, `Niacinamide`, `Ceramides`), appends to `active_routine_products`, re-evaluates the combined routine, flags a `PAIRWISE_CONFLICT` (`glycolic_acid` + `retinol`), and proposes alternating PM nights.
3. **Cumulative Active Stacking Detection**
   - *Input:* `"Can I use CeraVe Renewing SA Cleanser and Paula's Choice 2% BHA Liquid Exfoliant together?"`
   - *Output:* Detects `salicylic_acid` across multiple products and raises a `CUMULATIVE_STACKING_CAUTION` warning against barrier over-exfoliation.
4. **Medical Guardrail Refusal**
   - *Input:* `"I have a bleeding scaly rash on my cheek, should I apply hydrocortisone or topical clindamycin?"`
   - *Output:* Deterministic policy plugin intercepts medical diagnosis / prescription steroid/antibiotic queries and directs the user to a board-certified dermatologist.

## Tools Required
| Tool | Type | Purpose & Schema | Auth |
| :--- | :--- | :--- | :--- |
| `GoogleSearchTool(bypass_multi_tools_limit=True)` | ADK Built-in | Grounded web search for brand product INCI ingredient lists alongside custom and MCP tools. | Gemini API / Vertex AI |
| `manage_routine_state` | ADK `FunctionTool` | Adds, removes, lists, or clears `tool_context.state["active_routine_products"]` with strict Pydantic validation and guided error recovery. | Local Session State |
| `manage_user_sensitivities` | ADK `FunctionTool` (Async) | Reads/updates persistent user sensitivity profiles in SQLite + JSON (`data/user_sensitivities.json`) with PII scrubbing and non-blocking async memory sync. | Local Persistent Store |
| `clear_entire_skincare_routine` | ADK `FunctionTool` (HITL) | Clears all routine products only after explicit human-in-the-loop confirmation (`require_confirmation=True`). | User Confirmation |
| `reset_user_sensitivity_memory` | ADK `FunctionTool` (HITL) | Deletes persistent user sensitivity profile only after explicit human-in-the-loop confirmation (`require_confirmation=True`). | User Confirmation |
| `analyze_routine_safety` | MCP Tool (`FastMCP` stdio) | Deterministically checks pairwise active conflicts, cumulative multi-product stacking, high-dose cautions, pregnancy flags, and user sensitivities. | Local stdio |
| `get_ingredient_caution_profile` | MCP Tool (`FastMCP` stdio) | Returns chemical class, max safe %, photosensitivity, pregnancy safety, and high-dose adverse effects for a single ingredient. | Local stdio |
| `am_pm_routine_resolution_pipeline` | ADK `AgentTool` (`SequentialAgent`) | Two-stage specialist pipeline (`biochemical_conflict_verifier_agent` on `gemini-2.5-flash` → `am_pm_schedule_architect_agent` on `gemini-2.5-pro`) for complex multi-active AM/PM routine scheduling. | Gemini API / Vertex AI |

## Constraints & Safety Rules
- **Cosmetic Information Only:** Strictly refuses medical diagnoses, disease treatment plans, or prescription medication dosing (tretinoin Rx guidance, topical steroids, antibiotics, antifungals).
- **Zero PII Persistence:** All incoming prompts, logs, and sensitivity records pass through `PIIRedactor` (regex + optional Google Cloud DLP) before logging or disk persistence.
- **Destructive Action Confirmation:** Wiping an entire routine or deleting saved user sensitivities pauses execution via ADK's `require_confirmation=True` Human-in-the-Loop hook.
- **Zero Hardcoded Secrets:** Credentials are resolved exclusively via Google Cloud Secret Manager or environment variables.

## Success Criteria
- 100% deterministic recall on seeded pairwise chemical conflicts (e.g., `glycolic_acid` + `retinol`, `benzoyl_peroxide` + `tretinoin`, `vitamin_c_l_ascorbic_acid` + `peptides_copper`) and cumulative multi-product active stacking.
- Cross-session sensitivity recall verified across independent session IDs in `DatabaseSessionService` + JSON/SQLite user memory.
- Sliding-window history compaction configured at `compaction_interval=10` and `overlap_size=2` on the ADK `App`.
- Full unit test suite (`pytest`) and golden evaluation dataset (`tests/eval/evalsets/dermguard_golden.evalset.json`) passing cleanly in CI.

## Reference Samples
- `google/adk-samples` (`deep-search`, `safety-plugins`, `memory-bank`)
