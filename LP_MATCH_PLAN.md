# LP Match — Implementation Plan (v3 Final)

## What this is
A Python pipeline that:
1. **Pulls LP data live from Notion** via the Notion API
2. **Reads a GP opportunity profile** from a JSON file you edit
3. **Extracts, filters, scores, and ranks** LPs against that GP
4. **Writes the results back to Notion** as a formatted page — right next to the CRM

Full loop: Notion → Python → Claude → Python → Notion

---

## How input works

### LP Data → Notion API (live pull)
The script calls the Notion API to fetch all LP records from your CRM database:
- Creates a Notion integration at https://www.notion.so/my-integrations
- Share the LP CRM database with that integration
- Script fetches all LP pages, detects embedded child pages (call notes), fetches those too
- Result: list of LP objects with structured fields + concatenated call notes

### GP Profile → `gp_profile.json` (you edit this)
```json
{
  "name": "India Deeptech Fund",
  "fund_size": "$20M",
  "stage": "pre-seed/seed",
  "geography": "India, Indian founders",
  "manager_type": "first-time fund manager",
  "sectors": ["deeptech", "AI", "hardware", "defense", "bio/lifesciences"],
  "broader_geo": ["emerging markets", "Africa", "Latin America", "SEA"],
  "lp_product_category": "Category 3 — emerging, small, early-stage Indian VC",
  "key_traits": [
    "Triple-contrarian: emerging geo + emerging manager + hard tech",
    "Bottleneck at seed — few GPs can invest here",
    "Ideal LP understands outlier dynamics, not checkbox allocation"
  ]
}
```
Change this file for any GP opportunity and re-run.

---

## How output works

### Primary output → Notion page (auto-created)
The script creates a new Notion page under your workspace with the full results. The team clicks it and sees everything without leaving Notion. Contains:

**Header section:**
- GP opportunity summary (fund size, stage, geography, sectors, thesis)
- Pipeline metadata (date, source database, method)

**Top 5 ranked LPs — each LP card has:**
- Rank + match % + confidence level
- Fit rationale (2-3 sentences)
- Key citation from call notes in a quote block
- Score breakdown in a toggle block (expandable table)
- Risk flags (conflicting signals, data gaps)
- Timing indicator (ready now / delayed / unknown)

**Excluded LPs — "why not" section:**
- Name + rejection reason
- 1-2 sentence explanation with evidence
- Near-miss flag (e.g., Valence8)

**Methodology section:**
- Scoring criteria + weights table
- AI vs rules-based boundary
- Confidence scoring explanation
- Philosophical influences (Yavuzhan, Jordan, Siya, Hummingbird)

### Secondary output → `output/lp_match_report.json`
Machine-readable JSON with all scores, extracted profiles, and rationale. For debugging and inspection.

### Terminal output
Progress prints as pipeline runs: fetching → extracting → filtering → scoring → writing to Notion.

---

## How to run
```bash
# Set your API keys
export ANTHROPIC_API_KEY=sk-ant-...
export NOTION_API_KEY=ntn_...
export NOTION_DATABASE_ID=3345d6a4e0dc817eb9ece2f97e21ba0c

# Optional: set parent page ID for where the report page gets created
export NOTION_PARENT_PAGE_ID=3345d6a4e0dc807c872dd574d98c9d2d

# Run
python main.py

# Output: new Notion page created + link printed to terminal
```

---

## Project structure
```
lp-match/
├── gp_profile.json               # ← YOU EDIT THIS (GP opportunity)
├── config.py                     # API keys, scoring weights, constants
├── notion_reader.py              # Stage 1: Fetch LPs from Notion API
├── extract.py                    # Stage 2: Claude extraction pass
├── filter.py                     # Stage 3: Hard disqualification filters
├── score.py                      # Stage 4-5: Scoring + composite
├── rationale.py                  # Stage 6: Claude rationale generation
├── notion_writer.py              # Stage 7: Write results back to Notion
├── main.py                       # Orchestrator
├── output/
│   └── lp_match_report.json      # Machine-readable backup
└── README.md
```

---

## Phase 1: Notion Reader (`notion_reader.py`) (~1 hour)

Fetches all LP records from the Notion CRM database. Handles two-layer data: structured fields + embedded child page call notes.

### Dependencies
```
pip install notion-client anthropic
```

### Implementation
```python
# notion_reader.py

from notion_client import Client
import os

def get_notion_client():
    return Client(auth=os.environ["NOTION_API_KEY"])

def fetch_all_lps(database_id):
    """Fetch all LP records from CRM. Returns list of LP dicts."""
    notion = get_notion_client()

    # Query database for all pages
    results = []
    has_more = True
    start_cursor = None
    while has_more:
        response = notion.databases.query(
            database_id=database_id,
            start_cursor=start_cursor
        )
        results.extend(response["results"])
        has_more = response.get("has_more", False)
        start_cursor = response.get("next_cursor")

    # Extract structured fields + call notes for each LP
    lps = []
    for page in results:
        lp = extract_lp_from_page(notion, page)
        lps.append(lp)
        print(f"  Fetched: {lp['name']} ({len(lp['call_notes'])} chars)")
    return lps

def extract_lp_from_page(notion, page):
    """Extract structured fields and call notes from one LP page."""
    props = page["properties"]
    structured = {
        "status": get_select(props.get("Status")),
        "check_size": get_select(props.get("Check Size")),
        "location": get_multi_select(props.get("Location")),
        "email": get_email(props.get("Email")),
    }
    name = get_title(props.get("Name"))

    # Fetch page content blocks
    blocks = get_all_blocks(notion, page["id"])
    call_notes = blocks_to_text(blocks)

    # Fetch embedded child pages (nested call notes)
    child_pages = find_child_pages(blocks)
    for child_id in child_pages:
        child_blocks = get_all_blocks(notion, child_id)
        child_text = blocks_to_text(child_blocks)
        call_notes += "\n\n--- Embedded call notes ---\n" + child_text

    return {
        "id": page["id"],
        "name": name,
        "structured": structured,
        "call_notes": call_notes
    }

def get_all_blocks(notion, block_id):
    """Fetch all child blocks with pagination."""
    blocks = []
    has_more = True
    start_cursor = None
    while has_more:
        response = notion.blocks.children.list(
            block_id=block_id, start_cursor=start_cursor
        )
        blocks.extend(response["results"])
        has_more = response.get("has_more", False)
        start_cursor = response.get("next_cursor")
    return blocks

def blocks_to_text(blocks):
    """Convert Notion blocks to plain text."""
    lines = []
    for block in blocks:
        btype = block["type"]
        if btype in ("paragraph", "bulleted_list_item", "numbered_list_item",
                      "heading_1", "heading_2", "heading_3", "toggle"):
            rich_text = block[btype].get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text.strip():
                lines.append(text)
    return "\n".join(lines)

def find_child_pages(blocks):
    """Find IDs of child pages embedded in content."""
    return [b["id"] for b in blocks if b["type"] == "child_page"]

# Property helpers
def get_title(prop):
    if not prop or not prop.get("title"): return ""
    return "".join(t.get("plain_text", "") for t in prop["title"])

def get_select(prop):
    if not prop or not prop.get("select"): return None
    return prop["select"].get("name")

def get_multi_select(prop):
    if not prop or not prop.get("multi_select"): return []
    return [ms["name"] for ms in prop["multi_select"]]

def get_email(prop):
    if not prop or not prop.get("email"): return ""
    return prop["email"] or ""
```

---

## Phase 2: Config (`config.py`) (~10 min)

```python
import os, json

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID")

MODEL = "claude-sonnet-4-20250514"

with open("gp_profile.json") as f:
    GP_PROFILE = json.load(f)

WEIGHTS = {
    "philosophical_alignment": 2.0,
    "active_intent": 1.8,
    "demonstrated_behavior": 1.5,
    "sector_alignment": 1.3,
    "geography_match": 1.3,
    "check_size_feasibility": 1.1,
    "relationship_proximity": 1.0
}

MAX_SCORE = sum(10 * w for w in WEIGHTS.values())
```

---

## Phase 3: Claude Extraction (`extract.py`) (~1.5 hours)

One Claude API call per LP. Raw Notion data → structured JSON profile.

### Extraction prompt must:
1. Receive GP profile as context
2. Distinguish explicitly stated vs. inferred vs. absent
3. Detect wrong asset class frameworks (Yavuzhan's trap)
4. Output confidence level (high/medium/low) based on data richness
5. Flag direct quotes for citation
6. Identify conflicting signals

### Output schema per LP:
```json
{
  "sector_interests": [],
  "geography_interests": [],
  "fund_stage_pref": "",
  "check_size_range": "",
  "min_fund_size": "",
  "past_investments": [],
  "exclusions": [],
  "lp_type": "",
  "involvement_style": "",
  "bandwidth": "",
  "framework_type": "",
  "risk_tolerance": "",
  "power_law_literacy": "",
  "india_product_awareness": "",
  "timing_readiness": "",
  "confidence_level": "high|medium|low",
  "conflicting_signals": "",
  "conviction_signals": [],
  "key_quotes": []
}
```

---

## Phase 4: Hard Filters (`filter.py`) (~30 min)

Rules-based, deterministic. Returns (passed, rejected) with reasons.

### Gates:
1. Geographic exclusion ("US only" / "no emerging markets")
2. Fund size mismatch (min fund AUM > $20M)
3. Wrong asset class framework (PE/credit mindset applied to venture)
4. Cumulative soft disqualifiers (3+ negatives)

Expected rejections: Valence8, YMCA, Ak Asset, Jordan Park, Rezayat (low confidence).

---

## Phase 5: Scoring (`score.py`) (~45 min)

7 criteria, 0-10 each, weighted 2.0x-1.0x.

| Criterion | Weight | Measures |
|-----------|--------|----------|
| Philosophical alignment | 2.0x | Understands outlier dynamics, seeks contrarian bets |
| Active intent | 1.8x | Explicitly stated India/deeptech/EM interest |
| Demonstrated behavior | 1.5x | Past investments in similar funds/geos |
| Sector alignment | 1.3x | Deeptech/AI/hardware/defense/bio overlap |
| Geography match | 1.3x | India or EM interest |
| Check size feasibility | 1.1x | Can write $500K-$3M into $20M fund |
| Relationship proximity | 1.0x | Vineyard trust level |

---

## Phase 6: Rationale Generation (`rationale.py`) (~30 min)

Claude generates structured JSON (not free text) so the Notion writer can format it:

```json
{
  "top_5": [
    {
      "rank": 1,
      "name": "GEM",
      "match_pct": 82.3,
      "confidence": "high",
      "rationale": "GEM's Ryan is actively searching...",
      "key_citation": "we mostly met to talk about India...",
      "risk_flags": ["Big check size may exceed..."],
      "timing": "ready_now",
      "per_criterion": {"philosophical_alignment": 8, ...}
    }
  ],
  "rejected": [
    {
      "name": "Valence8",
      "reason": "Geographic exclusion",
      "explanation": "Philosophically ideal but...",
      "near_miss": true
    }
  ]
}
```

---

## Phase 7: Notion Writer (`notion_writer.py`) (~1 hour)

Creates the output page in Notion using the Notion API. This is the mirror of `notion_reader.py` — one reads, one writes.

### Implementation
```python
# notion_writer.py

from notion_client import Client
import os
from datetime import date

def write_results_to_notion(results, gp_profile, weights, parent_page_id):
    """
    Create a new Notion page with the ranked LP match results.
    Returns the URL of the created page.
    """
    notion = Client(auth=os.environ["NOTION_API_KEY"])

    # Build the page content as Notion-compatible markdown
    content = build_report_content(results, gp_profile, weights)

    # Create the page
    page_title = f"LP Match Report — {gp_profile['name']}"
    
    # Use Notion API to create page
    response = notion.pages.create(
        parent={"page_id": parent_page_id},
        icon={"type": "emoji", "emoji": "🎯"},
        properties={
            "title": [{"text": {"content": page_title}}]
        },
        # Content is added via blocks API after page creation
    )
    
    page_id = response["id"]
    
    # Add content blocks to the page
    # (Notion API requires adding blocks separately after page creation)
    blocks = build_content_blocks(results, gp_profile, weights)
    
    # Notion API limits to 100 blocks per request, so batch if needed
    for i in range(0, len(blocks), 100):
        batch = blocks[i:i+100]
        notion.blocks.children.append(block_id=page_id, children=batch)
    
    page_url = response["url"]
    print(f"Report created: {page_url}")
    return page_url


def build_content_blocks(results, gp_profile, weights):
    """
    Build Notion API block objects for the report content.
    Returns a list of block dicts.
    """
    blocks = []
    
    # --- HEADER ---
    blocks.append(paragraph_block(
        f"Generated: {date.today().isoformat()} · "
        f"Source: LP CRM · "
        f"Pipeline: Notion API → Claude Extraction → Scoring → Notion Output"
    ))
    blocks.append(divider_block())
    
    # --- GP PROFILE ---
    blocks.append(heading_block("GP opportunity profile", level=2))
    blocks.append(callout_block(
        f"{gp_profile['name']} — {gp_profile['fund_size']} · "
        f"{gp_profile['stage']} · {gp_profile['manager_type']}\n"
        f"Sectors: {', '.join(gp_profile['sectors'])}\n"
        f"Geography: {gp_profile['geography']}",
        emoji="🎯"
    ))
    blocks.append(divider_block())
    
    # --- TOP 5 ---
    blocks.append(heading_block("Top 5 LP matches", level=2))
    
    for lp in results["top_5"]:
        # LP heading
        conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
            lp["confidence"], "⚪"
        )
        blocks.append(heading_block(
            f"#{lp['rank']} · {lp['name']} — {lp['match_pct']}% match · "
            f"{conf_emoji} {lp['confidence']} confidence",
            level=3
        ))
        
        # Rationale
        blocks.append(paragraph_block(lp["rationale"]))
        
        # Citation
        blocks.append(quote_block(lp["key_citation"]))
        
        # Score breakdown (in a toggle)
        score_text = format_score_table(lp["per_criterion"], weights)
        blocks.append(toggle_block("Score breakdown", score_text))
        
        # Risk flags
        if lp.get("risk_flags"):
            for flag in lp["risk_flags"]:
                blocks.append(callout_block(f"Risk: {flag}", emoji="⚠️"))
        
        # Timing
        timing_emoji = {"ready_now": "🟢", "delayed": "🟡", "unknown": "⚪"}.get(
            lp.get("timing", "unknown"), "⚪"
        )
        blocks.append(paragraph_block(
            f"{timing_emoji} Timing: {lp.get('timing', 'Unknown')}"
        ))
        blocks.append(divider_block())
    
    # --- EXCLUDED ---
    blocks.append(heading_block("Excluded LPs — why not", level=2))
    
    for lp in results["rejected"]:
        near_miss = " (near-miss)" if lp.get("near_miss") else ""
        blocks.append(callout_block(
            f"{lp['name']} — {lp['reason']}{near_miss}\n\n"
            f"{lp['explanation']}",
            emoji="❌"
        ))
    
    blocks.append(divider_block())
    
    # --- METHODOLOGY ---
    blocks.append(heading_block("Methodology", level=2))
    blocks.append(table_block(weights))
    blocks.append(paragraph_block(
        "AI boundary: Claude for extraction (messy NL → structured) and "
        "rationale generation. Rules for filtering (deterministic) and "
        "scoring (transparent, tunable)."
    ))
    blocks.append(paragraph_block(
        "Informed by: Yavuzhan's Allocator's Manifesto, Jordan's LP Guide "
        "to Venture Funds, Siya's India LP Framework, Hummingbird profile."
    ))
    
    return blocks


# --- Block builder helpers ---

def heading_block(text, level=2):
    return {
        "type": f"heading_{level}",
        f"heading_{level}": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }
    }

def paragraph_block(text):
    return {
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }
    }

def quote_block(text):
    return {
        "type": "quote",
        "quote": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }
    }

def callout_block(text, emoji="💡"):
    return {
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": emoji},
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }
    }

def divider_block():
    return {"type": "divider", "divider": {}}

def toggle_block(title, content_text):
    return {
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": title}}],
            "children": [paragraph_block(content_text)]
        }
    }

def table_block(weights):
    # Simplified: render as a paragraph with formatted text
    # Full table blocks are more complex in Notion API
    lines = ["Criterion | Weight"]
    lines.append("---|---")
    for k, v in weights.items():
        name = k.replace("_", " ").title()
        lines.append(f"{name} | ×{v}")
    return paragraph_block("\n".join(lines))

def format_score_table(scores, weights):
    lines = []
    total = 0
    for criterion, weight in weights.items():
        score = scores.get(criterion, 0)
        weighted = score * weight
        total += weighted
        bar = "█" * score + "░" * (10 - score)
        name = criterion.replace("_", " ").title()
        lines.append(f"{name}: {bar} {score}/10 (×{weight} = {weighted:.1f})")
    lines.append(f"Total: {total:.1f} / {sum(10*w for w in weights.values())}")
    return "\n".join(lines)
```

---

## Phase 8: Orchestrator (`main.py`) (~20 min)

```python
#!/usr/bin/env python3
"""LP Match — Vineyard LP-GP Matching System"""

import json
from config import (GP_PROFILE, WEIGHTS, MAX_SCORE, 
                     ANTHROPIC_API_KEY, NOTION_DATABASE_ID, NOTION_PARENT_PAGE_ID)
from notion_reader import fetch_all_lps
from extract import extract_all
from filter import apply_hard_filters
from score import score_lp, compute_composite
from rationale import generate_rationale
from notion_writer import write_results_to_notion
import anthropic

def main():
    print("=" * 50)
    print("LP MATCH — Vineyard LP-GP Matching System")
    print("=" * 50)
    print(f"\nGP: {GP_PROFILE['name']}")
    print(f"Fund: {GP_PROFILE['fund_size']} · {GP_PROFILE['stage']}")
    print(f"Sectors: {', '.join(GP_PROFILE['sectors'])}\n")

    # 1. Fetch from Notion
    print("STAGE 1: Fetching LPs from Notion...")
    lp_records = fetch_all_lps(NOTION_DATABASE_ID)
    print(f"→ Fetched {len(lp_records)} LPs\n")

    # 2. Extract via Claude
    print("STAGE 2: Extracting structured profiles via Claude...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    profiles = extract_all(client, lp_records, GP_PROFILE)
    print(f"→ Extracted {len(profiles)} profiles\n")

    # 3. Hard filters
    print("STAGE 3: Applying hard filters...")
    passed, rejected = apply_hard_filters(profiles)
    print(f"→ Passed: {len(passed)} | Rejected: {len(rejected)}")
    for r in rejected:
        print(f"  ✗ {r['lp']['name']}: {r['reason']}")
    print()

    # 4. Score
    print("STAGE 4: Scoring...")
    scored = []
    for lp in passed:
        scores = score_lp(lp, GP_PROFILE)
        composite = compute_composite(scores, WEIGHTS)
        scored.append({"lp": lp, "scores": scores, "composite": composite})
    scored.sort(key=lambda x: x["composite"]["match_pct"], reverse=True)
    for s in scored:
        conf = s["lp"]["extracted"].get("confidence_level", "?")
        print(f"  {s['lp']['name']}: {s['composite']['match_pct']}% ({conf})")
    print()

    # 5. Rationale
    top_5 = scored[:5]
    print("STAGE 5: Generating rationale via Claude...")
    rationale_data = generate_rationale(client, top_5, rejected, GP_PROFILE)

    # 6. Write to Notion
    print("STAGE 6: Writing results to Notion...")
    results = {
        "top_5": rationale_data["top_5"],
        "rejected": rationale_data["rejected"],
        "all_scored": scored
    }
    page_url = write_results_to_notion(
        results, GP_PROFILE, WEIGHTS, NOTION_PARENT_PAGE_ID
    )

    # 7. Save JSON backup
    with open("output/lp_match_report.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\n{'=' * 50}")
    print(f"DONE — Report: {page_url}")
    print(f"JSON backup: output/lp_match_report.json")
    print(f"{'=' * 50}")

if __name__ == "__main__":
    main()
```

---

## Notion setup (before coding)

1. Go to https://www.notion.so/my-integrations
2. Create a new integration named "LP Match"
3. Copy the token (starts with `ntn_`)
4. Go to your LP CRM database in Notion → "..." → Connections → Add "LP Match"
5. Also add the integration to the parent page where you want reports created

---

## Build order for Claude Code

| Step | File | Time | Test |
|------|------|------|------|
| 1 | `gp_profile.json` | 5 min | Pre-fill with given GP |
| 2 | `config.py` | 10 min | Import, print GP_PROFILE |
| 3 | `notion_reader.py` | 60 min | Run standalone, print 15 LP names + note lengths |
| 4 | `extract.py` | 90 min | Run on 2-3 LPs, inspect JSON |
| 5 | `filter.py` | 30 min | Verify rejections |
| 6 | `score.py` | 45 min | Print sorted scores |
| 7 | `rationale.py` | 30 min | Inspect structured output |
| 8 | `notion_writer.py` | 60 min | Create test page, verify formatting |
| 9 | `main.py` | 20 min | Full end-to-end |
| 10 | `README.md` | 10 min | Setup + run instructions |

**Total: ~6 hours**

---

## Claude Code initial prompt

> I'm building LP Match — a Python pipeline that reads LP data from a Notion CRM via the Notion API, extracts intelligence using Claude API, scores LPs against a GP opportunity from gp_profile.json, and writes a formatted report back to Notion as a new page. My full plan is in LP_MATCH_PLAN.md — read it completely. My Notion database ID is 3345d6a4e0dc817eb9ece2f97e21ba0c and the parent page for output is 3345d6a4e0dc807c872dd574d98c9d2d. Let's start with Step 1: gp_profile.json, then config.py, then the Notion reader. Build each file, test it, then move to the next.

---

## Demo flow (15 min)

1. **Show `gp_profile.json`** — "I define the GP here. Change this for any opportunity."
2. **Run `python main.py`** — terminal shows live Notion fetch → Claude extraction → filter results → scores → "Writing to Notion..."
3. **Open the Notion page** — walk through each section. It's right next to the CRM they already use.
4. **Click a toggle** — show score breakdown for #1 LP
5. **Show #1's citation** — open the actual LP record in Notion, find the same quote in the call notes
6. **Show the Valence8 near-miss** — "This is the best LP we can't approach"
7. **Live weight adjustment** — change config, re-run, new Notion page created with different rankings
8. **Close with methodology section** — explain AI vs rules, cite team's writing