"""
Natural-language chatbot over extracted LP profiles and scoring.

Loads output/extracted_profiles.json and output/scored_results.json once,
keeps a short rolling conversation, and answers questions with Claude.
Slash commands handle deterministic lookups without an API call.

Usage:
  python3 query.py
  > /help
  > tell me about GEM
  > what's his main objection?
  > /lp weizmann
  > /rank
  > /exit
"""

import copy
import json
import os
import subprocess
import sys
from datetime import date, datetime
import anthropic
from config import MODEL, ANTHROPIC_API_KEY, NOTION_PARENT_PAGE_ID, GP_PROFILE, output_path
from extract import split_merged_quotes
from notion_writer import get_notion_client, _rt, _paragraph, _heading_3, _divider
import gp as gp_mod

MAX_HISTORY_TURNS = 6  # last 6 user+assistant turns (12 messages)
FOOTER_MARKERS = ("[Source:",)


# ---------------------------------------------------------------------------
# LP summary builder — shared between slash commands and the LLM prompt
# ---------------------------------------------------------------------------

def build_lp_summary(lp, score_record=None, rejection_record=None):
    """Build a compact text summary for one LP — extracted intelligence plus
    scoring or rejection status. No raw call notes."""
    ext = lp.get("extracted", {})
    structured = lp.get("structured", {})

    lines = [f"=== {lp['name']} ==="]

    if score_record:
        rank = score_record.get("rank")
        comp = score_record.get("composite", {})
        scores = score_record.get("scores", {})
        lines.append(f"status: SCORED (rank #{rank})")
        lines.append(f"match_pct: {comp.get('match_pct')}")
        lines.append(f"confidence: {score_record.get('confidence', '?')}")
        lines.append(
            f"composite: weighted_total={comp.get('weighted_total')} "
            f"penalty={comp.get('penalty', 0)} "
            f"relationship_trust_bonus={comp.get('relationship_trust_bonus', 0)} "
            f"near_commitment_bonus={comp.get('near_commitment_bonus', 0)} "
            f"final={comp.get('total')}"
        )
        if scores:
            lines.append(f"per_criterion: {', '.join(f'{k}={v}' for k, v in scores.items())}")
        if score_record.get("negative_flags"):
            lines.append(f"negative_flags: {'; '.join(score_record['negative_flags'])}")
    elif rejection_record:
        lines.append("status: REJECTED")
        lines.append(f"gate: {rejection_record.get('gate', '?')}")
        lines.append(f"reason: {rejection_record.get('reason', '')}")
        if rejection_record.get("negative_flags"):
            lines.append(f"negative_flags: {'; '.join(rejection_record['negative_flags'])}")
    else:
        lines.append("status: UNRANKED (not in scoring output)")

    if structured.get("status"):
        lines.append(f"crm_status: {structured['status']}")
    if structured.get("check_size"):
        lines.append(f"crm_check_size: {structured['check_size']}")
    if structured.get("location"):
        lines.append(f"location: {', '.join(structured['location'])}")

    fields = [
        "sector_interests", "geography_interests", "past_investments",
        "exclusions", "key_quotes", "conviction_signals",
        "framework_type", "timing_readiness", "lp_type",
        "check_size_range", "min_fund_size", "bandwidth",
        "confidence_level", "note_taker_observations",
        "open_questions", "pending_actions",
    ]
    for f in fields:
        val = ext.get(f)
        if not val or val == "unknown":
            continue
        if isinstance(val, list):
            if not val:
                continue
            lines.append(f"{f}: {'; '.join(str(v) for v in val)}")
        else:
            lines.append(f"{f}: {val}")

    pattern = ext.get("investment_pattern", {})
    if isinstance(pattern, dict) and pattern.get("buying_profile"):
        lines.append(f"buying_profile: {pattern['buying_profile']}")

    # Decision-maker role — the authoritative "who is in charge" for this LP.
    od = ext.get("organizational_dynamics", {})
    if isinstance(od, dict):
        dm = od.get("decision_maker")
        if dm and dm != "unknown":
            lines.append(f"decision_maker: {dm}")
        dm_ev = od.get("evidence")
        if dm_ev and dm_ev != "unknown":
            lines.append(f"decision_maker_context: {dm_ev}")

    # Contextual enrichment — people, institutions, funds, terms referenced
    # in the notes. Prioritize person + institution entries; cap the rest.
    ce = ext.get("contextual_enrichment", [])
    if isinstance(ce, list) and ce:
        def _ce_rank(entry):
            t = entry.get("type") if isinstance(entry, dict) else None
            return {"person": 0, "institution": 1, "fund": 2, "term": 3}.get(t, 4)
        ordered = sorted(
            [e for e in ce if isinstance(e, dict)], key=_ce_rank,
        )
        rendered = []
        for e in ordered[:6]:
            typ = e.get("type", "?")
            ref = e.get("reference", "?")
            ctx = e.get("context", "")
            rel = e.get("relevance", "")
            tail = ctx if ctx else rel
            if ctx and rel and ctx != rel:
                tail = f"{ctx} — {rel}"
            if tail:
                rendered.append(f"  [{typ}] {ref}: {tail}")
        if rendered:
            lines.append("contextual_enrichment:")
            lines.extend(rendered)

    return "\n".join(lines)


def build_cross_gp_block(lp_name, cross_gp_record, gp_names, active_slug):
    """Format cross-GP scores for one LP. Returns empty string if fewer than
    two GPs have data — single-GP case is already covered by the main block."""
    if not cross_gp_record or len(cross_gp_record) < 2:
        return ""
    lines = ["cross_gp_scores:"]
    for slug in sorted(cross_gp_record.keys()):
        rec = cross_gp_record[slug]
        gp_name = gp_names.get(slug, slug)
        marker = " *active*" if slug == active_slug else ""
        if rec["status"] == "scored":
            s = rec.get("scores", {})
            score_str = " ".join(f"{k.split('_')[0]}={v}" for k, v in s.items())
            lines.append(
                f"  {slug} ({gp_name}){marker}: "
                f"{rec['match_pct']}% match, rank #{rec['rank']} | {score_str}"
            )
        else:
            lines.append(
                f"  {slug} ({gp_name}){marker}: REJECTED "
                f"[{rec.get('gate', '?')}] {rec.get('reason', '')}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_profiles():
    path = output_path("extracted_profiles.json")
    if not os.path.exists(path):
        print(f"Missing {path}\nRun 'python3 extract.py' first.")
        sys.exit(1)
    with open(path) as f:
        profiles = json.load(f)
    usable, skipped = [], 0
    for lp in profiles:
        ext = lp.get("extracted", {})
        if not ext or "_parse_error" in ext:
            skipped += 1
            continue
        # Normalize quotes that were merged with bullet/pipe/em-dash at
        # extraction time — older on-disk files predate the cleanup fix.
        if isinstance(ext.get("key_quotes"), list):
            ext["key_quotes"] = split_merged_quotes(ext["key_quotes"])
        usable.append(lp)
    return usable, skipped


def load_scoring():
    path = output_path("scored_results.json")
    if not os.path.exists(path):
        print(f"No scoring data found ({path}).")
        print("Run 'python3 main.py' first for ranking questions.")
        return {}, {}
    with open(path) as f:
        data = json.load(f)
    scored_by_name = {}
    for i, s in enumerate(data.get("scored", []), start=1):
        record = dict(s)
        record["rank"] = i
        scored_by_name[s["name"]] = record
    rejected_by_name = {r["name"]: r for r in data.get("rejected", [])}
    return scored_by_name, rejected_by_name


def load_cross_gp_scoring():
    """Load output/scored_results_<slug>.json for every saved GP. Returns
    (cross_gp_by_lp, gp_names) where cross_gp_by_lp[lp_name][slug] is:
      {"status": "scored", "rank": int, "match_pct": float, "scores": {...}}
      or
      {"status": "rejected", "gate": str, "reason": str}
    """
    cross = {}
    names = {}
    for slug in gp_mod.list_slugs():
        prof = gp_mod.load_profile(slug) or {}
        names[slug] = prof.get("name", slug)
        path = os.path.join("output", f"scored_results_{slug}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for i, s in enumerate(data.get("scored", []), start=1):
            cross.setdefault(s["name"], {})[slug] = {
                "status": "scored",
                "rank": i,
                "match_pct": s.get("composite", {}).get("match_pct"),
                "scores": s.get("scores", {}),
            }
        for r in data.get("rejected", []):
            cross.setdefault(r["name"], {})[slug] = {
                "status": "rejected",
                "gate": r.get("gate"),
                "reason": r.get("reason"),
            }
    return cross, names


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

HELP_TEXT = """Commands:
  /help               Show this help
  /lp <name>          Full profile for one LP (partial match, case-insensitive)
  /rank               Ranked list of passed LPs
  /rejected           Rejected LPs with gate + reason
  /match <lp_name>    Match one LP against all saved GP profiles
  /gp                 Show the active GP profile
  /gp list            List all GP profiles
  /gp switch <name>   Switch active GP (offers to re-run pipeline)
  /gp add             Add a new GP from pasted text
  /save [note]        Save the last Q&A to a Notion log page
  /reload             Reload extracted_profiles.json + scored_results.json
  /clear              Reset conversation memory
  /exit, /quit        Exit

Anything else is sent to Claude with your last 6 turns of context."""


def _find_lp(profiles, query):
    q = query.lower().strip()
    for lp in profiles:
        if lp["name"].lower() == q:
            return lp
    for lp in profiles:
        if q in lp["name"].lower():
            return lp
    return None


def cmd_lp(arg, profiles, scored_by_name, rejected_by_name):
    if not arg:
        return "Usage: /lp <name>"
    lp = _find_lp(profiles, arg)
    if not lp:
        return f"No LP matching '{arg}' in the CRM."

    summary = build_lp_summary(
        lp,
        score_record=scored_by_name.get(lp["name"]),
        rejection_record=rejected_by_name.get(lp["name"]),
    )
    # Append up to 3 key quotes for readability
    quotes = lp.get("extracted", {}).get("key_quotes", [])[:3]
    if quotes:
        summary += "\n\nkey_quotes:"
        for q in quotes:
            summary += f"\n  - \"{q}\""
    return summary


def cmd_rank(scored_by_name, profiles):
    if not scored_by_name:
        return "No scoring data loaded. Run 'python3 main.py' first."
    by_name = {lp["name"]: lp for lp in profiles}
    lines = []
    for name, rec in sorted(scored_by_name.items(), key=lambda x: x[1]["rank"]):
        rank = rec["rank"]
        pct = rec.get("composite", {}).get("match_pct", "?")
        conf = rec.get("confidence", "?")
        ext = by_name.get(name, {}).get("extracted", {})
        bp = ext.get("investment_pattern", {}).get("buying_profile", "") if isinstance(ext.get("investment_pattern"), dict) else ""
        bp_short = (bp[:80] + "...") if len(bp) > 80 else bp
        lines.append(f"  #{rank:<3}  {name:30s}  {pct}%  [{conf} confidence]  {bp_short}")
    return "\n".join(lines)


def cmd_rejected(rejected_by_name):
    if not rejected_by_name:
        return "No rejection data loaded. Run 'python3 main.py' first."
    lines = []
    for name, r in rejected_by_name.items():
        gate = r.get("gate", "?")
        reason = r.get("reason", "")
        lines.append(f"  {name:30s}  [{gate}]  {reason}")
    return "\n".join(lines)


def _match_explanations(lp, scored_matches):
    """One Claude call producing a strong-fit and weakest-link sentence for
    each matched GP. Returns {slug: {"strong": str, "weak": str}}."""
    if not scored_matches:
        return {}

    ext = lp.get("extracted", {})
    pattern = ext.get("investment_pattern", {}) or {}
    lp_blob = {
        "name": lp["name"],
        "sector_interests": ext.get("sector_interests", []),
        "geography_interests": ext.get("geography_interests", []),
        "past_investments": ext.get("past_investments", []),
        "exclusions": ext.get("exclusions", []),
        "framework_type": ext.get("framework_type", "unknown"),
        "key_quotes": ext.get("key_quotes", [])[:3],
        "conviction_signals": ext.get("conviction_signals", [])[:3],
        "buying_profile": pattern.get("buying_profile", "") if isinstance(pattern, dict) else "",
    }

    gp_blocks = []
    for m in scored_matches:
        gp = m["gp_profile"]
        gp_blocks.append({
            "slug": m["slug"],
            "name": m["gp_name"],
            "sectors": gp.get("sectors", []),
            "geography": gp.get("geography", ""),
            "stage": gp.get("stage", ""),
            "manager_type": gp.get("manager_type", ""),
            "fund_size": gp.get("fund_size", ""),
            "scores": m["scores"],
            "match_pct": m["composite"]["match_pct"],
        })

    prompt = f"""You are explaining how an LP fits or doesn't fit each GP opportunity. For each GP below, write ONE strong-fit sentence and ONE weakest-link sentence, grounded in the LP's extracted data and the specific scores.

LP:
{json.dumps(lp_blob, indent=2)}

GPs:
{json.dumps(gp_blocks, indent=2)}

Rules:
- Each sentence under 20 words.
- "strong": cite the highest or a high-scoring dimension and tie it to specific LP evidence (a past investment, geography, exclusion, quote, or buying profile).
- "weak": cite the lowest scoring dimension and explain concretely what it means given the LP's profile.
- Return ONLY a JSON object keyed by slug, each value {{"strong": "...", "weak": "..."}}. No markdown."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.split("\n") if not l.strip().startswith("```"))
        return json.loads(raw)
    except Exception:
        return {}


def cmd_match(arg, profiles):
    """Match one LP against every saved GP profile."""
    if not arg:
        return "Usage: /match <lp_name>"
    lp = _find_lp(profiles, arg)
    if not lp:
        return f"No LP matching '{arg}' in the CRM."
    ext = lp.get("extracted")
    if not ext or "_parse_error" in ext:
        return "LP has no extracted profile. Run python3 main.py first."

    # Local imports — keep module load cheap for REPL startup
    from score import score_lp, compute_composite
    from filter import apply_hard_filters
    from config import WEIGHTS

    slugs = gp_mod.list_slugs()
    if not slugs:
        return "No GP profiles found. Add one with /gp add."

    gps = []
    skipped = []
    for slug in slugs:
        try:
            prof = gp_mod.load_profile(slug)
        except Exception as e:
            skipped.append(f"{slug} (load failed: {e})")
            continue
        if prof:
            gps.append((slug, prof))
        else:
            skipped.append(slug)

    if not gps:
        return "No loadable GP profiles."

    results = []
    fell_back = []  # (slug, reason) for each GP where per-GP extraction didn't load
    for slug, gp_profile in gps:
        # Load the GP-specific extraction — extraction is GP-context-aware
        # so scoring must use the matching context. Track why we fell back
        # (if we did) so the user knows which scores are approximate.
        extract_file = os.path.join(
            "output", f"extracted_profiles_{slug}.json"
        )
        lp_copy = None
        fallback_reason = None
        if not os.path.exists(extract_file):
            fallback_reason = "no per-GP extraction file"
        else:
            try:
                with open(extract_file) as f:
                    candidates = json.load(f)
                match = next(
                    (c for c in candidates if c.get("name") == lp["name"]),
                    None,
                )
                if match is None:
                    fallback_reason = "LP not in per-GP file"
                else:
                    ext = match.get("extracted", {})
                    if not ext:
                        fallback_reason = "per-GP extraction empty"
                    elif "_parse_error" in ext:
                        fallback_reason = "per-GP extraction has parse error"
                    else:
                        lp_copy = copy.deepcopy(match)
            except (json.JSONDecodeError, OSError) as e:
                fallback_reason = f"per-GP file unreadable ({type(e).__name__})"

        if lp_copy is None:
            lp_copy = copy.deepcopy(lp)
            fell_back.append((slug, fallback_reason))

        passed, rejected = apply_hard_filters([lp_copy], gp_profile)
        if rejected:
            r = rejected[0]
            results.append({
                "slug": slug,
                "gp_name": gp_profile.get("name", slug),
                "gp_profile": gp_profile,
                "rejected": True,
                "gate": r["gate"],
                "reason": r["reason"],
            })
        else:
            scores = score_lp(lp_copy, gp_profile)
            composite = compute_composite(
                scores, WEIGHTS, lp=lp_copy, gp_profile=gp_profile,
            )
            results.append({
                "slug": slug,
                "gp_name": gp_profile.get("name", slug),
                "gp_profile": gp_profile,
                "rejected": False,
                "scores": scores,
                "composite": composite,
            })

    passed_results = sorted(
        [r for r in results if not r["rejected"]],
        key=lambda x: x["composite"]["match_pct"], reverse=True,
    )
    rejected_results = [r for r in results if r["rejected"]]
    ordered = passed_results + rejected_results

    explanations = _match_explanations(lp, passed_results)

    lines = [
        f"Matching {lp['name']} against {len(gps)} GP profile"
        f"{'s' if len(gps) != 1 else ''}:",
        "",
    ]
    for rank, r in enumerate(ordered, start=1):
        if r["rejected"]:
            lines.append(
                f"  #{rank}  {r['gp_name']} — REJECTED ({r['gate']}: {r['reason']})"
            )
            lines.append("")
            continue
        pct = r["composite"]["match_pct"]
        s = r["scores"]
        score_line = (
            f"      intellectual={s['intellectual_alignment']} "
            f"intent={s['active_intent']} "
            f"behavior={s['demonstrated_behavior']} "
            f"sector={s['sector_alignment']} "
            f"geo={s['geography_match']} "
            f"check={s['check_size_feasibility']} "
            f"relationship={s['relationship_proximity']}"
        )
        lines.append(f"  #{rank}  {r['gp_name']} — {pct}% match")
        lines.append(score_line)
        exp = explanations.get(r["slug"], {})
        if exp.get("strong"):
            lines.append(f"      Strong fit: {exp['strong']}")
        if exp.get("weak"):
            lines.append(f"      Weakest link: {exp['weak']}")
        lines.append("")

    if skipped:
        lines.append(f"  (skipped: {', '.join(skipped)})")

    # Flag GPs where we fell back to the active-GP extraction, for any reason.
    if fell_back:
        details = ", ".join(f"{slug} ({reason})" for slug, reason in fell_back)
        lines.append(
            f"  ⚠ Used active-GP extraction for: {details}. "
            f"Scores may differ from main.py output. Run 'python3 main.py' "
            f"under each affected GP for accurate scoring."
        )

    return "\n".join(lines).rstrip()


def cmd_gp(arg):
    """Handle /gp subcommands. Returns (output_text, auto_reload)."""
    parts = arg.split(maxsplit=1) if arg else []
    sub = parts[0].lower() if parts else ""
    sub_arg = parts[1].strip().strip('"\'') if len(parts) > 1 else ""

    if not sub:
        active = gp_mod.load_active()
        if not active:
            return "No active GP.", False
        prof = gp_mod.load_profile(active) or {}
        lines = [f"Active GP: {active} ({prof.get('name', '?')})"]
        for k in ("fund_size", "stage", "geography", "manager_type",
                  "lp_product_category"):
            if prof.get(k):
                lines.append(f"  {k}: {prof[k]}")
        if prof.get("sectors"):
            lines.append(f"  sectors: {', '.join(prof['sectors'])}")
        if prof.get("broader_geo"):
            lines.append(f"  broader_geo: {', '.join(prof['broader_geo'])}")
        if prof.get("key_traits"):
            lines.append("  key_traits:")
            for t in prof["key_traits"]:
                lines.append(f"    - {t}")
        return "\n".join(lines), False

    if sub == "list":
        active = gp_mod.load_active()
        slugs = gp_mod.list_slugs()
        if not slugs:
            return "No GP profiles found.", False
        lines = []
        for s in slugs:
            prof = gp_mod.load_profile(s) or {}
            marker = "* " if s == active else "  "
            lines.append(f"{marker}{s:25s}  {prof.get('name', '?')}")
        return "\n".join(lines), False

    if sub == "switch":
        if not sub_arg:
            return "Usage: /gp switch <name>", False
        prof = gp_mod.load_profile(sub_arg)
        if not prof:
            return f"No profile named '{sub_arg}'.", False
        prev = gp_mod.load_active()
        gp_mod.set_active(sub_arg)
        msg = f"Active GP is now: {sub_arg} ({prof.get('name', '?')})"
        auto_reload = False
        if prev and prev != sub_arg:
            ans = input("Run pipeline now? (y/n): ").strip().lower()
            if ans == "y":
                print("\nRunning python3 main.py ...\n")
                result = subprocess.run([sys.executable, "main.py"])
                if result.returncode == 0:
                    msg += f"\nData refreshed for {prof.get('name', sub_arg)}."
                    auto_reload = True
                else:
                    msg += (f"\nPipeline exited with code {result.returncode}. "
                            "Run /reload manually if output files were written.")
            else:
                msg += "\nReload data with /reload when pipeline is done."
        return msg, auto_reload

    if sub == "add":
        text = gp_mod.collect_paste(
            "Paste GP description, end with END on blank line.\n"
        )
        if not text:
            return "No input. Cancelled.", False
        print("\nProcessing with Claude...\n")
        try:
            profile = gp_mod.structure_with_claude(text)
        except Exception as e:
            return f"Structuring failed: {e}", False
        print("Here's the structured profile:")
        gp_mod.print_profile(profile)
        suggested = gp_mod.slugify(profile.get("name", "gp"))
        ans = input(f"\nSave this as? (suggested: {suggested}, or type your own name / 'cancel'): ").strip()
        if ans.lower() == "cancel":
            return "Cancelled.", False
        slug = gp_mod.slugify(ans) if ans else suggested
        if gp_mod.load_profile(slug):
            overwrite = input(f"'{slug}' already exists. Overwrite? (y/n): ").strip().lower()
            if overwrite != "y":
                return "Cancelled.", False
        gp_mod.save_profile(slug, profile)
        return f"Saved to gp_profiles/{slug}.json", False

    return f"Unknown /gp subcommand: {sub}. Try /help.", False


def _answer_to_blocks(answer):
    """Turn an answer string into Notion blocks. Split on blank lines into
    paragraphs; chunk any paragraph over 1900 chars to stay under Notion's
    2000-char rich_text cap."""
    blocks = []
    for para in answer.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        while len(para) > 1900:
            blocks.append(_paragraph(_rt(para[:1900])))
            para = para[1900:]
        blocks.append(_paragraph(_rt(para)))
    return blocks


def cmd_save(note, last_exchange, session_page_id):
    """Save the last Q&A to Notion. Returns (new_page_id, output_text)."""
    if not last_exchange:
        return session_page_id, "Nothing to save yet — ask a question first."

    question, answer = last_exchange
    try:
        client = get_notion_client()

        created_msg = ""
        if session_page_id is None:
            today = date.today().strftime("%B %d, %Y")
            gp_name = GP_PROFILE.get("name", "GP")
            title = f"Chatbot Log — {gp_name} — {today}"
            page = client.pages.create(
                parent={"page_id": NOTION_PARENT_PAGE_ID},
                icon={"type": "emoji", "emoji": "\U0001f4ac"},
                properties={"title": [_rt(title)]},
            )
            session_page_id = page["id"]
            created_msg = f"Created log page: {page['url']}\n"

        blocks = [_heading_3(question)]
        blocks.extend(_answer_to_blocks(answer))

        meta_parts = [
            datetime.now().strftime("%b %d %Y %H:%M"),
            f"GP: {GP_PROFILE.get('name', '?')}",
        ]
        if note:
            meta_parts.append(f"note: {note}")
        blocks.append(_paragraph(_rt(" · ".join(meta_parts), italic=True)))
        blocks.append(_divider())

        client.blocks.children.append(block_id=session_page_id, children=blocks)
        return session_page_id, created_msg + "Saved."
    except Exception as e:
        return session_page_id, f"Save failed: {e}"


def handle_slash(cmd, profiles, scored_by_name, rejected_by_name):
    """Return (output_text, should_clear_memory, should_exit, should_reload)."""
    parts = cmd.split(maxsplit=1)
    verb = parts[0].lower()
    # Strip surrounding quotes so /match "ak asset" and /lp "weizmann" work.
    arg = parts[1].strip().strip('"\'') if len(parts) > 1 else ""

    if verb == "/help":
        return HELP_TEXT, False, False, False
    if verb == "/lp":
        return cmd_lp(arg, profiles, scored_by_name, rejected_by_name), False, False, False
    if verb == "/rank":
        return cmd_rank(scored_by_name, profiles), False, False, False
    if verb == "/rejected":
        return cmd_rejected(rejected_by_name), False, False, False
    if verb == "/match":
        return cmd_match(arg, profiles), False, False, False
    if verb == "/gp":
        out, auto_reload = cmd_gp(arg)
        return out, False, False, auto_reload
    if verb == "/reload":
        return "", False, False, True
    if verb == "/clear":
        return "Memory cleared.", True, False, False
    if verb in ("/exit", "/quit"):
        return "", False, True, False
    return "Unknown command. Try /help.", False, False, False


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

def build_system_prompt(lp_summaries, gp_names=None, active_slug=None):
    joined = "\n\n".join(lp_summaries)
    gp_names = gp_names or {}
    gp_roster_lines = []
    for slug in sorted(gp_names.keys()):
        marker = " *active*" if slug == active_slug else ""
        gp_roster_lines.append(f"  - {slug} ({gp_names[slug]}){marker}")
    gp_roster = "\n".join(gp_roster_lines) if gp_roster_lines else "  (none)"

    return f"""You are an internal chatbot for a venture capital fundraising partner. The partner is reading your answer in 20 seconds before an outreach call — be concise, specific, and honest about what the data does and does not cover.

## CRITICAL: no fabrication
Every specific fact in your answer must come from the extracted LP data below. Before writing any number, fund name, team size, portfolio breakdown, or role description, verify it appears verbatim in the LP's data.

Forbidden fabrications include (but are not limited to):
- Inventing portfolio compositions ("2 public managers and 1 legacy manager") that are not in past_investments or contextual_enrichment
- Inventing team sizes ("10-12 person team") by conflating other numbers (e.g., "$10-12B AUM")
- Inventing investment counts, check sizes, or fund names not present in the data
- Synthesizing plausible-sounding details from adjacent facts

If the data doesn't contain a specific detail, say "the data doesn't specify" or omit the claim entirely. It is always better to give a shorter, factually grounded answer than a longer, partially-invented one.

When you cite a number or specific fund/person, you must be able to point to the exact field in the LP data where it came from.

## GP roster
The system has saved profiles for these GPs. Use the slug when you need a short handle; use the display name when addressing the partner:
{gp_roster}

## LP data
Each LP block starts with a status line (SCORED with rank, REJECTED with gate, or UNRANKED) for the ACTIVE GP, followed by scoring data or gate/reason, then extracted intelligence fields. If the LP has been scored against more than one GP, a `cross_gp_scores:` block appears at the end listing match %, rank, and per-criterion scores (or REJECTED + gate) for every GP.

{joined}

## Rules
- Answer ONLY from the data above. If the data doesn't cover it, say so plainly.
- Every specific claim must cite either a quote (from key_quotes / conviction_signals) or a score (match_pct or per_criterion). Vague claims are bugs.
- For ranking questions, cite actual match_pct and per_criterion scores — do not infer.
- For rejection questions, name the gate and quote the reason.
- When comparing two LPs, only list criteria where one scores STRICTLY HIGHER than the other. Ties are not wins — skip them.
- "Unknown" fields mean missing data, not flexibility. Do not treat unknown geography_interests as "versatile" — treat it as "we don't know."
- For statistical or numeric questions (median, mean, average, variance, sum, distribution), follow the "Statistics and numeric questions" section below — do not silently fall back to a distribution and call it a median.
- If an LP name in the question is not in the LP data above, say plainly "X is not in the Vineyard CRM." Do NOT invent details. Example: if the user asks about "Blackstone" and there is no Blackstone block, answer "Blackstone is not in the Vineyard CRM."
- LP profiles contain people's names in their contextual fields (e.g. "Ryan" at GEM, "Ben" at Weizmann, "Dylan" at Everblue, "Lindsay" at UVIMCO, "Marina" as a referrer). When the user asks about a person by first name, search ALL LP profiles (not just LP names) — check decision_maker, contextual_enrichment, conviction_signals, key_quotes, past_investments, and the free-text fields. If found, answer using that LP's data and name the LP they are associated with. Only say "not in the CRM" if the name doesn't appear anywhere in any profile.
- When answering "who is <person>?" questions, use this strict priority — do NOT lead with lower-priority fields just because they contain the person's name:
  1. **decision_maker + decision_maker_context** — the authoritative role/responsibility. If present, lead with this.
  2. **contextual_enrichment `[institution]` entry for the LP itself** — the institution's size, structure, mandate. Use to set the "where" (e.g. "GEM, a $10-12B OCIO").
  3. **conviction_signals + key_quotes** — use for what the person has said or committed to.
  4. **past_investments, buying_profile** — use for what the person has actually backed.
  5. **note_taker_observations** — use ONLY for internal team sentiment ("vibes well with us", "seems slow") and only if the user specifically asks about team impressions. NEVER lead a "who is X" answer with a note_taker_observations line. In particular, do not surface context-less references to people the user has no knowledge of (e.g. "he thought David was smart but his roommate is smarter") unless the user asked about that exact person.
- Example of the correct structure for "who is Ryan?": first sentence names the role from decision_maker ("Ryan leads the emerging-manager mandate at GEM"), second sentence situates the institution from contextual_enrichment ("GEM is a $10-12B OCIO"), third sentence cites a key_quote or conviction_signal for stance toward this GP.
- If the question is ambiguous (e.g. "who's interesting?"), ask for clarification instead of guessing.
- Keep the answer short. Bullets are fine. No preamble like "Based on the data".

## Precision and grounding
- Never use hedges like "likely", "probably", "seems to be", "appears to be", "all likely" for LP facts. If the data states something, state it plainly. If the data doesn't state it, say so. Hedges are a tell that you're extrapolating — stop and check whether the answer is actually grounded.
- Before describing a fund name generically, check `contextual_enrichment` for that fund. If present, cite the enrichment verbatim. Example: for Charlie Goodacre's past_investments (Concept, Giant, Whitestar, Superseed, Isomer), the enrichment block has concrete one-line descriptors for each — USE THEM. Do not paraphrase them as "likely European emerging managers" when the data says "European venture fund".
- `past_investments` can mix specific fund names (e.g. "Isomer", "Z47") with generic headcounts (e.g. "2 public managers", "1 legacy manager") and size tags (e.g. "$5M"). Render these distinctions faithfully. Do not treat "2 public managers" as if it were a named venture fund. Do not consolidate mixed entries as if they were all venture commitments.
- `sector_interests` lists topics the LP discussed or expressed interest in during the call. It is NOT proof of past sector investments. Do not say "they invest in X" when X is only in sector_interests. For actual investment evidence, cite `past_investments`, `buying_profile`, or `contextual_enrichment`. If those are empty for a sector, say interest was stated but not demonstrated.
- When the sector_alignment score is low (0-3) but sector_interests names the GP's sectors, that is the extraction flagging a mismatch between stated interest and demonstrated behavior. Do NOT reconcile by softening ("may not align perfectly"). State it as: "they flagged interest in AI/software but the score reflects a weak match with their actual buying profile."

## Directness when the data is explicit

When the extracted data explicitly states a preference or constraint, report it plainly. Do not soften it with diplomatic hedges when the data is unambiguous.

Examples of what NOT to do:
- Data says: "focuses on sub-100M funds, sub-50M preferred"
  Bad answer: "Currently focuses on sub-100M funds but has the capacity for larger commitments to the right managers."
  Good answer: "Mandate is sub-100M funds, sub-50M preferred. A $200M fund is outside their stated scope."

- Data says: "Fund 1-2s increasingly preferred"
  Bad answer: "Could scale up for Fund 2"
  Good answer: "Explicit preference for Fund 1-2s. Fund 3+ would be a mandate stretch."

- Data says: "emerging markets exclusion"
  Bad answer: "May have limited appetite for emerging markets"
  Good answer: "Explicit emerging markets exclusion."

Softening is only correct when the data itself is ambiguous or contradictory. When the data is explicit and you soften it, you are misrepresenting the LP. The partner will walk into a meeting with the wrong expectation.

Hedge words like "appears to," "may have," "could potentially," "has capacity for" are appropriate only when the underlying data supports uncertainty. When the data is clear, state what it says.

## Statistics and numeric questions
Before answering any statistical question (median, mean, average, variance, sum, distribution), classify the underlying field as NUMERIC or CATEGORICAL.
- NUMERIC fields have ordered numeric values with meaningful intervals: per_criterion scores (0-10), match_pct, and numeric ranges inside `check_size_range` (e.g. "$5M-$10M", "$20M-$30M") or `min_fund_size` ("$100M").
- CATEGORICAL fields have discrete labels with no defined numeric interval: `crm_check_size` (Small / Medium / Big / Personal), `framework_type`, `lp_type`, `timing_readiness`, `status`, `confidence`. `check_size_range` is mixed — some LPs have numeric ranges, others have bucket labels ("Medium", "Big") or descriptive text ("substantial backing").

Rules:
- A true median, mean, or variance CANNOT be computed on categorical data. If the user asks for one on a categorical field, you MUST open the answer with an explicit disclaimer like: "crm_check_size is categorical, not numeric, so a true median cannot be computed." Then give the closest useful answer: the distribution across categories and the MODE (most common). Label the mode as "mode" — never as "median". "Median falls in Medium" is wrong; "Mode = Medium" is right.
- Mode ≠ median. Median requires ordering numeric values and picking the middle. Mode is the most frequent label. Do not conflate them.
- When the user's question is scoped to SCORED LPs ("of the ranked LPs", "our top N", "the passed set", "the 11 scored LPs"), compute ONLY over the scored set. Never fold rejected LPs' numbers into a scored-LP statistic. Rejected LPs have their own blocks above; include them only if the user explicitly asks about rejected or all LPs.
- When the user's question is scoped to ALL LPs or is ambiguous about scope, state which set you used ("across all 13 LPs in the CRM" or "across the 11 scored LPs only") so the partner knows what they're looking at.
- For mixed fields like `check_size_range`, split the set before computing: LPs with numeric ranges go into the numeric calculation; LPs with bucket labels or descriptive text are listed separately. Never convert "Medium" into a dollar figure to force a numeric median.
- Correct answer shape for "median check size of the scored LPs": open with "crm_check_size is categorical, not numeric — a true median cannot be computed." Then: "Distribution across N scored LPs: X Big, Y Medium, Z Personal. Mode = Medium. LPs with numeric check_size_range: [list each with its range]."

## Cross-GP comparisons
The `cross_gp_scores:` block in each LP shows how that LP scored against every saved GP. Use it directly when the user asks about a GP that isn't the active one — do NOT say "I don't have data on that GP" if the LP has a row for that slug.

- Match the user's phrasing to a slug using the GP roster above. "India Deeptech" → india_deeptech; "US AI" or "US Infra" → us_infra; "European Biotech" or "Europe" → europe_biotech. If the phrasing is ambiguous across multiple slugs, ask which one.
- When answering "why is X ranked #1 here but #4 there?", cite both per-criterion score lines from cross_gp_scores and identify which criteria drove the delta. Example: "geo=10 vs geo=2, sector=10 vs sector=5 — the US AI mandate matches Dziugas's portfolio; India Deeptech does not."
- Extracted intelligence fields (sector_interests, quotes, observations, etc.) are loaded for the ACTIVE GP's extraction only. When reasoning about fit against a non-active GP, you may cite cross_gp_scores numbers and rejection gates freely, but be cautious using the extracted fields — they reflect the active GP's context. If a cross-GP question needs deep qualitative reasoning, say the partner should run `/match <lp>` for a GP-contextual breakdown, or switch the active GP with `/gp switch <slug>`.
- For REJECTED rows in cross_gp_scores, cite the gate and reason verbatim. Do not speculate about scores that were never computed.

## How to identify an LP's "main objection"

An LP's main objection is the most severe blocker to them committing. Rank objection severity strictly in this order:

1. **Explicit stated blockers (highest severity)** — things the LP or note-taker explicitly stated: "we don't do X", exclusions, bandwidth constraints, timing delays, internal friction. Pull from key_quotes, note_taker_observations, bandwidth field, timing_readiness field, and exclusions list.

2. **Per-criterion scores of 0-3** — serious structural mismatches that indicate the LP cannot or will not fit. Cite the criterion name and the score.

3. **Per-criterion scores of 4-5** — moderate concerns. Flag as secondary.

**Per-criterion scores of 6, 7, 8, or 9 are NEVER objections.** A score of 6+ represents a moderate-to-strong fit on that dimension. It is not a concern. It is not a hesitation. It is not an objection. Do not frame it as one. Even if 6 is the lowest score on the scorecard, if nothing is below 6, the answer is "no major per-criterion objections."

**Relationship_proximity is never an LP objection.** It is a Vineyard-side metric measuring how close WE are to the LP. A low relationship_proximity score means Vineyard hasn't built the relationship yet — it does not mean the LP is objecting to anything. Pipeline gap ≠ LP objection.

**The answer protocol:**
- If there is at least one level-1 stated blocker → lead with that
- Else if there is at least one level-2 score (0-3) → lead with that score and criterion
- Else if there is at least one level-3 score (4-5) → lead with that, framed as "moderate concern"
- Else → say "No major objections surfaced in the data. This LP looks viable across all scored dimensions."

Never lead with a score of 6+ as "the main objection." Never lead with relationship_proximity as "the main objection." Violating either of these rules is a critical bug in the answer.

## Conversation history
You have the last 6 turns of conversation history. Use it to resolve pronouns (he/she/they/his/her/their) and references like "that LP", "the top one", "him" — bind them to the LP most recently discussed. Only ask for clarification if the reference is still genuinely ambiguous after checking history.

## Footer — required on every answer
Choose the footer based on what the answer actually uses:

- [Source: scores + N LPs cited] — if per-criterion scores, match %, or score breakdowns appear in the answer
- [Source: extracted profiles — N LPs cited] — if LPs are named or quoted but no scores are used
- [Source: general reasoning, no specific LP data] — ONLY if the answer doesn't name or reference any specific LP

Count the LPs actually cited by name in the answer, not the LPs loaded in context. The footer must be the last line of your response."""


def ensure_footer(text):
    """Validate Claude included the footer; append a default if missing."""
    if any(m in text for m in FOOTER_MARKERS):
        return text
    default = "[Source: general reasoning, no specific LP data]"
    return text.rstrip() + "\n" + default


# ---------------------------------------------------------------------------
# Main REPL
# ---------------------------------------------------------------------------

def _load_all():
    """Load everything and build the system prompt. Returns a state dict."""
    profiles, skipped = load_profiles()
    scored_by_name, rejected_by_name = load_scoring()
    cross_gp_by_lp, gp_names = load_cross_gp_scoring()
    active_slug = gp_mod.load_active()
    summaries = []
    for lp in profiles:
        summary = build_lp_summary(
            lp,
            score_record=scored_by_name.get(lp["name"]),
            rejection_record=rejected_by_name.get(lp["name"]),
        )
        cross = build_cross_gp_block(
            lp["name"], cross_gp_by_lp.get(lp["name"], {}),
            gp_names, active_slug,
        )
        if cross:
            summary += "\n" + cross
        summaries.append(summary)
    return {
        "profiles": profiles,
        "skipped": skipped,
        "scored_by_name": scored_by_name,
        "rejected_by_name": rejected_by_name,
        "cross_gp_by_lp": cross_gp_by_lp,
        "gp_names": gp_names,
        "active_slug": active_slug,
        "system_prompt": build_system_prompt(summaries, gp_names, active_slug),
    }


def main():
    state = _load_all()
    if state["skipped"]:
        print(f"Skipping {state['skipped']} LPs with extraction errors.")

    print(f"Loaded {len(state['profiles'])} LP profiles "
          f"({len(state['scored_by_name'])} scored, "
          f"{len(state['rejected_by_name'])} rejected). "
          f"Type /help for commands, /exit to quit.\n")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    history = []  # list of {"role": "user"|"assistant", "content": str}
    last_exchange = None   # (question, answer) — most recent Claude turn
    session_page_id = None  # Notion log page created on first /save

    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue

        # Defensive: strip leading whitespace, stray prompt chars, or quote
        # marks that can slip in when pasting or from terminal buffer quirks.
        cleaned = question.lstrip(" >\t\"'").strip()

        if cleaned.startswith("/"):
            # Rebind so downstream handlers see the cleaned command.
            question = cleaned
            # /save is handled here because it needs session state
            parts = question.split(maxsplit=1)
            if parts[0].lower() == "/save":
                note = parts[1] if len(parts) > 1 else ""
                session_page_id, out = cmd_save(note, last_exchange, session_page_id)
                print(f"\n{out}\n")
                continue

            out, clear_mem, should_exit, should_reload = handle_slash(
                question,
                state["profiles"],
                state["scored_by_name"],
                state["rejected_by_name"],
            )
            if should_exit:
                break
            if should_reload:
                if out:
                    print(f"\n{out}")
                state = _load_all()
                print(f"\nReloaded. {len(state['profiles'])} LPs "
                      f"({len(state['scored_by_name'])} scored, "
                      f"{len(state['rejected_by_name'])} rejected).\n")
                continue
            if clear_mem:
                history = []
                last_exchange = None
            if out:
                print(f"\n{out}\n")
            continue

        # Trim history to last N turns before sending
        trimmed = history[-(MAX_HISTORY_TURNS * 2):]
        messages = trimmed + [{"role": "user", "content": question}]

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=1500,
                temperature=0,
                system=state["system_prompt"],
                messages=messages,
            )
            answer = response.content[0].text.strip()
            answer = ensure_footer(answer)
        except Exception as e:
            print(f"ERROR: {e}\n")
            continue

        print(f"\n{answer}\n")
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        last_exchange = (question, answer)


if __name__ == "__main__":
    main()
