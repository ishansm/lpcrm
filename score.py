"""
Stage 4-5: Scoring engine.

Rules-based, deterministic, transparent. No AI — just weighted criteria
comparing extracted LP profiles against the GP opportunity.

7 criteria, 0-10 each, weighted per config.WEIGHTS.
All scoring references the GP profile dynamically.
"""

import re
from filter import parse_fund_size


# ---------------------------------------------------------------------------
# Signal quality assessment
# ---------------------------------------------------------------------------

# Known large/established funds — used to detect institutional brand-name preference.
# Maintained manually; substring-matched against past_investments (case-insensitive).
ESTABLISHED_FUNDS = {
    "sequoia", "firstround", "first round", "accel", "a16z", "andreessen",
    "benchmark", "insight partners", "tiger global", "lightspeed",
    "general catalyst", "greylock", "index ventures", "peak xv",
    "bessemer", "ggv", "coatue", "thrive", "founders fund", "khosla", "nea",
    "battery", "iconiq", "ivp", "spark capital", "ia ventures",
    "ribbit", "dst", "softbank", "general atlantic",
    # Removed: "tiger" (too broad), "index" (too broad), "ia" (too broad), "a91" (EM fund)
}

# Emerging market / small / non-brand-name funds — explicitly excluded from the
# established-fund penalty to avoid false positives on LPs with EM exposure.
EMERGING_MARKET_FUNDS = {
    "kaszek", "canary", "lsvp", "a91", "z47", "matrix india",
    "isomer", "vintage", "truebridge", "box group", "pitango",
    "83north", "tlv", "concept", "giant", "whitestar", "superseed",
    "basecase", "browder", "essence", "nomads",
}


def _signal_quality(lp):
    """Multiplier (0.6-1.0) reflecting richness of LP intelligence.

    Uses extracted signal_source_quality.conviction_reliability if available,
    falls back to heuristic for backward compatibility with older extractions.
    """
    ext = lp.get("extracted", {})

    # Prefer extraction-time assessment
    ssq = ext.get("signal_source_quality", {})
    if isinstance(ssq, dict) and ssq.get("conviction_reliability"):
        reliability = ssq["conviction_reliability"].lower()
        return {"high": 1.0, "medium": 0.8, "low": 0.6}.get(reliability, 0.8)

    # Fallback heuristic for older extractions without signal_source_quality
    notes_len = len(lp.get("call_notes", ""))
    has_quotes = bool(ext.get("key_quotes"))
    has_observations = bool(ext.get("note_taker_observations"))
    confidence = ext.get("confidence_level", "unknown").lower()

    if notes_len > 1000 and (has_quotes or has_observations):
        return 1.0
    if notes_len > 500 or confidence in ("medium", "high"):
        return 0.8
    return 0.6


def _is_emerging_gp(gp_profile):
    """Check if the GP is a first-time or emerging manager."""
    mt = gp_profile.get("manager_type", "").lower()
    return "first-time" in mt or "emerging" in mt


# ---------------------------------------------------------------------------
# Individual scoring functions — each returns 0-10
# ---------------------------------------------------------------------------

def score_intellectual_alignment(lp, gp_profile):
    """2.0x — Understands outlier dynamics, seeks contrarian bets, venture-native.

    Signals: framework_type, risk_tolerance, involvement_style, conviction_signals,
    contextual_enrichment (PE-framework terms lower score, venture-native terms boost).
    """
    ext = lp.get("extracted", {})
    score = 5  # baseline

    # Framework type is the strongest signal
    framework = ext.get("framework_type", "unknown").lower()
    if framework == "venture-native":
        score += 3
    elif framework == "traditional-allocator":
        score -= 1
    elif framework in ("pe-crossover", "credit-mindset"):
        score -= 3  # shouldn't reach here (filtered), but safety net

    # Risk tolerance
    risk = ext.get("risk_tolerance", "unknown").lower()
    if risk == "high":
        score += 1
    elif risk == "low":
        score -= 2

    # Conviction signals mentioning contrarian/outlier/emerging manager themes
    conviction = " ".join(ext.get("conviction_signals", [])).lower()
    contrarian_terms = ["emerging manager", "contrarian", "outlier", "unconventional",
                        "first-time", "small fund", "early stage", "power law"]
    hits = sum(1 for t in contrarian_terms if t in conviction)
    score += min(hits, 2)  # cap at +2

    # Involvement style — hands-on LPs often understand venture better
    involvement = ext.get("involvement_style", "unknown").lower()
    if involvement in ("advisory", "hands-on"):
        score += 1

    # Contextual enrichment: PE-framework terms lower score, venture-native boost
    enrichment = ext.get("contextual_enrichment", [])
    pe_terms = 0
    venture_terms = 0
    for item in enrichment:
        if not isinstance(item, dict):
            continue
        ctx = (item.get("context", "") + " " + item.get("relevance", "")).lower()
        if any(t in ctx for t in ("pe framework", "pe-crossover", "credit mindset",
                                   "framework mismatch", "pe strateg")):
            pe_terms += 1
        if any(t in ctx for t in ("venture-native", "emerging manager", "seed stage",
                                   "early-stage", "power law", "outlier")):
            venture_terms += 1
    if pe_terms > venture_terms:
        score -= min(pe_terms, 2)
    elif venture_terms > pe_terms:
        score += min(venture_terms, 1)

    # Competitive positioning — positive Vineyard sentiment boosts alignment
    comp_pos = ext.get("competitive_positioning", [])
    for cp in comp_pos:
        if not isinstance(cp, dict):
            continue
        competitor = (cp.get("competitor") or "").lower()
        sentiment = (cp.get("lp_sentiment") or "").lower()
        if any(t in competitor for t in ("vineyard",)) and sentiment == "positive":
            score += 1
            break

    # --- Established-fund preference penalty ---
    # LPs who predominantly invest in large brand-name funds show institutional
    # thinking, not emerging-manager alignment. Only applies for emerging GPs.
    past = ext.get("past_investments", [])
    pattern = ext.get("investment_pattern", {})

    if _is_emerging_gp(gp_profile) and past:
        established_count = sum(
            1 for inv in past
            if any(ef in inv.lower() for ef in ESTABLISHED_FUNDS)
            and not any(em in inv.lower() for em in EMERGING_MARKET_FUNDS)
        )
        large_size = False
        if isinstance(pattern, dict):
            size_str = (pattern.get("typical_fund_size") or "").lower()
            large_size = any(t in size_str for t in ("100m", "500m", "1b", "billion"))

        if established_count >= 2 or (established_count >= 1 and large_size):
            score -= 3
        elif established_count == 1:
            score -= 2

    # --- FoF / first-believer bonus ---
    # Signals that LP values backing emerging managers, paying FoF fees,
    # being anchor/first-believer. Strong positive for emerging GP fit.
    first_believer_terms = [
        "fof fees", "happy to pay fees", "first believer", "anchor",
        "continuation of", "sees us as", "smaller fund", "committing to us",
        "wants to do it", "ready to commit", "backing emerging",
    ]
    all_signals = conviction + " " + " ".join(ext.get("key_quotes", [])).lower()
    fb_hits = sum(1 for t in first_believer_terms if t in all_signals)

    if _is_emerging_gp(gp_profile) and fb_hits:
        score += 3 if fb_hits >= 2 else 2

    # --- FoF exclusion without emerging-manager conviction ---
    # If LP excludes FoFs and shows no first-believer signals, penalize —
    # thesis-first / vehicle-agnostic LPs without EM conviction aren't aligned.
    exclusions_lower = [e.lower() for e in ext.get("exclusions", []) if e != "unknown"]
    has_fof_exclusion = any("fof" in e or "fund of fund" in e for e in exclusions_lower)
    if has_fof_exclusion and fb_hits == 0:
        decision = ext.get("decision_process", {})
        approach = (decision.get("approach") or "").lower() if isinstance(decision, dict) else ""
        if approach in ("thesis-first", "vehicle-agnostic"):
            score -= 2
        else:
            score -= 1

    return max(0, min(10, score))


def score_active_intent(lp, gp_profile):
    """1.8x — Explicitly stated interest in the GP's sectors/geo/stage.

    Compares LP's stated interests against GP profile fields.
    """
    ext = lp.get("extracted", {})
    score = 0

    # Geography interest overlap
    gp_geos = set()
    if gp_profile.get("geography"):
        for g in gp_profile["geography"].split(","):
            gp_geos.add(g.strip().lower())
    for g in gp_profile.get("broader_geo", []):
        gp_geos.add(g.strip().lower())

    lp_geos = {g.lower() for g in ext.get("geography_interests", [])}
    geo_overlap = gp_geos & lp_geos
    if geo_overlap:
        score += 3

    # Sector interest overlap
    gp_sectors = {s.lower() for s in gp_profile.get("sectors", [])}
    lp_sectors = {s.lower() for s in ext.get("sector_interests", []) if s != "unknown"}
    sector_overlap = gp_sectors & lp_sectors
    if sector_overlap:
        score += 2

    # Stage preference match
    gp_stage = gp_profile.get("stage", "").lower()
    lp_stage = ext.get("fund_stage_pref", "").lower()
    if lp_stage and lp_stage != "unknown":
        # Check for overlap in stage terms
        gp_stage_terms = set(re.split(r"[/,\s]+", gp_stage))
        lp_stage_terms = set(re.split(r"[/,\s]+", lp_stage))
        if gp_stage_terms & lp_stage_terms:
            score += 2
        # Partial match — "early stage" matches "pre-seed/seed"
        elif any(t in lp_stage for t in ("early", "seed", "pre-seed")):
            if any(t in gp_stage for t in ("early", "seed", "pre-seed")):
                score += 2

    # Conviction signals that reference GP-relevant themes
    conviction = " ".join(ext.get("conviction_signals", [])).lower()
    gp_keywords = set()
    for field in ("geography", "stage", "manager_type"):
        if gp_profile.get(field):
            gp_keywords.update(w.lower() for w in re.split(r"[/,\s]+", gp_profile[field]) if len(w) > 2)
    for s in gp_profile.get("sectors", []):
        gp_keywords.add(s.lower())

    keyword_hits = sum(1 for kw in gp_keywords if kw in conviction)
    score += min(keyword_hits, 3)

    # Apply signal quality — template checkbox data scores lower than
    # real conversational signals about active interest
    raw = max(0, min(10, score))
    quality = _signal_quality(lp)
    return max(0, min(10, round(raw * quality)))


def score_demonstrated_behavior(lp, gp_profile):
    """1.5x — Past investments in similar funds, geos, or stages.

    Hard evidence > stated intent. Uses investment_pattern (synthesized from
    enriched fund names) as PRIMARY input, with contextual_enrichment for
    geo/stage/sector signal matching.
    """
    ext = lp.get("extracted", {})
    score = 0
    past = ext.get("past_investments", [])
    pattern = ext.get("investment_pattern", {})
    enrichment = ext.get("contextual_enrichment", [])

    if not past and not pattern:
        return 3  # no data — neutral, not penalized

    # Having past fund investments at all shows LP sophistication
    # Reduced bonus if all investments are in large established funds and GP is emerging
    emerging_gp = _is_emerging_gp(gp_profile)
    if emerging_gp and past:
        all_established = all(
            any(ef in inv.lower() for ef in ESTABLISHED_FUNDS)
            and not any(em in inv.lower() for em in EMERGING_MARKET_FUNDS)
            for inv in past
        )
        score += 1 if all_established else 2
    else:
        score += 2

    # Number of past investments (more = more active)
    score += min(len(past), 3)

    # PRIMARY: investment_pattern — what they actually invested in
    gp_keywords = set()
    for s in gp_profile.get("sectors", []):
        gp_keywords.add(s.lower())
    if gp_profile.get("geography"):
        for g in gp_profile["geography"].split(","):
            gp_keywords.add(g.strip().lower())
    for g in gp_profile.get("broader_geo", []):
        gp_keywords.add(g.strip().lower())
    gp_stage = gp_profile.get("stage", "").lower()

    if isinstance(pattern, dict):
        pattern_text = " ".join(str(v) for v in pattern.values()).lower()
        pattern_hits = sum(1 for kw in gp_keywords if kw in pattern_text)

        # If LP typically invests in large funds ($100M+) and GP is emerging,
        # cap the pattern bonus — investing in large India funds proves geo
        # interest but NOT willingness to back a $20M first-timer.
        large_fund_investor = any(
            t in pattern_text for t in ("100m", "500m", "1b", "billion")
        )
        if emerging_gp and large_fund_investor:
            score += min(pattern_hits, 1)
        else:
            score += min(pattern_hits * 2, 3)

        # Stage match from pattern
        if gp_stage and any(t in pattern_text for t in gp_stage.split("/")):
            score += 1

    # SECONDARY: contextual_enrichment — enriched fund references with geo/stage
    enrichment_hits = 0
    for item in enrichment:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("fund", "asset"):
            continue
        ctx = (item.get("context", "") + " " + item.get("relevance", "")).lower()
        if any(kw in ctx for kw in gp_keywords):
            enrichment_hits += 1
    score += min(enrichment_hits, 2)

    # LP type — institutional/OCIO/endowment have more track record
    lp_type = ext.get("lp_type", "").lower()
    if any(t in lp_type for t in ("ocio", "institutional", "endowment", "fund of funds")):
        score += 1

    # pattern_fit_with_gp guardrail — Claude's direct assessment of behavioral fit
    # overrides keyword-matching when present, acting as a calibration check.
    fit_assessment = ""
    if isinstance(pattern, dict):
        fit_assessment = (pattern.get("pattern_fit_with_gp") or "").lower()
    if fit_assessment:
        if "strong fit" in fit_assessment:
            score = max(score, 7)
        elif "weak fit" in fit_assessment:
            score = min(score, 5)

    return max(0, min(10, score))


def score_sector_alignment(lp, gp_profile):
    """1.3x — Overlap between LP's sector interests and GP's sectors."""
    ext = lp.get("extracted", {})
    gp_sectors = {s.lower() for s in gp_profile.get("sectors", [])}
    lp_sectors = {s.lower() for s in ext.get("sector_interests", []) if s != "unknown"}

    if not gp_sectors:
        return 5  # GP doesn't specify sectors

    if not lp_sectors:
        return 4  # unknown — slight neutral, not penalized

    # Direct overlap
    overlap = gp_sectors & lp_sectors
    if overlap:
        # Scale: 1 overlap = 6, 2 = 7, 3+ = 8
        score = min(5 + len(overlap), 8)
    else:
        # Check for adjacent/related terms
        all_text = " ".join(lp_sectors)
        adjacent_hits = 0
        for gs in gp_sectors:
            # Broad matching — "tech" in "deeptech", "bio" in "bio/lifesciences"
            if any(gs[:3] in ls for ls in lp_sectors):
                adjacent_hits += 1
        score = 3 + min(adjacent_hits, 3)

    # Bonus: conviction signals mention GP sectors
    conviction = " ".join(ext.get("conviction_signals", [])).lower()
    sector_in_conviction = sum(1 for s in gp_sectors if s in conviction)
    score += min(sector_in_conviction, 2)

    return max(0, min(10, score))


def score_geography_match(lp, gp_profile):
    """1.3x — LP interest in the GP's target geography.

    Checks BOTH extracted geography_interests AND structured CRM location field.
    An LP physically located in an emerging market country gets geography credit
    even if their extracted interests don't explicitly mention it.
    """
    ext = lp.get("extracted", {})

    gp_geos = set()
    if gp_profile.get("geography"):
        for g in gp_profile["geography"].split(","):
            gp_geos.add(g.strip().lower())
    for g in gp_profile.get("broader_geo", []):
        gp_geos.add(g.strip().lower())

    lp_geos = {g.lower() for g in ext.get("geography_interests", []) if g.lower() != "unknown"}

    # Also include structured CRM location as geography signal
    structured_locations = {
        loc.lower() for loc in lp.get("structured", {}).get("location", [])
    }

    # Map of countries/cities that count as emerging market geography
    # (used when GP targets emerging markets)
    emerging_market_locations = {
        "india", "kenya", "nigeria", "south africa", "johannesburg", "cape town",
        "brazil", "sao paulo", "buenos aires", "mexico", "colombia",
        "singapore", "indonesia", "vietnam", "thailand", "philippines",
        "dubai", "uae", "saudi arabia", "middle east", "jordan",
        "istanbul", "turkey", "china", "shanghai", "hong kong", "taipei",
        "seoul", "africa", "latin america", "sea",
    }

    if not gp_geos:
        return 5  # GP doesn't specify geography

    # Check structured location against GP geos and emerging markets
    location_geo_score = 0
    if structured_locations:
        # Direct location match with GP geos
        if structured_locations & gp_geos:
            location_geo_score = 8

        # Location is in an emerging market and GP targets emerging markets
        elif gp_geos & {"emerging markets", "africa", "latin america", "sea", "india"} \
                and structured_locations & emerging_market_locations:
            location_geo_score = 5

    # Check extracted geography interests
    extracted_geo_score = 0
    if lp_geos:
        overlap = gp_geos & lp_geos
        if overlap:
            extracted_geo_score = min(6 + len(overlap) * 2, 10)
        else:
            extracted_geo_score = 2
            broad_terms = {"global", "international", "diversified", "multi-region"}
            if lp_geos & broad_terms:
                extracted_geo_score = 5
    elif not structured_locations:
        return 3  # no geo data at all — neutral

    # Check contextual_enrichment for EM exposure via investments
    enrichment = ext.get("contextual_enrichment", [])
    enrichment_geo_score = 0
    for item in enrichment:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("fund", "asset"):
            continue
        ctx = (item.get("context", "") + " " + item.get("relevance", "")).lower()
        if any(geo in ctx for geo in gp_geos):
            enrichment_geo_score = max(enrichment_geo_score, 6)
            break  # one match is enough for the boost

    # Apply signal quality to extracted geo score — template geography data
    # ("Geography: India, SEA") is less meaningful than conversational signals.
    # Location and enrichment scores are inherently verified, so not adjusted.
    quality = _signal_quality(lp)
    extracted_geo_score = round(extracted_geo_score * quality)

    # Take the highest of all three signals
    score = max(extracted_geo_score, location_geo_score, enrichment_geo_score)

    # Exclusion check — if LP explicitly excludes GP geos, heavy penalty
    exclusions = {e.lower() for e in ext.get("exclusions", []) if e != "unknown"}
    for excl in exclusions:
        for geo in gp_geos:
            if geo in excl:
                return 0  # hard zero — shouldn't reach here if filters worked

    return max(0, min(10, score))


def score_check_size_feasibility(lp, gp_profile):
    """1.1x — Can the LP write a check that makes sense for this fund size?

    A $20M fund needs $500K-$3M checks. A $100M fund needs $2M-$15M.
    """
    ext = lp.get("extracted", {})
    gp_fund_m = parse_fund_size(gp_profile.get("fund_size", ""))

    if gp_fund_m is None:
        return 5  # can't evaluate

    # Ideal check size is roughly 2.5%-15% of fund size
    ideal_min = gp_fund_m * 0.025
    ideal_max = gp_fund_m * 0.15

    # Parse LP check size
    check_str = ext.get("check_size_range", "unknown")
    structured_check = lp.get("structured", {}).get("check_size")

    # Try to parse the extracted range first
    check_m = None
    if check_str and check_str != "unknown":
        # Try to extract a number
        numbers = re.findall(r"\$?([\d.]+)\s*([mbk])?", check_str.lower())
        if numbers:
            vals = []
            for num_str, unit in numbers:
                v = float(num_str)
                if unit == "b":
                    v *= 1000
                elif unit == "k":
                    v /= 1000
                vals.append(v)
            check_m = sum(vals) / len(vals)  # average if range

    # Fall back to structured field
    if check_m is None and structured_check:
        check_map = {"personal": 0.1, "small": 0.5, "medium": 2.0, "big": 5.0}
        check_m = check_map.get(structured_check.lower())

    if check_m is None:
        return 4  # unknown — neutral

    # Hard penalty: check size meets or exceeds the entire fund size
    # e.g., LP writes "$20-30M" checks into a $20M fund — they'd own the whole fund
    if check_m >= gp_fund_m * 0.8:
        # Approaches or exceeds fund size — 1-2/10
        if check_m >= gp_fund_m:
            return 1
        else:
            return 2

    # Score based on how well check fits the fund
    if ideal_min <= check_m <= ideal_max:
        base = 9  # sweet spot
    elif check_m < ideal_min:
        ratio = check_m / ideal_min if ideal_min > 0 else 0
        base = max(2, int(5 + ratio * 4))
    else:
        ratio = ideal_max / check_m if check_m > 0 else 0
        base = max(3, int(5 + ratio * 4))

    # Enrichment: institutional type may reveal allocation constraints
    enrichment = ext.get("contextual_enrichment", [])
    for item in enrichment:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "institution":
            continue
        ctx = (item.get("context", "") + " " + item.get("relevance", "")).lower()
        # Endowments/foundations often have minimum allocation thresholds
        if any(t in ctx for t in ("minimum allocation", "minimum commitment",
                                   "too small for their", "allocation floor")):
            base = max(base - 1, 1)
            break

    return base


def score_relationship_proximity(lp, gp_profile):
    """1.5x — How close is the relationship? Based on CRM status, engagement signals,
    and enriched relationship references."""
    ext = lp.get("extracted", {})
    status = lp.get("structured", {}).get("status", "").lower()

    # CRM status is the strongest signal
    status_scores = {
        "closed": 10,
        "verbally committed": 9,
        "in diligence": 8,
        "qualified": 6,
        "contacted": 5,
        "lead": 4,
        "nurture": 3,
        "longlist": 2,
        "not pursuing": 1,
        "passed": 1,
    }
    score = status_scores.get(status, 4)

    # Bandwidth as modifier
    bandwidth = ext.get("bandwidth", "unknown").lower()
    if bandwidth == "high":
        score += 1
    elif bandwidth == "low":
        score -= 1

    # Timing as modifier
    timing = ext.get("timing_readiness", "unknown").lower()
    if timing == "ready_now":
        score += 1
    elif timing == "6-12_months":
        score -= 1

    # Enrichment: relationship references showing warm inbound or high trust
    enrichment_boost = 0
    enrichment = ext.get("contextual_enrichment", [])
    for item in enrichment:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "relationship":
            continue
        rel_text = (item.get("context", "") + " " + item.get("relevance", "")).lower()
        if any(t in rel_text for t in ("high trust", "warm referral", "inbound",
                                        "strong relationship", "sophisticated",
                                        "wants to work with")):
            enrichment_boost = 1
            break

    # Extreme conviction detection — near-commitment phrases in conviction
    # signals and key quotes deserve a bigger boost than generic relationship warmth
    conviction_boost = 0
    extreme_phrases = [
        "90%", "committing to us", "wants to do it", "ready to commit",
        "all but committed", "just need to", "verbally committed",
        "will commit", "going to commit", "really wants to",
    ]
    conviction_text = " ".join(ext.get("conviction_signals", [])).lower()
    conviction_text += " " + " ".join(ext.get("key_quotes", [])).lower()
    extreme_hits = sum(1 for p in extreme_phrases if p in conviction_text)
    if extreme_hits >= 2:
        conviction_boost = 2
    elif extreme_hits == 1:
        conviction_boost = 1

    score += min(enrichment_boost + conviction_boost, 3)

    # Organizational dynamics: friction in decision-making is a negative signal
    org = ext.get("organizational_dynamics", {})
    if isinstance(org, dict):
        alignment = (org.get("internal_alignment") or "").lower()
        if alignment == "friction":
            score -= 1

    return max(0, min(10, score))


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

SCORING_FUNCTIONS = {
    "intellectual_alignment": score_intellectual_alignment,
    "active_intent": score_active_intent,
    "demonstrated_behavior": score_demonstrated_behavior,
    "sector_alignment": score_sector_alignment,
    "geography_match": score_geography_match,
    "check_size_feasibility": score_check_size_feasibility,
    "relationship_proximity": score_relationship_proximity,
}


def score_lp(lp, gp_profile):
    """Score an LP on all 7 criteria. Returns dict of criterion → score (0-10)."""
    scores = {}
    for criterion, func in SCORING_FUNCTIONS.items():
        scores[criterion] = func(lp, gp_profile)
    return scores


def compute_penalties(lp, gp_profile):
    """Compute penalties for explicit negative signals in extracted data.

    Returns (total_penalty: float, penalty_details: list of (amount, reason) tuples).
    Penalties are subtracted from the weighted total AFTER computing the raw score.
    """
    ext = lp.get("extracted", {})
    penalties = []

    conflicting = (ext.get("conflicting_signals") or "").lower()
    exclusions = [e.lower() for e in ext.get("exclusions", []) if e != "unknown"]

    # --- Penalty 1: Negative language about GP's geography in conflicting_signals ---
    # Terms that indicate active skepticism about a geography as an investment market
    geo_negative_terms = [
        "fraud", "corruption", "governance", "risk of", "not compelling",
        "high valuations", "delayed dpi", "poor returns", "weak returns",
        "difficult market", "regulatory risk", "capital controls",
    ]
    if conflicting:
        geo_hits = [t for t in geo_negative_terms if t in conflicting]
        if geo_hits:
            penalties.append((-3.0, f"Negative geo signals: {', '.join(geo_hits[:3])}"))

    # --- Penalty 2: Soft exclusion overlap with GP profile ---
    # Exclusions that partially overlap GP sectors/geo/stage but weren't caught by hard filters
    gp_terms = set()
    for s in gp_profile.get("sectors", []):
        gp_terms.add(s.lower())
    if gp_profile.get("geography"):
        for g in gp_profile["geography"].split(","):
            gp_terms.add(g.strip().lower())
    if gp_profile.get("stage"):
        for t in gp_profile["stage"].split("/"):
            gp_terms.add(t.strip().lower())
    for g in gp_profile.get("broader_geo", []):
        gp_terms.add(g.strip().lower())
    if gp_profile.get("manager_type"):
        gp_terms.add(gp_profile["manager_type"].lower())

    for excl in exclusions:
        for gp_term in gp_terms:
            # Partial overlap — exclusion mentions something GP-adjacent
            if gp_term in excl or excl in gp_term:
                penalties.append((-2.0, f"Soft exclusion overlap: '{excl}' vs GP '{gp_term}'"))
                break  # one penalty per exclusion

    # --- Penalty 3: Hesitation about fund structure/approach in conflicting_signals ---
    structure_negative_terms = [
        "too small", "too early", "unproven", "first-time risk",
        "no track record", "prefer larger", "prefer later",
        "hesitant", "skeptical", "concerned about",
    ]
    if conflicting:
        structure_hits = [t for t in structure_negative_terms if t in conflicting]
        if structure_hits:
            penalties.append((-2.0, f"Structure/approach hesitation: {', '.join(structure_hits[:3])}"))

    total_penalty = sum(p[0] for p in penalties)
    return total_penalty, penalties


def compute_composite(scores, weights, lp=None, gp_profile=None):
    """Compute weighted composite score with penalties and relationship-trust bonus.

    Order: weighted sum → penalties → bonus → cap at max.
    """
    weighted_total = sum(scores[c] * weights[c] for c in weights)
    max_score = sum(10 * w for w in weights.values())

    # Penalties for explicit negative signals
    total_penalty = 0
    penalty_details = []
    if lp and gp_profile:
        total_penalty, penalty_details = compute_penalties(lp, gp_profile)

    # Sliding relationship-trust bonus — gated by minimum GP-specific fit
    phil = scores["intellectual_alignment"]
    rel = scores["relationship_proximity"]
    intent = scores["active_intent"]

    relationship_trust_bonus = 0
    bonus_skipped_reason = None

    if phil >= 7 and rel >= 7 and intent <= 3:
        sector_geo_fit = scores["sector_alignment"] + scores["geography_match"]
        demo_behavior = scores["demonstrated_behavior"]

        if sector_geo_fit < 10:
            bonus_skipped_reason = (
                f"Trust bonus skipped: sector+geo fit too low ({sector_geo_fit}/20, need >=10)"
            )
        elif demo_behavior < 5:
            bonus_skipped_reason = (
                f"Trust bonus skipped: insufficient demonstrated behavior ({demo_behavior}/10)"
            )
        else:
            trust_strength = ((phil - 6) + (rel - 6)) / 8  # 0.25 to 1.0
            max_bonus = 7
            relationship_trust_bonus = round(trust_strength * max_bonus, 1)

    final = weighted_total + total_penalty + relationship_trust_bonus
    final = max(0, min(final, max_score))  # floor at 0, cap at max

    return {
        "weighted_total": round(weighted_total, 1),
        "total": round(final, 1),
        "max": round(max_score, 1),
        "match_pct": round(final / max_score * 100, 1) if max_score > 0 else 0,
        "relationship_trust_bonus": relationship_trust_bonus,
        "bonus_skipped_reason": bonus_skipped_reason,
        "penalty": round(total_penalty, 1),
        "penalty_details": penalty_details,
    }


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------
# Usage:
#   python3 score.py
# Reads:  output/filter_results.json
# Writes: output/scored_results.json

if __name__ == "__main__":
    import json
    import os
    from config import GP_PROFILE, WEIGHTS, output_path

    input_path = output_path("filter_results.json")
    score_out = output_path("scored_results.json")

    if not os.path.exists(input_path):
        print(f"Missing {input_path}")
        print("Run 'python3 filter.py' first.")
        raise SystemExit(1)

    print(f"Loading filter results from {input_path}...\n")
    with open(input_path) as f:
        filter_data = json.load(f)

    passed = filter_data["passed"]
    rejected = filter_data["rejected"]
    print(f"Passed: {len(passed)} | Rejected: {len(rejected)}\n")

    # Score
    print("Scoring...\n")
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

    # Sort by match percentage
    scored.sort(key=lambda x: x["composite"]["match_pct"], reverse=True)

    # Print results
    print(f"{'='*70}")
    print(f"{'Rank':<5} {'LP Name':<30} {'Match%':>7} {'Conf':<7} {'Total':>7}")
    print(f"{'='*70}")

    for i, s in enumerate(scored):
        conf_str = s["confidence"]
        print(
            f"  {i+1:<3} {s['name']:<30} "
            f"{s['composite']['match_pct']:>6.1f}% "
            f"{conf_str:<8} "
            f"{s['composite']['total']:>5.1f}/{s['composite']['max']:.0f}"
        )
        # Per-criterion breakdown
        for criterion, weight in WEIGHTS.items():
            raw = s["scores"][criterion]
            weighted = raw * weight
            bar = "\u2588" * raw + "\u2591" * (10 - raw)
            name = criterion.replace("_", " ").title()
            print(f"        {name:<28} {bar} {raw:>2}/10  x{weight}  = {weighted:>5.1f}")

        # Buying profile — single most useful summary line
        bp = s["lp"].get("extracted", {}).get("investment_pattern", {})
        if isinstance(bp, dict) and bp.get("buying_profile"):
            print(f"        >> {bp['buying_profile']}")

        # Score math summary
        comp = s["composite"]
        print(f"        {'─'*50}")
        print(f"        Weighted total: {comp['weighted_total']:>6.1f}")
        if comp.get("penalty", 0) < 0:
            for amount, reason in comp.get("penalty_details", []):
                print(f"        Penalty:        {amount:>+6.1f}  ({reason})")
        bonus = comp.get("relationship_trust_bonus", 0)
        skip_reason = comp.get("bonus_skipped_reason")
        if bonus > 0:
            phil = s["scores"]["intellectual_alignment"]
            rel = s["scores"]["relationship_proximity"]
            intent = s["scores"]["active_intent"]
            print(f"        Trust bonus:    {bonus:>+6.1f}  (phil={phil}, rel={rel}, intent={intent})")
        elif skip_reason:
            print(f"        Trust bonus:    {0.0:>+6.1f}  ({skip_reason})")
        if comp.get("penalty", 0) < 0 or bonus > 0:
            print(f"        Final score:    {comp['total']:>6.1f}/{comp['max']:.0f}")

        # Flags
        for flag in s.get("negative_flags", []):
            print(f"        \u26a0 {flag}")
        for flag in s.get("info_flags", []):
            print(f"        \u2139 {flag}")

        # Display-only fields from enrichment (not scored)
        ext = s["lp"].get("extracted", {})
        observations = ext.get("note_taker_observations", [])
        if observations:
            print(f"        Internal notes: {'; '.join(observations[:3])}")
        pending = ext.get("pending_actions", [])
        if pending:
            print(f"        Next steps: {'; '.join(pending[:3])}")
        open_q = ext.get("open_questions", [])
        if open_q:
            print(f"        Open questions: {'; '.join(open_q[:3])}")
        temporal = ext.get("temporal_signals", {})
        if isinstance(temporal, dict) and temporal.get("recency") != "unknown":
            print(f"        Timing: {temporal.get('recency', '?')} (last: {temporal.get('latest_interaction_date', '?')})")
        print()

    # Print rejected for reference
    print(f"\nREJECTED ({len(rejected)}):")
    for r in rejected:
        print(f"  \u2717 {r['name']:<30} [{r['gate']}] {r['reason']}")

    # Save scored results
    os.makedirs("output", exist_ok=True)
    save_data = {
        "gp_profile": GP_PROFILE,
        "weights": WEIGHTS,
        "scored": [
            {
                "name": s["name"],
                "scores": s["scores"],
                "composite": s["composite"],
                "confidence": s["confidence"],
                "negative_flags": s["negative_flags"],
                "info_flags": s["info_flags"],
                "lp": s["lp"],
            }
            for s in scored
        ],
        "rejected": rejected,
    }
    with open(score_out, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\nSaved scored results to {score_out}")
