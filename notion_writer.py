"""
Stage 7: Write rationale results to a Notion page.

Creates a formatted report page under the parent workspace page,
matching the sample format with LP cards, score breakdowns, and methodology.

Uses the notion-client Python SDK (same as notion_reader.py).
"""

import json
import os
import re
from datetime import date
from notion_client import Client
from config import NOTION_PARENT_PAGE_ID, WEIGHTS, GP_PROFILE, output_path


def get_notion_client():
    token = os.environ.get("NOTION_API_KEY")
    if not token:
        raise RuntimeError("NOTION_API_KEY environment variable not set")
    return Client(auth=token)


# ---------------------------------------------------------------------------
# Block builder helpers
# ---------------------------------------------------------------------------

def _rt(text, bold=False, italic=False, code=False):
    """Build a rich_text element."""
    obj = {"type": "text", "text": {"content": text}}
    annotations = {}
    if bold:
        annotations["bold"] = True
    if italic:
        annotations["italic"] = True
    if code:
        annotations["code"] = True
    if annotations:
        obj["annotations"] = annotations
    return obj


def _paragraph(*rich_text_items):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": list(rich_text_items)}}


def _heading_2(text):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [_rt(text)]}}


def _heading_3(text):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [_rt(text)]}}


def _quote(*rich_text_items):
    return {"object": "block", "type": "quote",
            "quote": {"rich_text": list(rich_text_items)}}


def _divider():
    return {"object": "block", "type": "divider", "divider": {}}


def _toggle(summary_text, children):
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": [_rt(summary_text)], "children": children}}


def _table_row(cells):
    """Build a table_row block. Each cell is a list of rich_text items."""
    return {"object": "block", "type": "table_row",
            "table_row": {"cells": cells}}


def _table(rows, col_count, has_header=True):
    """Build a table block with rows."""
    return {"object": "block", "type": "table",
            "table": {
                "table_width": col_count,
                "has_column_header": has_header,
                "has_row_header": False,
                "children": rows,
            }}


def _bulleted(text):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [_rt(text)]}}


# ---------------------------------------------------------------------------
# Page content builders
# ---------------------------------------------------------------------------

def build_header_blocks(gp_profile, scored_count, rejected_count):
    """Build the header section with GP profile and pipeline metadata."""
    today = date.today().strftime("%B %d, %Y")
    return [
        _paragraph(
            _rt(f"Generated: {today}", bold=True),
            _rt(f" · Source: LP CRM excerpt · "),
        ),
        _divider(),
        _heading_2("GP opportunity profile"),
        _quote(
            _rt(f"{gp_profile.get('name', 'Fund')}", bold=True),
            _rt(f" — {gp_profile.get('fund_size', '?')} · {gp_profile.get('stage', '?')} · {gp_profile.get('manager_type', '?')}"),
        ),
        _paragraph(
            _rt("Sectors: ", bold=True),
            _rt(", ".join(gp_profile.get("sectors", []))),
        ),
        _paragraph(
            _rt("Geography: ", bold=True),
            _rt(f"{gp_profile.get('geography', '?')}, {', '.join(gp_profile.get('broader_geo', []))}"),
        ),
        _paragraph(
            _rt("Category: ", bold=True),
            _rt(gp_profile.get("lp_product_category", "?")),
        ),
        _paragraph(
            _rt("Thesis: ", bold=True),
            _rt("; ".join(gp_profile.get("key_traits", []))),
        ),
        _divider(),
    ]


def build_lp_card_blocks(lp_data):
    """Build blocks for one top-5 LP card."""
    rank = lp_data["rank"]
    name = lp_data["name"]
    pct = lp_data["match_pct"]
    conf = lp_data["confidence"]

    conf_emoji = {"high": "\U0001f7e2", "medium": "\U0001f7e1", "low": "\U0001f534"}.get(conf, "\u26aa")

    blocks = []

    # Header
    trust_note = ""
    per_crit = lp_data.get("per_criterion", {})
    # Check if trust bonus might be present (we note it if phil>=7 and rel>=7 and intent<=3)
    phil = per_crit.get("intellectual_alignment", 0)
    rel = per_crit.get("relationship_proximity", 0)
    intent = per_crit.get("active_intent", 0)
    if phil >= 7 and rel >= 7 and intent <= 3:
        trust_strength = ((phil - 6) + (rel - 6)) / 8
        bonus = round(trust_strength * 7, 1)
        if bonus > 0:
            trust_note = f" · +{bonus} trust bonus"

    blocks.append(_heading_3(
        f"#{rank} · {name} — {pct}% match · {conf_emoji} {conf.title()} confidence{trust_note}"
    ))

    # Buying profile
    bp = lp_data.get("buying_profile", "")
    if bp:
        blocks.append(_paragraph(_rt(bp, italic=True)))

    # Rationale
    blocks.append(_paragraph(_rt(lp_data.get("rationale", ""))))

    # Key citation
    # Support both key_citations (new, array) and key_citation (old, string)
    citations = lp_data.get("key_citations", [])
    if not citations and lp_data.get("key_citation"):
        citations = [lp_data["key_citation"]]
    if isinstance(citations, str):
        citations = [citations]
    for cit in citations:
        blocks.append(_quote(_rt(f'"{cit}"', italic=True)))

    # Score breakdown toggle
    score_rows = [
        _table_row([
            [_rt("Criterion", bold=True)],
            [_rt("Score", bold=True)],
            [_rt("Weight", bold=True)],
            [_rt("Weighted", bold=True)],
        ])
    ]
    total_weighted = 0
    for criterion, weight in WEIGHTS.items():
        raw = per_crit.get(criterion, 0)
        weighted = round(raw * weight, 1)
        total_weighted += weighted
        display_name = criterion.replace("_", " ").title()
        score_rows.append(_table_row([
            [_rt(display_name)],
            [_rt(f"{raw}/10")],
            [_rt(f"×{weight}")],
            [_rt(str(weighted))],
        ]))
    score_rows.append(_table_row([
        [_rt("Total", bold=True)],
        [_rt("")],
        [_rt("")],
        [_rt(f"{pct} / 100", bold=True)],
    ]))

    blocks.append(_toggle("Score breakdown", [_table(score_rows, 4)]))

    # Risk flags
    for risk in lp_data.get("risk_flags", []):
        blocks.append(_paragraph(_rt("\u26a0\ufe0f Risk: ", bold=True), _rt(risk)))

    # Timing
    timing = lp_data.get("timing", "unknown")
    timing_emoji = {"ready_now": "\U0001f7e2", "delayed": "\U0001f7e1", "unknown": "\u26aa"}.get(timing, "\u26aa")
    timing_label = {"ready_now": "Ready now", "delayed": "Delayed", "unknown": "Unknown"}.get(timing, timing)
    blocks.append(_paragraph(
        _rt(f"{timing_emoji} Timing: ", bold=True),
        _rt(timing_label),
    ))

    # Open questions
    oqs = lp_data.get("open_questions", [])
    if oqs:
        blocks.append(_paragraph(_rt("\U0001f4a1 Open questions: ", bold=True), _rt("; ".join(oqs))))

    # Pending actions / next steps
    actions = lp_data.get("pending_actions", [])
    if actions:
        blocks.append(_paragraph(_rt("\U0001f4cb Next steps: ", bold=True), _rt("; ".join(actions))))

    blocks.append(_divider())
    return blocks


def build_rejected_blocks(rejected_lps):
    """Build blocks for the rejected LPs section."""
    blocks = [_heading_2("Excluded LPs — why not")]

    GATE_LABELS = {
        "geographic_exclusion": "LP geography excludes GP target",
        "fund_size_mismatch": "Fund size mismatch — LP backs much larger funds",
        "wrong_framework": "Wrong asset class framework — PE/credit thinking applied to venture",
    }

    for r in rejected_lps:
        name = r["name"]
        near_miss = r.get("near_miss", False)
        label = " (near-miss)" if near_miss else ""

        gate = r.get("gate", "")
        reason = r.get("reason", "")

        if gate in GATE_LABELS:
            heading_label = GATE_LABELS[gate]
        elif gate == "cumulative_negative":
            match = re.match(r"(\d+)", reason)
            count = match.group(1) if match else "Multiple"
            heading_label = f"{count} active negative signals"
        else:
            heading_label = reason[:80] + ("..." if len(reason) > 80 else "")

        blocks.append(_heading_3(f"{name} — \u274c {heading_label}{label}"))
        blocks.append(_paragraph(_rt(r.get("explanation", ""))))

    blocks.append(_divider())
    return blocks


def build_methodology_blocks():
    """Build the methodology section with weights table."""
    descriptions = {
        "intellectual_alignment": "Understands outlier dynamics, seeks emerging/contrarian bets, venture-native framework",
        "active_intent": "Explicitly stated interest matching GP's geography/sectors/stage",
        "demonstrated_behavior": "Past investments in similar funds, geographies, sectors — capital deployed, not discussed",
        "relationship_proximity": "Vineyard trust level — the relationship IS the distribution channel",
        "sector_alignment": "Overlap between LP's sector interests and GP's focus areas",
        "geography_match": "India or broader emerging market interest, with signal quality discount for template data",
        "check_size_feasibility": "Can write a realistic ticket into the GP's fund size",
    }

    rows = [
        _table_row([
            [_rt("Criterion", bold=True)],
            [_rt("Weight", bold=True)],
            [_rt("What it measures", bold=True)],
        ])
    ]
    for criterion, weight in WEIGHTS.items():
        display_name = criterion.replace("_", " ").title()
        desc = descriptions.get(criterion, "")
        rows.append(_table_row([
            [_rt(display_name)],
            [_rt(f"×{weight}")],
            [_rt(desc)],
        ]))

    blocks = [
        _heading_2("Methodology"),
        _table(rows, 3),
        _paragraph(
            _rt("Post-scoring modifiers: ", bold=True),
            _rt("Relationship-trust bonus (+0 to +7) for high-alignment, high-relationship, low-intent LPs. "
                "Negative signal penalties (-2 to -3) for explicit negative language. "
                "Signal source quality discount (×0.6 to ×1.0) for template vs conversation data."),
        ),
        _paragraph(
            _rt("AI vs rules-based boundary: ", bold=True),
            _rt("AI (Claude Sonnet 4): extraction and rationale generation. "
                "Rules-based (Python): hard filters (deterministic, auditable) and weighted scoring (transparent, tunable)."),
        ),
    ]
    return blocks


# ---------------------------------------------------------------------------
# Main page creation
# ---------------------------------------------------------------------------

def create_report_page(rationale_path=None, scored_path=None):
    """Create the Notion report page. Returns the page URL."""
    if rationale_path is None:
        rationale_path = output_path("rationale_results.json")
    if scored_path is None:
        scored_path = output_path("scored_results.json")

    print(f"Loading rationale results from {rationale_path}...")
    with open(rationale_path) as f:
        rationales = json.load(f)

    print(f"Loading scored results from {scored_path}...")
    with open(scored_path) as f:
        scored_data = json.load(f)

    gp_profile = scored_data.get("gp_profile", GP_PROFILE)
    top5 = rationales.get("top_5", [])
    rejected = rationales.get("rejected", [])

    # Inject gate field from scored_results into rationale rejected LPs
    scored_rejected_by_name = {r["name"]: r for r in scored_data.get("rejected", [])}
    for r in rejected:
        scored_r = scored_rejected_by_name.get(r["name"], {})
        if "gate" not in r and scored_r.get("gate"):
            r["gate"] = scored_r["gate"]
        if not r.get("reason") and scored_r.get("reason"):
            r["reason"] = scored_r["reason"]

    scored_count = len(scored_data.get("scored", []))
    rejected_count = len(scored_data.get("rejected", []))

    # Build all blocks
    all_blocks = []
    all_blocks.extend(build_header_blocks(gp_profile, scored_count, rejected_count))

    # Top 5 section
    all_blocks.append(_heading_2("Top 5 LP matches"))
    for lp_data in top5:
        all_blocks.extend(build_lp_card_blocks(lp_data))

    # Rejected section
    all_blocks.extend(build_rejected_blocks(rejected))

    # Methodology
    all_blocks.extend(build_methodology_blocks())

    # Create page
    notion = get_notion_client()
    gp_name = gp_profile.get("name", "Fund")

    print(f"\nCreating Notion page under parent {NOTION_PARENT_PAGE_ID}...")

    page = notion.pages.create(
        parent={"page_id": NOTION_PARENT_PAGE_ID},
        icon={"type": "emoji", "emoji": "\U0001f3af"},
        properties={
            "title": [_rt(f"LP Match Report — {gp_name}")]
        },
    )
    page_id = page["id"]
    page_url = page["url"]
    print(f"  Created page: {page_url}")

    # Append blocks in batches of 100 (API limit)
    print(f"  Writing {len(all_blocks)} blocks...")
    for i in range(0, len(all_blocks), 100):
        batch = all_blocks[i:i+100]
        notion.blocks.children.append(block_id=page_id, children=batch)
        print(f"    Batch {i//100 + 1}: {len(batch)} blocks")

    print(f"\n  Done! Page URL: {page_url}")
    return page_url


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------
# Usage:
#   python3 notion_writer.py
# Reads:  output/rationale_results.json, output/scored_results.json
# Writes: Notion page

if __name__ == "__main__":
    url = create_report_page()
    print(f"\n{url}")
