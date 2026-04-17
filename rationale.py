"""
Stage 6: Rationale generation.

Takes scored results and asks Claude to write human-readable rationales
for the top-5 ranked LPs and all rejected LPs. Output is structured JSON
suitable for reports, Notion pages, or partner briefings.

Uses MODEL (Sonnet) — this is simpler text generation, not nuanced extraction.
"""

import json
import os
import anthropic
from config import MODEL, ANTHROPIC_API_KEY, GP_PROFILE


def build_rationale_prompt(scored_lps, rejected_lps, gp_profile):
    """Build the rationale generation prompt."""

    # GP context
    gp_lines = []
    for key, label in [
        ("name", "Fund"), ("fund_size", "Fund size"), ("stage", "Stage"),
        ("manager_type", "Manager type"), ("geography", "Geography"),
        ("lp_product_category", "Product category"),
    ]:
        if gp_profile.get(key):
            gp_lines.append(f"- {label}: {gp_profile[key]}")
    if gp_profile.get("sectors"):
        gp_lines.append(f"- Sectors: {', '.join(gp_profile['sectors'])}")
    if gp_profile.get("broader_geo"):
        gp_lines.append(f"- Broader geo: {', '.join(gp_profile['broader_geo'])}")
    if gp_profile.get("key_traits"):
        gp_lines.append(f"- Key traits: {'; '.join(gp_profile['key_traits'])}")
    gp_context = "\n".join(gp_lines)

    # Top 5 scored LPs — full detail including enrichment for specific references
    top5 = scored_lps[:5]
    top5_blocks = []
    for i, s in enumerate(top5):
        ext = s["lp"].get("extracted", {})
        pattern = ext.get("investment_pattern", {})
        temporal = ext.get("temporal_signals", {})
        top5_blocks.append(json.dumps({
            "rank": i + 1,
            "name": s["name"],
            "match_pct": s["composite"]["match_pct"],
            "confidence": s["confidence"],
            "scores": s["scores"],
            "composite": s["composite"],
            "negative_flags": s.get("negative_flags", []),
            "info_flags": s.get("info_flags", []),
            "conviction_signals": ext.get("conviction_signals", []),
            "key_quotes": ext.get("key_quotes", []),
            "exclusions": ext.get("exclusions", []),
            "open_questions": ext.get("open_questions", []),
            "pending_actions": ext.get("pending_actions", []),
            "buying_profile": pattern.get("buying_profile", ""),
            "pattern_fit_with_gp": pattern.get("pattern_fit_with_gp", ""),
            "note_taker_observations": ext.get("note_taker_observations", []),
            "timing_readiness": ext.get("timing_readiness", "unknown"),
            "temporal_signals": temporal,
            "framework_type": ext.get("framework_type", "unknown"),
            "lp_type": ext.get("lp_type", "unknown"),
            "geography_interests": ext.get("geography_interests", []),
            "past_investments": ext.get("past_investments", []),
            "contextual_enrichment": ext.get("contextual_enrichment", []),
            "competitive_positioning": ext.get("competitive_positioning", []),
            "conflicting_signals": ext.get("conflicting_signals", ""),
            "organizational_dynamics": ext.get("organizational_dynamics", {}),
            "decision_process": ext.get("decision_process", {}),
            "structured_status": s["lp"].get("structured", {}).get("status", ""),
        }, indent=2))

    # Rejected LPs — include enough data for actionable explanations
    rejected_blocks = []
    for r in rejected_lps:
        ext = r["lp"].get("extracted", {})
        pattern = ext.get("investment_pattern", {})
        rejected_blocks.append(json.dumps({
            "name": r["name"],
            "gate": r.get("gate", ""),
            "reason": r.get("reason", ""),
            "negative_flags": r.get("negative_flags", []),
            "buying_profile": pattern.get("buying_profile", "") if isinstance(pattern, dict) else "",
            "pattern_fit_with_gp": pattern.get("pattern_fit_with_gp", "") if isinstance(pattern, dict) else "",
            "past_investments": ext.get("past_investments", []),
            "geography_interests": ext.get("geography_interests", []),
            "exclusions": ext.get("exclusions", []),
            "conviction_signals": ext.get("conviction_signals", []),
            "key_quotes": ext.get("key_quotes", []),
            "competitive_positioning": ext.get("competitive_positioning", []),
            "framework_type": ext.get("framework_type", "unknown"),
            "note_taker_observations": ext.get("note_taker_observations", []),
            "open_questions": ext.get("open_questions", []),
            "check_size_range": ext.get("check_size_range", "unknown"),
            "lp_type": ext.get("lp_type", "unknown"),
        }, indent=2))

    return f"""You are writing an internal LP intelligence brief for a venture capital fundraising team. This brief will be read by a partner 10 minutes before making outreach calls.

## GP opportunity
{gp_context}

## Task
Generate structured rationales for the top 5 ranked LPs and all rejected LPs below.

## Top 5 scored LPs

{chr(10).join(f'### LP #{i+1}{chr(10)}{block}' for i, block in enumerate(top5_blocks))}

## Rejected LPs

{chr(10).join(f'### Rejected: {rejected_lps[i]["name"]}{chr(10)}{block}' for i, block in enumerate(rejected_blocks))}

## How to write each rationale

Each rationale is a 10-minute outreach prep brief. For every ranked LP, cover three things:

1. **Strongest reason to approach** — the single most compelling reason this LP fits this GP. Be specific: name funds they've backed, geographies they've deployed in, or quotes that reveal alignment. "They have India exposure" is weak. "Big position in Reliance + backed Kaszek in Brazil — proven EM appetite at scale" is strong. Pull from contextual_enrichment, past_investments, and competitive_positioning.

2. **Main objection to prepare for** — the hesitation or risk the partner should expect. Pull from conflicting_signals, risk_flags, negative_flags, or open_questions. Every LP has at least one concern — name it so the partner isn't blindsided.

3. **Entry point** — name the specific person, warm referral, CRM status, or relationship channel that gets the conversation started. Use the LP's structured_status, organizational_dynamics.decision_maker, and any relationship references from contextual_enrichment.

### Use extracted data, not generic language
Every rationale MUST reference at least one specific fund name, person name, or verbatim quote from the LP's data. Use contextual_enrichment to find specific fund references (with their enriched context). Use competitive_positioning to frame the approach.

### Approach framing from competitive_positioning
- If the LP has positive sentiment toward a competitor, note it as a framing angle. E.g., "Loved Nomads' model — frame GP intro as a similar approach in a new geography."
- If they have negative sentiment toward a competitor, note what to avoid. E.g., "Dislikes Pattern Ventures' fund-access-platform style — emphasize direct relationship, not platform."

### Specific next steps
Don't write "follow up." Write the actual next step from pending_actions with timing. "Reconnect end of Jan when bandwidth frees up" or "They'll chat internally — follow up in 2 weeks for decision."

## How to write each rejection

For each rejected LP:
1. **Explain with evidence** — reference specific data (quotes, fund names, stated exclusions) that triggered the filter.
2. **What would need to change** — one sentence on what shift would make this LP approachable. E.g., "If they expand geographic mandate beyond US" or "If they launch a small-fund program below their $200M floor." Make the rejected list actionable.
3. **Near-miss flag** — true when exactly ONE hard disqualifier prevents an otherwise strong match. Explain what makes them painful to exclude. False when there are fundamental mismatches (wrong framework, multiple structural issues, no real alignment).

## Output schema

Return a JSON object with exactly this structure:

{{
  "top_5": [
    {{
      "rank": 1,
      "name": "LP name",
      "match_pct": 85.0,
      "confidence": "high",
      "rationale": "3-4 sentences covering: strongest reason to approach, main objection to prepare for, and entry point. Must reference specific fund names, people, or quotes.",
      "key_citations": ["Up to 5 exact verbatim quotes from the LP's call notes — see instructions below."],
      "risk_flags": ["specific risks — each flag should name the concern concretely"],
      "timing": "ready_now / delayed / unknown",
      "per_criterion": {{"intellectual_alignment": 10, "active_intent": 8, "demonstrated_behavior": 9, "sector_alignment": 7, "geography_match": 8, "check_size_feasibility": 7, "relationship_proximity": 9}},
      "open_questions": ["conversation starters from the LP's open_questions field"],
      "pending_actions": ["specific next steps with timing from the LP's pending_actions field"],
      "buying_profile": "from the LP's buying_profile field verbatim"
    }}
  ],
  "rejected": [
    {{
      "name": "LP name",
      "reason": "the hard filter gate and reason",
      "explanation": "2-3 sentences: what triggered the filter (with evidence), and what would need to change for this LP to become approachable.",
      "near_miss": true
    }}
  ]
}}

## Rules
- "rationale" must be 3-4 sentences with at least one specific fund name, person, or quote. Written in third person ("They...").
- "key_citations": Pull up to 5 verbatim quotes directly from the LP's call notes (from key_quotes and conviction_signals) that reveal intent, preferences, objections, or relationship signals. These must be exact text from the notes — never paraphrase or fabricate. Prioritize quotes that show: (1) strongest conviction signal, (2) main objection or hesitation, (3) relationship/trust indicators, (4) investment behavior evidence, (5) timing or next steps. If the LP has fewer than 5 meaningful quotes in their notes, include as many as exist — don't pad with weak quotes.
- "timing": ready_now → "ready_now", 3-6_months or 6-12_months → "delayed", unknown → "unknown".
- "open_questions" and "pending_actions" come directly from the LP data — do not invent.
- "buying_profile" comes directly from the LP's buying_profile field — do not rewrite.
- For rejected LPs, "explanation" MUST include what would need to change to make them approachable.

CRITICAL CITATION RULE: Every citation in key_citations must be an EXACT verbatim quote from the LP's call notes — copy-paste accuracy. Do NOT paraphrase, combine multiple phrases into one quote, add words that aren't in the source, or rephrase in any way. If the notes say "have not talked to any of the middle category like Prime, 3one4, Blume types" then the citation must be exactly that — not "Hasn't talked to middle category funds like Prime, 3one4, Blume - exactly our category." The "exactly our category" part is your interpretation, not a quote. If the notes say "familiar with Nova, etc. and they really like thematic bent and discovery angle" then do NOT split and recombine as "familiar with Nova and likes thematic bent and discovery angle." Quote the exact text as it appears. If you need to truncate a long quote, use "..." to indicate omission but never change the words.

## Output format
Return ONLY valid JSON. No markdown, no explanation, no wrapping."""


def generate_rationales(scored_path="output/scored_results.json"):
    """Load scored results, generate rationales, save to output/rationale_results.json."""

    print(f"Loading scored results from {scored_path}...\n")
    with open(scored_path) as f:
        data = json.load(f)

    scored = data["scored"]
    rejected = data["rejected"]
    gp_profile = data.get("gp_profile", GP_PROFILE)

    print(f"Top 5 of {len(scored)} scored LPs:")
    for i, s in enumerate(scored[:5]):
        print(f"  {i+1}. {s['name']:30s} {s['composite']['match_pct']:>5.1f}%  ({s['confidence']})")
    print(f"\nRejected: {len(rejected)}")
    for r in rejected:
        print(f"  ✗ {r['name']:30s} [{r.get('gate', '?')}]")

    prompt = build_rationale_prompt(scored, rejected, gp_profile)

    print(f"\nGenerating rationales with {MODEL}...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=6000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text.strip()

    # Handle markdown wrapping
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines)

    try:
        rationales = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"\n  PARSE ERROR: {e}")
        print(f"  Raw (first 500 chars): {raw_text[:500]}")
        rationales = {"_parse_error": str(e), "_raw": raw_text[:2000]}

    # Save
    output_path = os.path.join("output", "rationale_results.json")
    os.makedirs("output", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(rationales, f, indent=2, default=str)
    print(f"\nSaved rationale results to {output_path}")

    # Print summary
    if "top_5" in rationales:
        print(f"\n{'='*70}")
        print(f"TOP 5 LP RATIONALES")
        print(f"{'='*70}")
        for lp in rationales["top_5"]:
            print(f"\n  #{lp['rank']} {lp['name']} — {lp['match_pct']}% ({lp['confidence']})")
            print(f"     {lp['buying_profile']}")
            print(f"     {lp['rationale']}")
            # Support both key_citations (new) and key_citation (old)
            citations = lp.get("key_citations", [])
            if not citations and lp.get("key_citation"):
                citations = [lp["key_citation"]]
            if isinstance(citations, str):
                citations = [citations]
            for cit in citations:
                print(f"     \U0001f4ce \"{cit}\"")
            if lp.get("risk_flags"):
                for rf in lp["risk_flags"]:
                    print(f"     \u26a0 {rf}")
            if lp.get("pending_actions"):
                print(f"     \U0001f4cb Next: {'; '.join(lp['pending_actions'])}")
            print(f"     Timing: {lp.get('timing', '?')}")

        print(f"\n{'='*70}")
        print(f"REJECTED")
        print(f"{'='*70}")
        for r in rationales.get("rejected", []):
            nm = " (NEAR MISS)" if r.get("near_miss") else ""
            print(f"\n  \u2717 {r['name']}{nm}")
            print(f"    {r['explanation']}")

    return rationales


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------
# Usage:
#   python3 rationale.py
# Reads:  output/scored_results.json
# Writes: output/rationale_results.json

if __name__ == "__main__":
    generate_rationales()
