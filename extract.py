"""
Stage 2: Claude extraction pass.

Single extraction call per LP at temperature 0. GP profile is provided as
context so extraction is opportunity-aware.

Key design choices:
  - Distinguishes explicitly stated vs. inferred vs. absent
  - Detects wrong asset class frameworks (PE/credit mindset)
  - Flags direct quotes for citation in the final report
  - Identifies conflicting signals
  - Resume support: re-run to pick up where rate limits interrupted
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
from config import EXTRACTION_MODEL


EXTRACTION_SCHEMA = {
    # --- Core profile fields ---
    "sector_interests": "list of strings — sectors they invest in or care about",
    "geography_interests": "list of strings — geographies they invest in or are interested in",
    "fund_stage_pref": "string — preferred fund stage (seed, Series A, growth, etc.)",
    "check_size_range": "string — typical check size range they write (e.g. '$1M-$5M')",
    "min_fund_size": "string — minimum fund AUM they'll invest in, or 'unknown'",
    "past_investments": "list of strings — known prior fund/direct investments",
    "exclusions": "list of strings — things they explicitly won't do (geos, sectors, types)",
    "lp_type": "string — family office / endowment / institutional / individual / etc.",
    "involvement_style": "string — passive / advisory / hands-on / board-seeking",
    "bandwidth": "string — high / medium / low / unknown — how much capacity they have",
    "framework_type": "string — venture-native / PE-crossover / credit-mindset / traditional-allocator / unknown",
    "risk_tolerance": "string — high / medium / low / unknown",
    "timing_readiness": "string — ready_now / 3-6_months / 6-12_months / unknown",
    "confidence_level": "string — high / medium / low — based on how much data exists",
    "conflicting_signals": "string — any contradictory information found, or empty string",
    "conviction_signals": "list of strings — strongest evidence this LP would commit",
    "key_quotes": "list of strings — direct quotes from call notes worth citing (max 3)",
    # --- New enrichment fields ---
    "contextual_enrichment": "list of objects — enrich every significant reference in the notes. Each object: {reference, type, context, relevance}. See instructions below.",
    "note_taker_observations": "list of strings — internal team judgments from notes that are NOT LP-stated facts (e.g. 'seem quite slow', 'young dude vibing well with us')",
    "temporal_signals": "object — {latest_interaction_date: ISO date or 'unknown', recency: 'recent'/'stale'/'unknown', notes_freshness: string assessment}",
    "pending_actions": "list of strings — next steps, follow-ups, action items mentioned in notes",
    "investment_pattern": "object — synthesized FROM contextual_enrichment fund/asset entries (not independently). Fields: {typical_fund_size: string range, typical_geography: string, typical_stage: string, concentration: 'few deep relationships'/'many shallow'/'unknown', buying_profile: string — one sentence characterizing the LP's investment personality, pattern_fit_with_gp: string — one sentence assessing fit between revealed buying pattern and GP opportunity}",
    "signal_source_quality": "object — {primary_source: 'call_conversation'/'structured_template'/'brief_notes', depth: 'deep'/'moderate'/'shallow', conviction_reliability: 'high'/'medium'/'low'}",
    # --- Decision and organizational fields ---
    "decision_process": "object — {approach: 'thesis-first'/'relationship-driven'/'opportunistic'/'process-driven'/'unknown', vehicle_preference: 'funds only'/'funds+directs+publics'/'vehicle-agnostic'/'unknown', decision_speed: 'fast'/'moderate'/'slow'/'unknown', evidence: string — specific quote or observation}",
    "organizational_dynamics": "object — {decision_maker: string (who decides), internal_alignment: 'aligned'/'friction'/'unknown', team_capacity: 'dedicated venture team'/'part-time'/'solo'/'unknown', evidence: string}",
    "competitive_positioning": "list of objects — each: {competitor: string (name of competing fund/relationship), lp_sentiment: 'positive'/'negative'/'neutral', quote: string}",
    "open_questions": "list of strings — unresolved items worth exploring in next conversation. NOT scoring penalties. E.g., 'Haven't done FoFs — open to discussing', 'No EM exposure but no stated objection'",
}


def build_extraction_prompt(lp, gp_profile):
    """Build the extraction prompt for one LP."""
    schema_desc = json.dumps(EXTRACTION_SCHEMA, indent=2)

    # Build GP context dynamically from whatever fields exist in the profile
    gp_lines = []
    if gp_profile.get("name"):
        gp_lines.append(f"- Fund: {gp_profile['name']}")
    if gp_profile.get("fund_size"):
        gp_lines.append(f"- Fund size: {gp_profile['fund_size']}")
    if gp_profile.get("stage"):
        gp_lines.append(f"- Stage: {gp_profile['stage']}")
    if gp_profile.get("manager_type"):
        gp_lines.append(f"- Manager type: {gp_profile['manager_type']}")
    if gp_profile.get("sectors"):
        gp_lines.append(f"- Sectors: {', '.join(gp_profile['sectors'])}")
    if gp_profile.get("geography"):
        gp_lines.append(f"- Geography: {gp_profile['geography']}")
    if gp_profile.get("broader_geo"):
        gp_lines.append(f"- Broader geo: {', '.join(gp_profile['broader_geo'])}")
    if gp_profile.get("lp_product_category"):
        gp_lines.append(f"- Product category: {gp_profile['lp_product_category']}")
    if gp_profile.get("key_traits"):
        gp_lines.append(f"- Key traits: {'; '.join(gp_profile['key_traits'])}")
    gp_context = "\n".join(gp_lines)

    return f"""You are an LP intelligence analyst for a venture capital fundraising team.

## Your task
Extract a structured profile from the raw CRM data below. This will be used to score this LP against a specific GP opportunity.

## GP opportunity context (what we're matching against)
{gp_context}

## LP CRM data

### Name: {lp['name']}

### Structured fields
- Status: {lp['structured'].get('status') or 'unknown'}
- Check Size: {lp['structured'].get('check_size') or 'unknown'}
- Location: {', '.join(lp['structured'].get('location', [])) or 'unknown'}
- Email: {lp['structured'].get('email') or 'unknown'}

### Call notes and meeting notes
{lp['call_notes'] if lp['call_notes'].strip() else '(No call notes available)'}

## Instructions

### Core profile fields
1. Extract every field in the schema below from the data above.
2. For each field, use ONLY what is explicitly stated or strongly implied. If the data doesn't say, use "unknown" or an empty list — do NOT fabricate.
3. For "framework_type": Classify based on how the LP actually behaves IN VENTURE, not on other asset classes they also touch. Many family offices and institutional allocators run venture, PE, and credit side-by-side — what matters is how they think when deploying venture capital specifically.

   Classify as:
   - **'venture-native'**: LP's venture investments show thesis-first, emerging-manager, early-stage, power-law thinking. Signs: backs first-time funds, small fund sizes, pre-seed/seed focus, "find early be first believer" language, comfortable with concentration, accepts long hold periods, seeks small funds as alternatives to tier-1 established managers. An LP who also does PE/buyout elsewhere is STILL venture-native if their venture activity looks like this.
   - **'pe-crossover'**: LP applies PE/buyout thinking TO their venture investments specifically. Signs: focus on IRR over TVPI, wants downside protection in venture deals, compares venture to fixed income, focuses on cash yield from venture, analyzes portfolio companies by revenue multiples rather than growth trajectory, prefers later-stage "proven" companies framed as de-risked, uses phrases like "pseudo-PE strategies" or "growth PE" to describe venture activity.
   - **'credit-mindset'**: LP evaluates venture through a credit/fixed-income lens. Signs: obsessed with drawdown, capital preservation, yield expectations for venture, stable return profiles.
   - **'traditional-allocator'**: LP follows institutional allocation frameworks — checkbox diversification, strict policy-driven decisions, avoids concentrated bets. Not wrong per se, but not sharp venture thinking either.
   - **'unknown'**: Insufficient data to classify.

   CRITICAL: Do NOT classify as 'pe-crossover' just because an LP mentions PE, buyout, or credit in their overall mandate or past investments. The question is how they think about VENTURE specifically. Example: an LP who says "we do $3M checks into small venture funds as a Sequoia alternative, find early be first believer" is venture-native, even if they also run a $500M PE book. By contrast, an LP who says "we apply our PE diligence framework to venture, focus on downside protection and IRR in our venture deals" is pe-crossover, even if they call themselves a venture investor.
4. For "key_quotes": Pull up to 3 verbatim quotes from the call notes that reveal the LP's intent, preferences, or objections. These will be cited in the final report. Only quote what's actually written — never paraphrase or fabricate.
5. For "conviction_signals": What evidence suggests this LP would actually commit to a fund matching the GP opportunity described above? Be specific.
6. For "conflicting_signals": Note anything that contradicts itself (e.g., states interest in a geography but has an explicit exclusion against it).
7. For "confidence_level": Assess based on the QUALITY of evidence, not quantity of text. The key question: "Was there a real conversation here, or just data entry?"
   - **high**: Multiple real call/meeting conversations captured, specific quotes from the LP, detailed discussion of preferences and past investments, clear conviction signals with context. The notes read like someone summarized a real conversation.
   - **medium**: One real conversation captured with some detail, or multiple brief interactions. Has some specific signals but gaps in key areas. The notes have substance but don't paint a full picture.
   - **low**: No real call conversation — just a structured template, a brief intro blurb, or sparse bullet points without context. Signals are stated but never discussed. Template checkbox data (e.g., "Geography: India, China, SEA") without any call context = low. A few bullet points with no quotes or narrative = low.
   Do NOT use character count as a proxy. A 1200-character note full of LinkedIn URLs and bullet-point headers is still "low." A 400-character note capturing one deeply informative call paragraph could be "medium" or "high."

### CRITICAL RULE for exclusions
"exclusions" must ONLY contain things the LP EXPLICITLY said they WON'T do — hard stated refusals like "US only, no emerging markets" or "we don't do first-time managers." Do NOT treat:
- Absence of experience as an exclusion ("didn't do any FoFs" → goes into note_taker_observations, and "they'll chat internally if it makes sense" → goes into open_questions)
- "Will consider" or "will discuss" language as an exclusion — if they're open to discussing something, it's an open_question, NOT an exclusion
- Current geographic focus as an exclusion ("mostly US/EU" → goes into investment_pattern.typical_geography)
- Lack of interest as an exclusion ("hasn't looked at India" → goes into open_questions)
Only genuine hard refusals like "we will NOT do X" or "X is off the table" go in exclusions. "Didn't do X" means they haven't done it YET — that's different from refusing to do it.

### Contextual enrichment (NEW — critical for scoring accuracy)
8. For "contextual_enrichment": For EVERY significant reference in the call notes, output an enrichment object. Categories:
   - **Fund/firm names** — type, geography, stage, approximate size. E.g., "A91" → India-focused growth-stage fund, ~$500M+
   - **People and roles** — professional context. E.g., "ex Alta advisers" → London-based FO advisory firm, contact has professional FO background
   - **Institutional types** — decode acronyms. E.g., "OCIO" → Outsourced CIO, manages portfolios for foundations/endowments with discretionary authority
   - **Industry terms implying investment approach** — E.g., "pseudo-PE strategies" → applying PE frameworks to venture, framework mismatch. "DPI" → distributions to paid-in capital, realized cash returns
   - **Companies/assets as investments** — E.g., "big position in Reliance" → India's largest conglomerate, indicates comfort with India exposure at scale
   - **Relationship references** — E.g., "He sees us as the most sophisticated fund investor people" → high trust signal. "Marina said to chat to us" → warm referral

   Each object must have: {{"reference": "exact text", "type": "fund|person|institution|term|asset|relationship", "context": "one sentence — what it is", "relevance": "what this tells us about LP fit"}}
   Rules: only enrich references actually in notes, output "unknown" if unrecognized — never guess, one sentence max per context, cover ALL reference types. Do not enrich obvious terms like city names.

### Internal observations and pipeline signals
9. For "note_taker_observations": Extract internal team judgments that are NOT LP-stated facts. Things like "seem quite slow," "doesn't have much clue," "young dude vibing well with us," "sounds a bit stuck." These are internal assessments by the note-taker.
10. For "temporal_signals": Extract timing intelligence: latest_interaction_date (ISO date if mentioned, else "unknown"), recency ("recent" if < 3 months, "stale" if > 6 months, else "unknown"), notes_freshness (your assessment of how current the intelligence seems).
11. For "pending_actions": Extract next steps, follow-ups, action items. "Reconnect end of Jan," "send materials," "they'll chat internally."
12. For "investment_pattern": This MUST be synthesized FROM your contextual_enrichment entries of type "fund" and "asset" — not generated independently. Look at every fund/asset you enriched above, then synthesize:
   - typical_fund_size: the size range of funds they invest in (based on enriched fund sizes)
   - typical_geography: where they deploy capital (based on enriched fund geographies)
   - typical_stage: what stages they back (based on enriched fund stages)
   - concentration: "few deep relationships" vs "many shallow" vs "unknown"
   - buying_profile: ONE sentence characterizing the LP's overall investment personality based on ALL past investments together. Examples: "Category 1 institutional allocator — backs brand-name funds at scale", "Emerging-market explorer — deploys across EM geographies through both funds and directs", "FoF-native — invests through fund-of-funds vehicles, comfortable paying double fees", "Opportunistic generalist — invests across stages and geographies with no clear pattern". Be specific and honest.
   - pattern_fit_with_gp: ONE sentence assessing how well the LP's revealed buying pattern matches the GP opportunity described above. Compare BEHAVIOR (what they actually invested in) to the GP profile — not stated preferences. If every past investment is a $500M+ established fund, say "weak fit — track record is entirely large established funds, no evidence of backing emerging managers" even if the LP mentioned the GP's geography once. If past investments include similar-profile funds, say "strong fit." Be candid.

### Signal source quality assessment
13. For "signal_source_quality": Assess the quality and reliability of the data you are extracting from.
   - primary_source: What kind of data drives most of your extraction? "call_conversation" (detailed meeting/call notes with back-and-forth, specific quotes, narrative), "structured_template" (form-filled data, checkboxes, category lists with minimal context), "brief_notes" (a few bullet points or one-liners, not enough for deep analysis)
   - depth: "deep" (multiple detailed conversations or extensive single call), "moderate" (one good call or reasonable notes), "shallow" (template fill-in, few bullet points, or mostly inferred)
   - conviction_reliability: "high" (your extraction signals come from detailed call discussions with specific evidence), "medium" (mix of call data and structured fields), "low" (mostly from templates, structured fields, or inferred — the LP might check boxes for "India" without ever discussing it in conversation)

### Decision, organization, and competitive fields
14. For "decision_process": How does this LP make investment decisions? approach (thesis-first / relationship-driven / opportunistic / process-driven / unknown), vehicle_preference (funds only / funds+directs+publics / vehicle-agnostic / unknown), decision_speed (fast / moderate / slow / unknown), evidence (specific quote or observation supporting your assessment).
15. For "organizational_dynamics": Who decides and how? decision_maker (the actual person who signs off), internal_alignment (aligned / friction / unknown — is there tension between family members, IC committees, etc.?), team_capacity (dedicated venture team / part-time / solo / unknown), evidence.
16. For "competitive_positioning": Other funds or relationships mentioned in the notes. For each: competitor name, LP sentiment toward them (positive / negative / neutral), and a relevant quote. This reveals what the LP values and where the GP opportunity fits relative to alternatives.
17. For "open_questions": Unresolved items worth exploring in the next conversation. These are NOT negatives — they are conversation starters. E.g., "Haven't done FoFs — open to discussing," "No EM exposure but no stated objection," "Unclear on minimum check size." Things that need clarification, not things that are disqualifying.

## Output schema
{schema_desc}

## Output format
Return ONLY a valid JSON object matching the schema above. No markdown, no explanation, no wrapping — just the JSON."""


def _call_extraction(client, prompt):
    """Single Claude extraction call at temperature 0. Returns parsed dict or error dict."""
    response = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=4000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text.strip()

    # Handle cases where Claude wraps JSON in markdown code blocks
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines)

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        return {"_parse_error": str(e), "_raw": raw_text[:1000]}


def _post_extraction_cleanup(extracted):
    """Fix common extraction mistakes that the prompt can't fully prevent.

    If the same topic appears in both exclusions and open_questions, the LP
    is open to discussing it — remove it from exclusions. "Didn't do FoFs"
    + "will chat internally" = open question, not a hard refusal.
    """
    exclusions = extracted.get("exclusions", [])
    open_questions = extracted.get("open_questions", [])
    if not exclusions or not open_questions:
        return

    oq_text = " ".join(open_questions).lower()
    cleaned = []
    for excl in exclusions:
        # If the exclusion topic also appears in open_questions, it's not a hard refusal
        excl_lower = excl.lower()
        # Extract key terms from the exclusion to check against open_questions
        if any(term in oq_text for term in excl_lower.split() if len(term) > 3):
            continue  # drop — it's an open question, not an exclusion
        cleaned.append(excl)

    extracted["exclusions"] = cleaned


def extract_single(client, lp, gp_profile):
    """Run extraction on a single LP (single pass, temperature 0)."""
    prompt = build_extraction_prompt(lp, gp_profile)
    lp["extracted"] = _call_extraction(client, prompt)
    if "_parse_error" not in lp["extracted"]:
        _post_extraction_cleanup(lp["extracted"])
    return lp


def extract_all(client, lps, gp_profile, max_workers=5):
    """Run extraction on all LPs in parallel. Returns the same list with 'extracted' added.

    Skips LPs that already have a valid 'extracted' field (for resume after rate limits).
    Uses ThreadPoolExecutor for I/O-bound parallelism.
    """
    to_extract = [
        lp for lp in lps
        if not (lp.get("extracted") and "_parse_error" not in lp.get("extracted", {}))
    ]

    skipped = len(lps) - len(to_extract)
    if skipped:
        print(f"  Skipping {skipped} already-extracted LPs.")

    print(f"  Extracting {len(to_extract)} LPs with {max_workers} parallel workers...\n")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(extract_single, client, lp, gp_profile): lp
            for lp in to_extract
        }

        for i, future in enumerate(as_completed(futures), start=1):
            lp = futures[future]
            try:
                future.result()
                if "_parse_error" in lp.get("extracted", {}):
                    print(f"  [{i}/{len(to_extract)}] PARSE ERROR: {lp['name']}")
                else:
                    conf = lp["extracted"].get("confidence_level", "?")
                    signals = len(lp["extracted"].get("conviction_signals", []))
                    print(f"  [{i}/{len(to_extract)}] Done: {lp['name']} (confidence={conf}, signals={signals})")
            except Exception as e:
                print(f"  [{i}/{len(to_extract)}] ERROR on {lp['name']}: {e}")

    # Print summary
    extracted_count = sum(
        1 for lp in lps
        if lp.get("extracted") and "_parse_error" not in lp.get("extracted", {})
    )
    print(f"\n  Extracted: {extracted_count}/{len(lps)} LPs")

    return lps


# --- Standalone ---
# Usage:
#   python3 extract.py                  # extract all LPs, save to output/
#   python3 extract.py "GEM"            # extract one LP, print only (no save)
# Reads:  Notion API (live)
# Writes: output/extracted_profiles.json

if __name__ == "__main__":
    import sys
    import os
    from config import GP_PROFILE, NOTION_DATABASE_ID, ANTHROPIC_API_KEY, output_path
    from notion_reader import fetch_all_lps, fetch_lp_by_name

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    extract_out = output_path("extracted_profiles.json")

    if len(sys.argv) > 1 and sys.argv[1] != "--all":
        # Single LP mode — print only, no save
        name = sys.argv[1]
        print(f"Fetching LP '{name}'...\n")
        lp = fetch_lp_by_name(NOTION_DATABASE_ID, name)
        if lp:
            print(f"\nExtracting...\n")
            extract_single(client, lp, GP_PROFILE)
            print(json.dumps(lp["extracted"], indent=2))
    else:
        # All LPs — extract and save (with resume support)
        # Load existing progress if available
        existing = {}
        if os.path.exists(extract_out):
            with open(extract_out) as f:
                for prev_lp in json.load(f):
                    if prev_lp.get("extracted") and "_parse_error" not in prev_lp.get("extracted", {}):
                        existing[prev_lp["name"]] = prev_lp

        print("Fetching all LPs from Notion...\n")
        lps = fetch_all_lps(NOTION_DATABASE_ID)

        # Restore previously extracted data
        if existing:
            restored = 0
            for lp in lps:
                if lp["name"] in existing:
                    lp["extracted"] = existing[lp["name"]]["extracted"]
                    restored += 1
            if restored:
                print(f"\nRestored {restored} previously extracted LPs.")

        print(f"\nExtracting {len(lps)} LPs (skipping already-extracted)...\n")
        extract_all(client, lps, GP_PROFILE)

        os.makedirs("output", exist_ok=True)
        with open(extract_out, "w") as f:
            json.dump(lps, f, indent=2, default=str)
        print(f"\nSaved extracted profiles to {extract_out}")

        for lp in lps:
            conf = lp.get("extracted", {}).get("confidence_level", "?")
            print(f"  {lp['name']:30s} confidence={conf}")
