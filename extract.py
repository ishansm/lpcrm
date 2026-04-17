"""
Stage 2: Claude extraction pass — two-phase.

Phase 1 (base): GP-agnostic. Pulls LP facts — sector/geo interests, framework
type, past investments, enrichment, quotes, pattern — from call notes alone.
Cached in output/extracted_base.json and reused across GPs.

Phase 2 (gp_fit): GP-specific. Given the cached base + a GP profile, produces
just `conviction_signals` (evidence this LP would commit to THIS GP) and
`investment_pattern.pattern_fit_with_gp`. Small prompt, quick call.

Each per-GP file (output/extracted_profiles_<slug>.json) is the base merged
with that GP's gp_fit. Downstream consumers (score.py, rationale.py,
query.py) see the same shape as before.

Key design choices:
  - Distinguishes explicitly stated vs. inferred vs. absent
  - Detects wrong asset class frameworks (PE/credit mindset)
  - Flags direct quotes for citation in the final report
  - Identifies conflicting signals
  - Resume support: re-run to pick up where rate limits interrupted
  - Base cache: switching GP reuses 95% of extraction work
"""

import copy
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
from config import EXTRACTION_MODEL


BASE_PATH = os.path.join("output", "extracted_base.json")


# Fields that describe LP-side facts and don't change per GP. Extracted once
# per LP in Phase 1 and cached in BASE_PATH.
BASE_SCHEMA = {
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
    "key_quotes": "list of strings — direct quotes from call notes worth citing (max 3)",
    # --- Enrichment fields ---
    "contextual_enrichment": "list of objects — enrich every significant reference in the notes. Each object: {reference, type, context, relevance}. See instructions below.",
    "note_taker_observations": "list of strings — internal team judgments from notes that are NOT LP-stated facts (e.g. 'seem quite slow', 'young dude vibing well with us')",
    "temporal_signals": "object — {latest_interaction_date: ISO date or 'unknown', recency: 'recent'/'stale'/'unknown', notes_freshness: string assessment}",
    "pending_actions": "list of strings — next steps, follow-ups, action items mentioned in notes",
    "investment_pattern": "object — synthesized FROM contextual_enrichment fund/asset entries (not independently). Fields: {typical_fund_size: string range, typical_geography: string, typical_stage: string, concentration: 'few deep relationships'/'many shallow'/'unknown', buying_profile: string — one sentence characterizing the LP's investment personality based on ALL past investments}",
    "signal_source_quality": "object — {primary_source: 'call_conversation'/'structured_template'/'brief_notes', depth: 'deep'/'moderate'/'shallow', conviction_reliability: 'high'/'medium'/'low'}",
    # --- Decision and organizational fields ---
    "decision_process": "object — {approach: 'thesis-first'/'relationship-driven'/'opportunistic'/'process-driven'/'unknown', vehicle_preference: 'funds only'/'funds+directs+publics'/'vehicle-agnostic'/'unknown', decision_speed: 'fast'/'moderate'/'slow'/'unknown', evidence: string — specific quote or observation}",
    "organizational_dynamics": "object — {decision_maker: string (who decides), internal_alignment: 'aligned'/'friction'/'unknown', team_capacity: 'dedicated venture team'/'part-time'/'solo'/'unknown', evidence: string}",
    "competitive_positioning": "list of objects — each: {competitor: string (name of competing fund/relationship), lp_sentiment: 'positive'/'negative'/'neutral', quote: string}",
    "open_questions": "list of strings — unresolved items worth exploring in next conversation. NOT scoring penalties. E.g., 'Haven't done FoFs — open to discussing', 'No EM exposure but no stated objection'",
}


# Fields that require the specific GP opportunity to be extracted meaningfully.
# Re-run per GP; small prompt.
GP_FIT_SCHEMA = {
    "conviction_signals": "list of strings — strongest evidence this LP would actually commit to the specific GP opportunity described above. Be specific.",
    "pattern_fit_with_gp": "string — ONE sentence assessing how the LP's revealed buying pattern (from investment_pattern + past_investments) matches the GP opportunity. Compare BEHAVIOR (what they actually invested in) to the GP profile — not stated preferences. If every past investment is a $500M+ established fund, say 'weak fit — track record is entirely large established funds, no evidence of backing emerging managers' even if the LP mentioned the GP's geography once. If past investments include similar-profile funds, say 'strong fit'. Be candid.",
}


# Back-compat alias in case any external tool reflects on this name.
EXTRACTION_SCHEMA = {**BASE_SCHEMA, **GP_FIT_SCHEMA}


def _gp_context_block(gp_profile):
    """Render the GP opportunity as a short bullet list for the GP-fit pass."""
    lines = []
    if gp_profile.get("name"):
        lines.append(f"- Fund: {gp_profile['name']}")
    if gp_profile.get("fund_size"):
        lines.append(f"- Fund size: {gp_profile['fund_size']}")
    if gp_profile.get("stage"):
        lines.append(f"- Stage: {gp_profile['stage']}")
    if gp_profile.get("manager_type"):
        lines.append(f"- Manager type: {gp_profile['manager_type']}")
    if gp_profile.get("sectors"):
        lines.append(f"- Sectors: {', '.join(gp_profile['sectors'])}")
    if gp_profile.get("geography"):
        lines.append(f"- Geography: {gp_profile['geography']}")
    if gp_profile.get("broader_geo"):
        lines.append(f"- Broader geo: {', '.join(gp_profile['broader_geo'])}")
    if gp_profile.get("lp_product_category"):
        lines.append(f"- Product category: {gp_profile['lp_product_category']}")
    if gp_profile.get("key_traits"):
        lines.append(f"- Key traits: {'; '.join(gp_profile['key_traits'])}")
    return "\n".join(lines)


def build_base_prompt(lp):
    """Phase 1 prompt — GP-agnostic. Extracts LP facts from raw CRM data.
    Output matches BASE_SCHEMA."""
    schema_desc = json.dumps(BASE_SCHEMA, indent=2)

    return f"""You are an LP intelligence analyst for a venture capital fundraising team.

## Your task
Extract a structured profile of this LP from the raw CRM data below. Focus on LP-side facts — who they are, what they invest in, how they think — NOT on fit with any specific GP. A separate pass handles GP fit.

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

### CRITICAL: Default to "unknown" for inferred attribute values

For these fields specifically, do NOT infer a value from general vibes or adjacent information. If the call notes do not explicitly discuss the attribute, the value MUST be "unknown":

- bandwidth: only set to "high"/"medium"/"low" if notes explicitly mention workload, capacity, being busy, being free, number of active deals, or similar. Do not infer bandwidth from tone, engagement level, LP type, how much they talked, or how institutional they seem.
- timing_readiness: only set if notes discuss when they're ready to commit, a specific timeline, or explicit timing language. Do not infer from CRM status alone.
- risk_tolerance: only set if notes discuss risk appetite, concentration, diversification preferences, or comparable indicators. Do not infer from fund types they've invested in.
- involvement_style: only set if notes discuss how they engage with GPs (board seats, advisory, hands-off, active). Do not infer from LP type.

"Unknown" is valuable. It tells the partner "this needs discovery in the call." A plausible-but-invented value misleads the partner into thinking it's known.

Example — WRONG:
Notes say: "Warm 30-minute call. Interested in emerging managers. Already has Essence and Browder in portfolio."
Bad extraction: bandwidth=medium (no workload discussion in notes)

Example — RIGHT:
Same notes.
Good extraction: bandwidth=unknown

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
4. For "key_quotes": Pull up to 3 verbatim quotes from the call notes that reveal the LP's intent, preferences, or objections. These will be cited in the final report. Only quote what's actually written — never paraphrase or fabricate. Each list item must be ONE standalone quote — do not merge two separate quotes into one string with bullets, dashes, or separators like "quote A • quote B". If there are two distinct quotes, they are two distinct list items.
5. For "conflicting_signals": Note anything that contradicts itself (e.g., states interest in a geography but has an explicit exclusion against it).
6. For "confidence_level": Assess based on the QUALITY of evidence, not quantity of text. The key question: "Was there a real conversation here, or just data entry?"
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

### Contextual enrichment
7. For "contextual_enrichment": For EVERY significant reference in the call notes, output an enrichment object. Categories:
   - **Fund/firm names** — type, geography, stage, approximate size. E.g., "A91" → India-focused growth-stage fund, ~$500M+
   - **People and roles** — professional context. E.g., "ex Alta advisers" → London-based FO advisory firm, contact has professional FO background
   - **Institutional types** — decode acronyms. E.g., "OCIO" → Outsourced CIO, manages portfolios for foundations/endowments with discretionary authority
   - **Industry terms implying investment approach** — E.g., "pseudo-PE strategies" → applying PE frameworks to venture, framework mismatch. "DPI" → distributions to paid-in capital, realized cash returns
   - **Companies/assets as investments** — E.g., "big position in Reliance" → India's largest conglomerate, indicates comfort with India exposure at scale
   - **Relationship references** — E.g., "He sees us as the most sophisticated fund investor people" → high trust signal. "Marina said to chat to us" → warm referral

   Each object must have: {{"reference": "exact text", "type": "fund|person|institution|term|asset|relationship", "context": "one sentence — what it is", "relevance": "what this tells us about the LP's nature, approach, or relationships"}}
   Rules: only enrich references actually in notes, output "unknown" if unrecognized — never guess, one sentence max per context, cover ALL reference types. Do not enrich obvious terms like city names. Keep "relevance" about the LP's character — not about any specific GP opportunity.

### Internal observations and pipeline signals
8. For "note_taker_observations": Extract internal team judgments that are NOT LP-stated facts. Things like "seem quite slow," "doesn't have much clue," "young dude vibing well with us," "sounds a bit stuck." These are internal assessments by the note-taker.
9. For "temporal_signals": Extract timing intelligence: latest_interaction_date (ISO date if mentioned, else "unknown"), recency ("recent" if < 3 months, "stale" if > 6 months, else "unknown"), notes_freshness (your assessment of how current the intelligence seems).
10. For "pending_actions": Extract next steps, follow-ups, action items. "Reconnect end of Jan," "send materials," "they'll chat internally."
11. For "investment_pattern": This MUST be synthesized FROM your contextual_enrichment entries of type "fund" and "asset" — not generated independently. Look at every fund/asset you enriched above, then synthesize:
   - typical_fund_size: the size range of funds they invest in (based on enriched fund sizes)
   - typical_geography: where they deploy capital (based on enriched fund geographies)
   - typical_stage: what stages they back (based on enriched fund stages)
   - concentration: "few deep relationships" vs "many shallow" vs "unknown"
   - buying_profile: ONE sentence characterizing the LP's overall investment personality based on ALL past investments together. Examples: "Category 1 institutional allocator — backs brand-name funds at scale", "Emerging-market explorer — deploys across EM geographies through both funds and directs", "FoF-native — invests through fund-of-funds vehicles, comfortable paying double fees", "Opportunistic generalist — invests across stages and geographies with no clear pattern". Be specific and honest.
   Do NOT produce pattern_fit_with_gp here — that's the GP-fit pass's job.

### Signal source quality assessment
12. For "signal_source_quality": Assess the quality and reliability of the data you are extracting from.
   - primary_source: What kind of data drives most of your extraction? "call_conversation" (detailed meeting/call notes with back-and-forth, specific quotes, narrative), "structured_template" (form-filled data, checkboxes, category lists with minimal context), "brief_notes" (a few bullet points or one-liners, not enough for deep analysis)
   - depth: "deep" (multiple detailed conversations or extensive single call), "moderate" (one good call or reasonable notes), "shallow" (template fill-in, few bullet points, or mostly inferred)
   - conviction_reliability: "high" (your extraction signals come from detailed call discussions with specific evidence), "medium" (mix of call data and structured fields), "low" (mostly from templates, structured fields, or inferred — the LP might check boxes for "India" without ever discussing it in conversation)

### Decision, organization, and competitive fields
13. For "decision_process": How does this LP make investment decisions? approach (thesis-first / relationship-driven / opportunistic / process-driven / unknown), vehicle_preference (funds only / funds+directs+publics / vehicle-agnostic / unknown), decision_speed (fast / moderate / slow / unknown), evidence (specific quote or observation supporting your assessment).
14. For "organizational_dynamics": Who decides and how? decision_maker (the actual person who signs off), internal_alignment (aligned / friction / unknown — is there tension between family members, IC committees, etc.?), team_capacity (dedicated venture team / part-time / solo / unknown), evidence.
15. For "competitive_positioning": Other funds or relationships mentioned in the notes. For each: competitor name, LP sentiment toward them (positive / negative / neutral), and a relevant quote. This reveals what the LP values relative to alternatives.
16. For "open_questions": Unresolved items worth exploring in the next conversation. These are NOT negatives — they are conversation starters. E.g., "Haven't done FoFs — open to discussing," "No EM exposure but no stated objection," "Unclear on minimum check size." Things that need clarification, not things that are disqualifying.

## Output schema
{schema_desc}

## Output format
Return ONLY a valid JSON object matching the schema above. No markdown, no explanation, no wrapping — just the JSON."""


def build_gp_fit_prompt(lp, base_extracted, gp_profile):
    """Phase 2 prompt — given a pre-extracted LP base and a specific GP
    opportunity, produce the two GP-sensitive fields. Small, focused call."""
    gp_context = _gp_context_block(gp_profile)

    # Pick just the base fields the GP-fit reasoning needs — don't ship the
    # whole object, keeps the prompt tight.
    relevant = {
        "name": lp["name"],
        "framework_type": base_extracted.get("framework_type", "unknown"),
        "sector_interests": base_extracted.get("sector_interests", []),
        "geography_interests": base_extracted.get("geography_interests", []),
        "past_investments": base_extracted.get("past_investments", []),
        "exclusions": base_extracted.get("exclusions", []),
        "check_size_range": base_extracted.get("check_size_range", "unknown"),
        "min_fund_size": base_extracted.get("min_fund_size", "unknown"),
        "key_quotes": base_extracted.get("key_quotes", []),
        "investment_pattern": base_extracted.get("investment_pattern", {}),
        "contextual_enrichment": base_extracted.get("contextual_enrichment", []),
    }
    lp_blob = json.dumps(relevant, indent=2)

    return f"""You are an LP intelligence analyst scoring opportunity fit. The LP profile below has already been extracted from call notes. Your job is to produce just two GP-specific fields for the opportunity described.

## GP opportunity
{gp_context}

## LP profile (pre-extracted)
{lp_blob}

## Task
Produce exactly these two fields, grounded in the LP profile above:

1. "conviction_signals": list of strings — strongest evidence this specific LP would actually commit to the GP described. Cite concrete items (past_investments, exclusions, quotes, investment_pattern.buying_profile, contextual_enrichment entries) — do not speculate beyond the profile. If there is no evidence, return an empty list, not a hedged sentence.

2. "pattern_fit_with_gp": ONE sentence assessing how the LP's revealed buying pattern (investment_pattern + past_investments) matches the GP opportunity. Compare BEHAVIOR to the GP profile — not stated preferences. If every past investment is a $500M+ established fund, say "weak fit — track record is entirely large established funds, no evidence of backing emerging managers" even if sector_interests mentions the GP's sector. If past investments include similar-profile funds, say "strong fit — [specific fund name] matches this GP's [profile feature]". Be candid.

## Output format
Return ONLY a JSON object: {{"conviction_signals": [...], "pattern_fit_with_gp": "..."}}. No markdown, no explanation."""


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


_BULLET_PIPE_SEP = re.compile(r"\s*[•|]\s*")
_SENTENCE_DASH_SEP = re.compile(r"\s+[—–]\s+(?=[A-Z0-9])")


def split_merged_quotes(entries):
    """Split list entries that glue multiple standalone quotes together with
    list separators. Extraction sometimes returns one string like
    "quote A • quote B" despite the prompt instructing one quote per item.

    Rules:
    - `•` and `|` never appear inside real quotes — split unconditionally.
    - Em/en-dashes are legitimate punctuation (asides, appositives), so split
      only when used between sentence-like fragments: surrounded by spaces,
      right side starts with a capital letter or digit, each resulting
      fragment has at least 3 words.
    """
    out = []
    for e in entries or []:
        if not isinstance(e, str):
            out.append(e)
            continue
        parts = _BULLET_PIPE_SEP.split(e)
        expanded = []
        for p in parts:
            candidates = _SENTENCE_DASH_SEP.split(p)
            if len(candidates) > 1 and all(
                len(c.split()) >= 3 for c in candidates
            ):
                expanded.extend(candidates)
            else:
                expanded.append(p)
        for x in expanded:
            x = x.strip()
            if x:
                out.append(x)
    return out


def _post_extraction_cleanup(extracted):
    """Fix common extraction mistakes that the prompt can't fully prevent.

    1. If an exclusion is substantially redundant with an open_question
       (most of its content words appear in a single open_question), drop
       it — the LP is open to discussing it, so it's not a hard refusal.
    2. Split key_quotes entries that glue two standalone quotes together
       with bullet, pipe, or sentence-separating em/en-dash.
    """
    quotes = extracted.get("key_quotes")
    if isinstance(quotes, list):
        extracted["key_quotes"] = split_merged_quotes(quotes)

    exclusions = extracted.get("exclusions", [])
    open_questions = extracted.get("open_questions", [])
    if not exclusions or not open_questions:
        return

    STOP_WORDS = {"will", "wont", "don", "not", "the", "and", "for", "with",
                  "any", "all", "our", "their", "them", "they", "that", "this",
                  "have", "has", "had", "been", "into", "from", "about"}

    def content_words(text):
        return {w for w in text.lower().split()
                if len(w) > 3 and w not in STOP_WORDS}

    cleaned = []
    for excl in exclusions:
        excl_words = content_words(excl)
        if not excl_words:
            cleaned.append(excl)
            continue
        # Drop only if a SINGLE open_question covers >=60% of the exclusion's
        # content words. One-word overlap is not enough.
        redundant = False
        for oq in open_questions:
            oq_words = content_words(oq)
            overlap = excl_words & oq_words
            if len(overlap) / len(excl_words) >= 0.6:
                redundant = True
                break
        if not redundant:
            cleaned.append(excl)

    extracted["exclusions"] = cleaned


# ---------------------------------------------------------------------------
# Base cache I/O and merge helpers
# ---------------------------------------------------------------------------

def _base_subset(extracted):
    """Return the BASE_SCHEMA-subset of a full extraction dict. Used to
    hydrate the base cache from existing per-GP extractions without
    re-running the base pass."""
    if not isinstance(extracted, dict) or "_parse_error" in extracted:
        return None
    base_keys = set(BASE_SCHEMA.keys())
    result = {k: copy.deepcopy(v) for k, v in extracted.items() if k in base_keys}
    # investment_pattern lives in BASE but pattern_fit_with_gp inside it is
    # GP-specific — strip it out of the base snapshot.
    ip = result.get("investment_pattern")
    if isinstance(ip, dict):
        result["investment_pattern"] = {
            k: v for k, v in ip.items() if k != "pattern_fit_with_gp"
        }
    return result


def _merge_gp_fit(base, gp_fit):
    """Combine the base extraction with GP-specific fields to produce the
    full `extracted` dict consumers expect."""
    merged = copy.deepcopy(base)
    if isinstance(gp_fit.get("conviction_signals"), list):
        merged["conviction_signals"] = gp_fit["conviction_signals"]
    else:
        merged.setdefault("conviction_signals", [])
    pfg = gp_fit.get("pattern_fit_with_gp")
    if pfg is not None:
        ip = merged.setdefault("investment_pattern", {})
        if isinstance(ip, dict):
            ip["pattern_fit_with_gp"] = pfg
    return merged


def load_base_cache(path=BASE_PATH):
    """Load the GP-agnostic base cache. Returns {lp_name: base_dict}."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    cache = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        base = entry.get("extracted")
        if name and isinstance(base, dict) and "_parse_error" not in base:
            cache[name] = base
    return cache


def save_base_cache(cache, path=BASE_PATH):
    """Write the base cache as a list of {name, extracted} entries sorted
    by name. Creates the output directory if missing."""
    entries = [
        {"name": name, "extracted": base}
        for name, base in sorted(cache.items())
    ]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(entries, f, indent=2, default=str)


def hydrate_base_cache_from_lps(cache, lps):
    """Backfill base cache from LPs that already have a full extraction
    (e.g. restored from a previous run). Only adds entries the cache is
    missing; never overwrites."""
    added = 0
    for lp in lps:
        name = lp.get("name")
        if not name or name in cache:
            continue
        base = _base_subset(lp.get("extracted"))
        if base:
            cache[name] = base
            added += 1
    return added


# ---------------------------------------------------------------------------
# Extraction passes
# ---------------------------------------------------------------------------

def extract_base_single(client, lp):
    """Phase 1: run the base extraction for one LP. Returns the base dict
    (or an error dict with _parse_error)."""
    prompt = build_base_prompt(lp)
    return _call_extraction(client, prompt)


def extract_gp_fit_single(client, lp, base_extracted, gp_profile):
    """Phase 2: given an LP's base extraction and a GP profile, produce the
    two GP-specific fields. Returns the gp_fit dict (or an error dict)."""
    prompt = build_gp_fit_prompt(lp, base_extracted, gp_profile)
    return _call_extraction(client, prompt)


def extract_single(client, lp, gp_profile, base_cache=None, cache_lock=None):
    """Two-phase extraction for one LP. Uses the base cache if the LP has a
    cached base; otherwise runs Phase 1 and stores the result. Then runs
    Phase 2 for this GP and merges into lp["extracted"].

    base_cache: shared dict {lp_name: base_dict}. Pass None to skip caching
    (useful for one-off extraction, e.g. the single-LP CLI path).
    cache_lock: threading.Lock guarding base_cache across workers.
    """
    name = lp["name"]

    base = None
    if base_cache is not None:
        # Read under lock — cache is shared across workers.
        if cache_lock is not None:
            with cache_lock:
                base = base_cache.get(name)
        else:
            base = base_cache.get(name)

    if base is None:
        base = extract_base_single(client, lp)
        if "_parse_error" in base:
            lp["extracted"] = base
            return lp
        if base_cache is not None:
            if cache_lock is not None:
                with cache_lock:
                    base_cache[name] = base
            else:
                base_cache[name] = base

    gp_fit = extract_gp_fit_single(client, lp, base, gp_profile)
    if "_parse_error" in gp_fit:
        # Don't block the pipeline — fall back to base with empty gp_fit
        # fields, tagged so downstream can tell the pass failed.
        merged = _merge_gp_fit(base, {"conviction_signals": [], "pattern_fit_with_gp": ""})
        merged["_gp_fit_parse_error"] = gp_fit.get("_parse_error", "unknown")
        lp["extracted"] = merged
    else:
        lp["extracted"] = _merge_gp_fit(base, gp_fit)

    _post_extraction_cleanup(lp["extracted"])
    return lp


def extract_all(client, lps, gp_profile, max_workers=5, save_path=None,
                base_cache=None, base_save_path=None):
    """Run two-phase extraction on all LPs in parallel. Returns the same
    list with 'extracted' added.

    Skips LPs that already have a valid 'extracted' field (for resume after
    rate limits or GP restore). When base_cache is provided, hydrates it
    from any restored LPs before starting so subsequent GP runs can reuse
    the base pass.

    base_cache: optional dict {lp_name: base_dict}. Mutated in place.
    base_save_path: if set, the base cache is written here after extraction.
    """
    if base_cache is not None:
        hydrated = hydrate_base_cache_from_lps(base_cache, lps)
        if hydrated:
            print(f"  Hydrated base cache from {hydrated} already-extracted LPs.")

    to_extract = [
        lp for lp in lps
        if not (lp.get("extracted") and "_parse_error" not in lp.get("extracted", {}))
    ]

    skipped = len(lps) - len(to_extract)
    if skipped:
        print(f"  Skipping {skipped} already-extracted LPs.")

    if base_cache is not None:
        need_base = sum(1 for lp in to_extract if lp["name"] not in base_cache)
        have_base = len(to_extract) - need_base
        if have_base:
            print(f"  Base cache hit for {have_base}/{len(to_extract)} LPs — GP-fit pass only.")

    print(f"  Extracting {len(to_extract)} LPs with {max_workers} parallel workers...\n")

    cache_lock = threading.Lock() if base_cache is not None else None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                extract_single, client, lp, gp_profile, base_cache, cache_lock,
            ): lp
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
                    gp_fit_err = "_gp_fit_parse_error" in lp["extracted"]
                    tag = " [gp-fit parse error]" if gp_fit_err else ""
                    print(f"  [{i}/{len(to_extract)}] Done: {lp['name']} (confidence={conf}, signals={signals}){tag}")
            except Exception as e:
                print(f"  [{i}/{len(to_extract)}] ERROR on {lp['name']}: {e}")

            # Incremental save — if anything crashes, we don't lose work.
            if save_path:
                try:
                    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                    with open(save_path, "w") as _f:
                        json.dump(lps, _f, indent=2, default=str)
                except Exception as _e:
                    print(f"    (incremental save failed: {_e})")
            if base_cache is not None and base_save_path:
                try:
                    if cache_lock is not None:
                        with cache_lock:
                            save_base_cache(base_cache, base_save_path)
                    else:
                        save_base_cache(base_cache, base_save_path)
                except Exception as _e:
                    print(f"    (base cache save failed: {_e})")

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
# Writes: output/extracted_profiles_<slug>.json, output/extracted_base.json

if __name__ == "__main__":
    import sys
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
            base_cache = load_base_cache()
            extract_single(client, lp, GP_PROFILE, base_cache=base_cache)
            save_base_cache(base_cache)
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
        base_cache = load_base_cache()
        extract_all(
            client, lps, GP_PROFILE,
            base_cache=base_cache, base_save_path=BASE_PATH,
        )

        os.makedirs("output", exist_ok=True)
        with open(extract_out, "w") as f:
            json.dump(lps, f, indent=2, default=str)
        print(f"\nSaved extracted profiles to {extract_out}")

        for lp in lps:
            conf = lp.get("extracted", {}).get("confidence_level", "?")
            print(f"  {lp['name']:30s} confidence={conf}")
