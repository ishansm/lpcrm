"""
LP Match Pipeline — Full orchestration.

Runs all stages in sequence:
  1. Fetch LP records from Notion CRM
  2. Extract structured profiles with Claude
  3. Apply hard disqualification filters
  4. Score remaining LPs against GP opportunity
  5. Generate rationales with Claude
  6. Write report to Notion

Each stage saves JSON to output/ for debugging and resume support.
Run with: python3 main.py
"""

import json
import os
import time
import anthropic
from config import GP_PROFILE, NOTION_DATABASE_ID, ANTHROPIC_API_KEY, WEIGHTS


def main():
    start = time.time()
    os.makedirs("output", exist_ok=True)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # ── Stage 1: Fetch from Notion ──────────────────────────────────────
    print("=" * 60)
    print("STAGE 1: Fetching LP records from Notion CRM")
    print("=" * 60)

    from notion_reader import fetch_all_lps
    lps = fetch_all_lps(NOTION_DATABASE_ID)
    print(f"\n  Fetched {len(lps)} LPs.\n")

    # ── Stage 2: Extract ────────────────────────────────────────────────
    print("=" * 60)
    print("STAGE 2: Extracting structured profiles (Claude)")
    print("=" * 60)

    from extract import extract_all
    extract_path = os.path.join("output", "extracted_profiles.json")

    # Resume support: restore previously extracted LPs
    if os.path.exists(extract_path):
        with open(extract_path) as f:
            existing = {
                prev["name"]: prev
                for prev in json.load(f)
                if prev.get("extracted") and "_parse_error" not in prev.get("extracted", {})
            }
        restored = 0
        for lp in lps:
            if lp["name"] in existing:
                lp["extracted"] = existing[lp["name"]]["extracted"]
                restored += 1
        if restored:
            print(f"  Restored {restored} previously extracted LPs.\n")

    extract_all(client, lps, GP_PROFILE)

    with open(extract_path, "w") as f:
        json.dump(lps, f, indent=2, default=str)
    print(f"\n  Saved to {extract_path}\n")

    # ── Stage 3: Filter ─────────────────────────────────────────────────
    print("=" * 60)
    print("STAGE 3: Applying hard disqualification filters")
    print("=" * 60)

    from filter import apply_hard_filters
    passed, rejected_raw = apply_hard_filters(lps, GP_PROFILE)

    # Save in the format score.py and rationale.py expect
    filter_path = os.path.join("output", "filter_results.json")
    rejected_serialized = [
        {
            "name": r["lp"]["name"],
            "gate": r["gate"],
            "reason": r["reason"],
            "negative_flags": r.get("negative_flags", []),
            "lp": r["lp"],
        }
        for r in rejected_raw
    ]
    with open(filter_path, "w") as f:
        json.dump({"passed": passed, "rejected": rejected_serialized}, f, indent=2, default=str)

    print(f"\n  Passed: {len(passed)} | Rejected: {len(rejected_raw)}")
    for r in rejected_raw:
        print(f"    \u2717 {r['lp']['name']:<30s} [{r['gate']}] {r['reason']}")
    print(f"\n  Saved to {filter_path}\n")

    # ── Stage 4: Score ──────────────────────────────────────────────────
    print("=" * 60)
    print("STAGE 4: Scoring LPs against GP opportunity")
    print("=" * 60)

    from score import score_lp, compute_composite

    scored = []
    for lp in passed:
        scores = score_lp(lp, GP_PROFILE)
        composite = compute_composite(scores, WEIGHTS, lp=lp, gp_profile=GP_PROFILE)
        scored.append({
            "name": lp["name"],
            "scores": scores,
            "composite": composite,
            "confidence": lp.get("extracted", {}).get("confidence_level", "unknown"),
            "negative_flags": lp.get("negative_flags", []),
            "info_flags": lp.get("info_flags", []),
            "lp": lp,
        })
    scored.sort(key=lambda x: x["composite"]["match_pct"], reverse=True)

    score_path = os.path.join("output", "scored_results.json")
    with open(score_path, "w") as f:
        json.dump({
            "gp_profile": GP_PROFILE,
            "weights": WEIGHTS,
            "scored": scored,
            "rejected": rejected_serialized,
        }, f, indent=2, default=str)

    print(f"\n  Top 5:")
    for i, s in enumerate(scored[:5]):
        bp = s["lp"].get("extracted", {}).get("investment_pattern", {})
        bp_str = bp.get("buying_profile", "") if isinstance(bp, dict) else ""
        print(f"    {i+1}. {s['name']:<30s} {s['composite']['match_pct']:>5.1f}%  ({s['confidence']})")
        if bp_str:
            print(f"       {bp_str}")
    print(f"\n  Saved to {score_path}\n")

    # ── Stage 5: Rationale ──────────────────────────────────────────────
    print("=" * 60)
    print("STAGE 5: Generating rationales (Claude)")
    print("=" * 60)

    from rationale import generate_rationales
    rationales = generate_rationales(scored_path=score_path)
    print()

    # ── Stage 6: Write to Notion ────────────────────────────────────────
    print("=" * 60)
    print("STAGE 6: Writing report to Notion")
    print("=" * 60)

    from notion_writer import create_report_page
    page_url = create_report_page(
        rationale_path=os.path.join("output", "rationale_results.json"),
        scored_path=score_path,
    )

    # ── Summary ─────────────────────────────────────────────────────────
    elapsed = time.time() - start
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    print()
    print("=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  LPs fetched:    {len(lps)}")
    extracted_count = sum(
        1 for lp in lps
        if lp.get("extracted") and "_parse_error" not in lp.get("extracted", {})
    )
    print(f"  LPs extracted:  {extracted_count}")
    print(f"  Passed filters: {len(passed)}")
    print(f"  Rejected:       {len(rejected_raw)}")
    print(f"  Scored:         {len(scored)}")
    print(f"  Time:           {minutes}m {seconds}s")
    print(f"\n  Notion report:  {page_url}")
    print(f"\n  Output files:")
    for p in ("extracted_profiles.json", "filter_results.json",
              "scored_results.json", "rationale_results.json"):
        print(f"    output/{p}")


if __name__ == "__main__":
    main()
