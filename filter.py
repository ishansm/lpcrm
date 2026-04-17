"""
Stage 3: Hard disqualification filters.

Rules-based, deterministic. No AI — just clear gates that reject LPs who
cannot possibly match the GP opportunity.

Gates:
  1. Geographic exclusion — LP explicitly excludes a geography the GP needs
  2. Fund size mismatch — LP's minimum fund AUM exceeds the GP's fund size
  3. Wrong asset class framework — PE/credit mindset applied to venture
  4. Cumulative soft disqualifiers — 3+ negative signals add up to a reject

All filters reference the GP profile dynamically. Nothing is hardcoded.
"""

import re


def parse_fund_size(size_str):
    """Parse a fund size string like '$20M' or '$1.5B' into a number in millions.
    Returns None if unparseable."""
    if not size_str or size_str == "unknown":
        return None
    s = size_str.strip().lower().replace(",", "").replace("$", "")
    # Match patterns like '20m', '1.5b', '500k', '100', 'sub-100m'
    s = re.sub(r"^sub-?", "", s)  # strip 'sub-' prefix
    m = re.match(r"([\d.]+)\s*(b|m|k)?", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or "m"
    if unit == "b":
        return num * 1000
    elif unit == "k":
        return num / 1000
    else:
        return num


def derive_gp_geographies(gp_profile):
    """Extract all geographies the GP cares about from the profile.
    Returns a set of lowercase geography terms."""
    geos = set()
    # From 'geography' field (comma-separated)
    if gp_profile.get("geography"):
        for g in gp_profile["geography"].split(","):
            geos.add(g.strip().lower())
    # From 'broader_geo' list
    for g in gp_profile.get("broader_geo", []):
        geos.add(g.strip().lower())
    return geos


def check_geographic_exclusion(lp, gp_geos):
    """Gate 1: Does the LP explicitly exclude geographies the GP needs?
    Returns (excluded: bool, reason: str)."""
    extracted = lp.get("extracted", {})
    exclusions = extracted.get("exclusions", [])

    for excl in exclusions:
        excl_lower = excl.lower()
        # Check if any GP geography is explicitly excluded
        for geo in gp_geos:
            if geo in excl_lower:
                return True, f"Excludes '{excl}' — conflicts with GP geography '{geo}'"
        # Check for broad exclusionary language
        if "emerging market" in excl_lower and any(
            g in ("emerging markets", "africa", "latin america", "sea", "india")
            for g in gp_geos
        ):
            return True, f"Excludes '{excl}' — GP targets emerging markets"

    # Also check geography_interests for "US only" type signals
    geo_interests = [g.lower() for g in extracted.get("geography_interests", [])]
    # Filter out "unknown" — if nothing remains, LP has no stated geo preference
    # which is NOT the same as "US-only". Only trigger on explicit domestic-only.
    known_geos = [g for g in geo_interests if g not in ("unknown", "")]
    if known_geos and all(
        g in ("us", "usa", "united states", "north america", "domestic")
        for g in known_geos
    ):
        # LP only invests domestically — only reject if GP has NO US focus.
        # If GP targets US alongside other geos, US-only LP is still viable.
        gp_includes_us = any(
            g in ("us", "usa", "united states", "north america", "domestic")
            for g in gp_geos
        )
        if not gp_includes_us:
            return True, f"LP geography is US-only — GP has no US focus ({', '.join(gp_geos)})"

    return False, ""


def check_fund_size_mismatch(lp, gp_fund_size_m):
    """Gate 2: Is the LP's minimum fund AUM larger than the GP's fund size?
    Returns (mismatched: bool, reason: str).

    Checks min_fund_size first, then falls back to investment_pattern.typical_fund_size
    which reveals actual behavior (e.g., LP only invests in $200-500M funds).
    """
    if gp_fund_size_m is None:
        return False, ""

    extracted = lp.get("extracted", {})
    min_fund = extracted.get("min_fund_size", "unknown")

    # --- Primary: explicit min_fund_size field ---
    if min_fund and min_fund != "unknown":
        min_fund_lower = min_fund.strip().lower()

        # "sub-X" means they invest in funds BELOW X — this is a ceiling, not a floor
        if min_fund_lower.startswith("sub"):
            pass  # not a floor — fall through to pattern check
        else:
            min_fund_m = parse_fund_size(min_fund)
            if min_fund_m is not None and min_fund_m > gp_fund_size_m * 5:
                return True, (
                    f"LP minimum fund size ({min_fund}) far exceeds "
                    f"GP fund size (${gp_fund_size_m:.0f}M)"
                )

    # --- Fallback: investment_pattern.typical_fund_size reveals actual behavior ---
    # If LP's demonstrated pattern shows they ONLY invest in funds far larger than GP.
    # Skip if the text indicates mixed/varied sizes (words like "mixed", "varies",
    # "from X to Y", "early stage") — those suggest the LP is open to smaller funds.
    pattern = extracted.get("investment_pattern", {})
    if isinstance(pattern, dict):
        typical_size = (pattern.get("typical_fund_size") or "").lower()
        if typical_size and typical_size != "unknown":
            # Don't hard-filter on mixed/varied ranges — LP may invest across sizes
            mixed_indicators = ("mixed", "varies", "from ", "to ", "early stage",
                                "emerging", "small", "range")
            if not any(ind in typical_size for ind in mixed_indicators):
                import re
                numbers = re.findall(r"\$?([\d.]+)\s*([mbk])?", typical_size)
                if numbers:
                    vals_m = []
                    for num_str, unit in numbers:
                        v = float(num_str)
                        if unit == "b":
                            v *= 1000
                        elif unit == "k":
                            v /= 1000
                        vals_m.append(v)
                    # Use the minimum value as their floor
                    floor_m = min(vals_m)
                    if floor_m > gp_fund_size_m * 5:
                        return True, (
                            f"LP typical fund size ({pattern['typical_fund_size']}) "
                            f"far exceeds GP fund size (${gp_fund_size_m:.0f}M)"
                        )

    return False, ""


def check_wrong_framework(lp):
    """Gate 3: Is the LP applying a PE/credit framework to venture?
    Returns (wrong: bool, reason: str)."""
    extracted = lp.get("extracted", {})
    framework = extracted.get("framework_type", "unknown").lower()

    if framework in ("pe-crossover", "credit-mindset"):
        return True, (
            f"Framework type is '{framework}' — "
            f"likely applying wrong asset class thinking to venture"
        )

    return False, ""


def check_cumulative_soft_disqualifiers(lp, gp_profile):
    """Gate 4: Do 3+ ACTIVE negative signals add up to a reject?
    Returns (rejected: bool, reason: str, negative_flags: list, info_flags: list).

    Key principle: absence of data is NOT a negative signal.
    "Unknown" means "we don't know" — not "they're a bad fit."
    Only count things the LP has *actively* signaled against.
    """
    extracted = lp.get("extracted", {})

    # Active negative signals — things the LP explicitly signaled
    negative_flags = []

    # Low bandwidth is an active signal (LP said they're busy/not looking)
    if extracted.get("bandwidth") == "low":
        negative_flags.append("Low bandwidth — unlikely to engage new managers")

    # Timing explicitly delayed (not "unknown" — that's absence of data)
    timing = extracted.get("timing_readiness", "unknown")
    if timing == "6-12_months":
        negative_flags.append(f"Timing: {timing} — not ready for near-term commitment")

    # Conflicting signals present (active contradiction in their own statements)
    if extracted.get("conflicting_signals"):
        negative_flags.append(f"Conflicting signals: {extracted['conflicting_signals'][:100]}")

    # Framework is traditional allocator (softer than PE/credit, but still active)
    if extracted.get("framework_type", "").lower() == "traditional-allocator":
        negative_flags.append("Traditional allocator framework — may not suit emerging managers")

    # Investment pattern shows preference for established/later funds (Fund 2/3+)
    # when GP is a first-time fund — active mismatch signal.
    # BUT: "Fund 1-2s preferred" means they LIKE early funds — don't flag that.
    gp_manager = gp_profile.get("manager_type", "").lower()
    if "first-time" in gp_manager or "emerging" in gp_manager:
        pattern = extracted.get("investment_pattern", {})
        if isinstance(pattern, dict):
            pattern_text = " ".join(str(v) for v in pattern.values()).lower()
            # Check for early-fund-positive language first — if LP prefers Fund 1s, skip
            prefers_early = any(t in pattern_text for t in (
                "fund 1", "fund i ", "first-time", "emerging manager",
            ))
            if not prefers_early and any(t in pattern_text for t in (
                "fund 2", "fund 3", "fund ii", "fund iii",
                "established fund", "proven track",
            )):
                negative_flags.append("Prefers established funds (Fund 2/3+) — GP is first-time")

    # Check size far exceeds GP fund — active structural mismatch
    check_str = extracted.get("check_size_range", "unknown")
    if check_str and check_str != "unknown":
        import re as _re
        nums = _re.findall(r"\$?([\d.]+)\s*([mbk])?", check_str.lower())
        if nums:
            gp_fund_m = parse_fund_size(gp_profile.get("fund_size", ""))
            if gp_fund_m:
                vals = []
                for ns, unit in nums:
                    v = float(ns)
                    if unit == "b": v *= 1000
                    elif unit == "k": v /= 1000
                    vals.append(v)
                avg_check = sum(vals) / len(vals)
                if avg_check >= gp_fund_m * 0.5:
                    negative_flags.append(
                        f"Check size ({check_str}) approaches GP fund size — structural mismatch"
                    )

    # Informational flags — not disqualifying, just context
    info_flags = []

    if extracted.get("confidence_level") == "low":
        info_flags.append(
            "Low confidence — insufficient data for reliable scoring. "
            "Recommend manual research before approach."
        )

    if timing == "unknown":
        info_flags.append("Timing unknown — needs clarification")

    if not extracted.get("conviction_signals"):
        info_flags.append("No conviction signals found — may need more discovery")

    if len(negative_flags) >= 3:
        return True, f"{len(negative_flags)} active negative signals", negative_flags, info_flags

    return False, "", negative_flags, info_flags


def apply_hard_filters(profiles, gp_profile):
    """Apply all hard filters. Returns (passed, rejected) lists.

    Each rejected item is: {"lp": lp, "reason": str, "gate": str, "details": str}
    Each passed item is the lp dict (with soft_flags attached).
    """
    gp_geos = derive_gp_geographies(gp_profile)
    gp_fund_size_m = parse_fund_size(gp_profile.get("fund_size", ""))

    passed = []
    rejected = []

    for lp in profiles:
        name = lp["name"]

        # Gate 1: Geographic exclusion
        excluded, reason = check_geographic_exclusion(lp, gp_geos)
        if excluded:
            rejected.append({
                "lp": lp, "reason": reason, "gate": "geographic_exclusion",
            })
            continue

        # Gate 2: Fund size mismatch
        mismatched, reason = check_fund_size_mismatch(lp, gp_fund_size_m)
        if mismatched:
            rejected.append({
                "lp": lp, "reason": reason, "gate": "fund_size_mismatch",
            })
            continue

        # Gate 3: Wrong framework
        wrong, reason = check_wrong_framework(lp)
        if wrong:
            rejected.append({
                "lp": lp, "reason": reason, "gate": "wrong_framework",
            })
            continue

        # Gate 4: Cumulative active negative signals (not absence of data)
        too_many, reason, negative_flags, info_flags = check_cumulative_soft_disqualifiers(lp, gp_profile)
        if too_many:
            rejected.append({
                "lp": lp, "reason": reason, "gate": "cumulative_negative",
                "negative_flags": negative_flags,
                "info_flags": info_flags,
            })
            continue

        # Passed — attach flags for downstream awareness
        lp["negative_flags"] = negative_flags
        lp["info_flags"] = info_flags
        passed.append(lp)

    return passed, rejected


# --- Standalone ---
# Usage:
#   python3 filter.py
# Reads:  output/extracted_profiles.json
# Writes: output/filter_results.json

if __name__ == "__main__":
    import json
    import os
    from config import GP_PROFILE, output_path

    input_path = output_path("extracted_profiles.json")
    filter_out = output_path("filter_results.json")

    if not os.path.exists(input_path):
        print(f"Missing {input_path}")
        print("Run 'python3 extract.py --all' first.")
        raise SystemExit(1)

    print(f"Loading extracted profiles from {input_path}...\n")
    with open(input_path) as f:
        profiles = json.load(f)

    # Apply filters
    print("Applying hard filters...\n")
    passed, rejected = apply_hard_filters(profiles, GP_PROFILE)

    print(f"{'='*60}")
    print(f"PASSED: {len(passed)}  |  REJECTED: {len(rejected)}")
    print(f"{'='*60}\n")

    print("REJECTED:")
    for r in rejected:
        name = r["lp"]["name"]
        gate = r["gate"]
        reason = r["reason"]
        print(f"  ✗ {name:30s} [{gate}]")
        print(f"    {reason}")
        if r.get("negative_flags"):
            for flag in r["negative_flags"]:
                print(f"      - {flag}")
        print()

    print("PASSED:")
    for lp in passed:
        conf = lp.get("extracted", {}).get("confidence_level", "?")
        neg = lp.get("negative_flags", [])
        info = lp.get("info_flags", [])
        print(f"  ✓ {lp['name']:30s} confidence={conf}")
        for flag in neg:
            print(f"      ⚠ {flag}")
        for flag in info:
            print(f"      ℹ {flag}")

    # Save for next step
    os.makedirs("output", exist_ok=True)
    save_data = {
        "passed": passed,
        "rejected": [
            {
                "name": r["lp"]["name"],
                "gate": r["gate"],
                "reason": r["reason"],
                "negative_flags": r.get("negative_flags", []),
                "lp": r["lp"],
            }
            for r in rejected
        ],
    }
    with open(filter_out, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\nSaved filter results to {filter_out}")
