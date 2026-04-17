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

import json
import os
import subprocess
import sys
from datetime import date, datetime
import anthropic
from config import MODEL, ANTHROPIC_API_KEY, NOTION_PARENT_PAGE_ID, GP_PROFILE, output_path
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
        lines.append(f"  #{rank}  {name:30s}  {pct}%  [{conf} confidence]  {bp_short}")
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
    for slug, gp_profile in gps:
        # Shallow copy so filter's flag mutations don't carry across iterations
        lp_copy = dict(lp)
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

    return "\n".join(lines).rstrip()


def cmd_gp(arg):
    """Handle /gp subcommands. Returns (output_text, auto_reload)."""
    parts = arg.split(maxsplit=1) if arg else []
    sub = parts[0].lower() if parts else ""
    sub_arg = parts[1].strip() if len(parts) > 1 else ""

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
    arg = parts[1] if len(parts) > 1 else ""

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

def build_system_prompt(lp_summaries, scoring_available):
    joined = "\n\n".join(lp_summaries)

    return f"""You are an internal chatbot for a venture capital fundraising partner. The partner is reading your answer in 20 seconds before an outreach call — be concise, specific, and honest about what the data does and does not cover.

## LP data
Each LP block starts with a status line (SCORED with rank, REJECTED with gate, or UNRANKED), followed by scoring data (if scored) or gate/reason (if rejected), then extracted intelligence fields.

{joined}

## Rules
- Answer ONLY from the data above. If the data doesn't cover it, say so plainly.
- Every specific claim must cite either a quote (from key_quotes / conviction_signals) or a score (match_pct or per_criterion). Vague claims are bugs.
- For ranking questions, cite actual match_pct and per_criterion scores — do not infer.
- For rejection questions, name the gate and quote the reason.
- When comparing two LPs, only list criteria where one scores STRICTLY HIGHER than the other. Ties are not wins — skip them.
- "Unknown" fields mean missing data, not flexibility. Do not treat unknown geography_interests as "versatile" — treat it as "we don't know."
- For numeric questions (median, average, sum) where raw dollar amounts are not in the data, fall back to category distribution (e.g. "3 Medium, 4 Big, 2 Small from crm_check_size") instead of refusing.
- If an LP name in the question is not in the LP data above, say plainly "X is not in the Vineyard CRM." Do NOT invent details. Example: if the user asks about "Blackstone" and there is no Blackstone block, answer "Blackstone is not in the Vineyard CRM."
- LP profiles contain people's names in their contextual fields (e.g. "Ryan" at GEM, "Ben" at Weizmann, "Dylan" at Everblue, "Lindsay" at UVIMCO, "Marina" as a referrer). When the user asks about a person by first name, search ALL LP profiles (not just LP names) — check key_quotes, conviction_signals, note_taker_observations, and every other extracted field. If found, answer using that LP's data and name the LP they are associated with. Only say "not in the CRM" if the name doesn't appear anywhere in any profile.
- If the question is ambiguous (e.g. "who's interesting?"), ask for clarification instead of guessing.
- Keep the answer short. Bullets are fine. No preamble like "Based on the data".

## Reasoning about objections, risks, and concerns
When the user asks about objections, risks, hesitations, concerns, or "what would make them say no" for a specific LP, do NOT limit yourself to explicitly stated objections in the extracted profile. The strongest objections are often revealed by LOW SCORES against the current GP, which indicate weak fit that the LP hasn't explicitly articulated. The extraction is GP-agnostic — the scores are what encode GP-specific fit.

Score thresholds:
- Per-criterion scores of 0-3 represent serious weaknesses and are legitimate objections to name.
- Scores of 4-5 are moderate concerns worth flagging.
- Scores of 6+ are not objections unless the user specifically asks about minor concerns.

Combine three sources of objection evidence, in order of priority:
1. Low scores against the active GP — e.g. "Geography match 2/10 — no LatAm exposure and no stated LatAm interest".
2. Extracted risk flags, negative_flags, and conflicting_signals — e.g. "Low bandwidth — unlikely to engage new managers".
3. Explicit stated exclusions or concerns in quotes — e.g. "They said 'we don't do first-time managers'".

Always cite the score number AND the criterion name when basing an objection on a low score. Example: "Main objection: geography fit. GEM scored 2/10 on geography_match for this GP — portfolio is US/Europe/India, no LatAm exposure."

Only say "no major objections surfaced in the data" when ALL per-criterion scores are 7+ AND there are no stated exclusions or negative flags. This should be rare.

## Conversation history
You have the last 6 turns of conversation history. Use it to resolve pronouns (he/she/they/his/her/their) and references like "that LP", "the top one", "him" — bind them to the LP most recently discussed. Only ask for clarification if the reference is still genuinely ambiguous after checking history.

## Footer — required on every answer
Choose the footer based on what the answer actually uses:

- [Source: scores + N LPs cited] — if per-criterion scores, match %, or score breakdowns appear in the answer
- [Source: extracted profiles — N LPs cited] — if LPs are named or quoted but no scores are used
- [Source: general reasoning, no specific LP data] — ONLY if the answer doesn't name or reference any specific LP

Count the LPs actually cited by name in the answer, not the LPs loaded in context. The footer must be the last line of your response."""


def ensure_footer(text, scoring_available):
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
    summaries = [
        build_lp_summary(
            lp,
            score_record=scored_by_name.get(lp["name"]),
            rejection_record=rejected_by_name.get(lp["name"]),
        )
        for lp in profiles
    ]
    return {
        "profiles": profiles,
        "skipped": skipped,
        "scored_by_name": scored_by_name,
        "rejected_by_name": rejected_by_name,
        "system_prompt": build_system_prompt(summaries, bool(scored_by_name)),
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

        if question.startswith("/"):
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
            answer = ensure_footer(answer, bool(state["scored_by_name"]))
        except Exception as e:
            print(f"ERROR: {e}\n")
            continue

        print(f"\n{answer}\n")
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        last_exchange = (question, answer)


if __name__ == "__main__":
    main()
