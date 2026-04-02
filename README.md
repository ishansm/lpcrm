# LP Match

A Python pipeline that matches Limited Partners (LPs) to GP fund opportunities using AI-powered extraction and deterministic rules-based scoring. It reads LP records from a Notion CRM — structured fields and unstructured call notes — extracts intelligence with Claude, scores each LP across seven weighted criteria, generates partner-ready rationales, and writes a formatted report back to Notion. The system is GP-agnostic: change the GP profile JSON and re-run to match any fund.

## How it works

The pipeline runs six stages in sequence:

1. **Notion Reader** — Fetches LP records from a Notion database, including structured CRM fields (status, check size, location) and unstructured page content (call notes, meeting notes, embedded child pages).
2. **Claude Extraction** — Sends each LP's raw data to Claude with the GP profile as context. Returns a structured JSON profile with 22+ fields: core preferences, contextual enrichment of every reference (funds, people, institutions, terms), investment pattern synthesis, signal source quality assessment, decision process, organizational dynamics, and competitive positioning.
3. **Hard Filters** — Four deterministic gates that reject LPs with hard disqualifiers: geographic exclusion, fund size mismatch, wrong asset class framework (PE/credit mindset), or 3+ cumulative negative signals. Absence of data is never treated as negative.
4. **Weighted Scoring** — Seven criteria scored 0-10 each, weighted and summed to 100. Fully deterministic — no AI. Includes post-scoring modifiers for relationship-trust bonus, negative signal penalties, and signal source quality discounts.
5. **Rationale Generation** — Sends the top-5 scored LPs and all rejected LPs to Claude for human-readable rationales written as 10-minute outreach prep briefs.
6. **Notion Writer** — Creates a formatted Notion page with LP cards (scores, rationales, citations, next steps), rejected LP explanations, and a methodology section.

**AI vs rules boundary:** AI handles the subjective work (interpreting messy notes, writing briefs). Rules handle the objective work (filtering, scoring). Every score is traceable to specific inputs — no black-box AI scoring.

## Setup

### Environment variables

Copy `.env.example` to `.env` and fill in:

```
ANTHROPIC_API_KEY=sk-ant-...
NOTION_API_KEY=ntn_...
NOTION_DATABASE_ID=your_crm_database_id
NOTION_PARENT_PAGE_ID=your_parent_page_id
```

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key for extraction and rationale generation |
| `NOTION_API_KEY` | Notion integration token with read/write access to CRM database and parent page |
| `NOTION_DATABASE_ID` | Notion database containing LP records |
| `NOTION_PARENT_PAGE_ID` | Notion page where the report will be created as a child |

### Dependencies

```bash
pip install anthropic notion-client python-dotenv
```

### Notion integration

1. Create a Notion integration at https://www.notion.so/my-integrations.
2. Grant it access to the LP database and the parent page for report output.
3. Copy the integration token to `NOTION_API_KEY` in `.env`.

## Usage

### Full pipeline

```bash
python3 main.py
```

Runs all six stages end-to-end. Prints progress at each stage and outputs a Notion report page URL at the end. Takes 3-5 minutes depending on LP count (one Claude API call per LP for extraction, one call total for rationale generation).

### Individual stages

Each stage is independently runnable and reads/writes JSON to `output/`:

```bash
python3 extract.py                # Extract all LPs (supports resume after interruption)
python3 extract.py "LP Name"      # Extract single LP (print only, no save)
python3 filter.py                 # Apply hard disqualification filters
python3 score.py                  # Score and rank passed LPs
python3 rationale.py              # Generate partner-ready rationales
python3 notion_writer.py          # Write report to Notion
```

Extraction supports **resume**: if interrupted by rate limits or errors, re-run and it skips already-extracted LPs.

### Changing the GP profile

1. Edit `gp_profile.json` with the new fund's details (name, size, stage, geography, sectors, manager type, key traits).
2. Delete `output/extracted_profiles.json` to force re-extraction (extraction is GP-context-aware).
3. Run `python3 main.py`.

All scoring functions, filters, and prompts reference the GP profile dynamically. Nothing is hardcoded to a specific fund.

## Project structure

```
config.py              Configuration: API keys, model selection, scoring weights
gp_profile.json        GP opportunity definition (edit this per fund)
notion_reader.py       Stage 1: Fetch LP records from Notion CRM
extract.py             Stage 2: Claude extraction with enrichment and quality assessment
filter.py              Stage 3: Hard disqualification filters (deterministic)
score.py               Stage 4: Weighted scoring engine (deterministic, no AI)
rationale.py           Stage 5: Claude rationale generation (outreach prep briefs)
notion_writer.py       Stage 6: Formatted report page written to Notion
main.py                Full pipeline orchestration
.env.example           Template for environment variables
.gitignore             Excludes output data, .env, caches
output/                JSON artifacts from each stage (gitignored)
```

## Scoring methodology

### Seven criteria (0-10 each, max 100)

| Criterion | Weight | What it measures |
|---|---|---|
| Intellectual alignment | x1.75 | Venture-native thinking, emerging manager affinity, contrarian conviction |
| Demonstrated behavior | x1.5 | Past investments in similar funds, geographies, stages — actions over words |
| Relationship proximity | x1.65 | Trust depth, engagement level, CRM status, conviction signals |
| Active intent | x1.4 | Explicitly stated interest matching GP geography, sectors, or stage |
| Sector alignment | x1.3 | Overlap between LP sector interests and GP focus areas |
| Geography match | x1.3 | Interest in GP target geography, discounted for template-sourced signals |
| Check size feasibility | x1.1 | Whether the LP can write a check that makes sense for the fund size |

### Post-scoring modifiers

- **Relationship-trust bonus** (+0 to +7): If an LP has high intellectual alignment, strong relationship proximity, and low active intent, a graduated bonus rewards deep trust. These LPs take introductions on relationship alone.
- **Negative signal penalties** (-2 to -3): Explicit negative language about the GP's geography or fund structure in call notes.
- **Signal source quality discount** (x0.6 to x1.0): Scores derived from template/checkbox data are discounted relative to scores from detailed call conversations. An LP whose geography interest comes from a form field scores lower than one who spent a meeting discussing it.

### Hard filters (pre-scoring)

1. **Geographic exclusion** — LP explicitly states they will not invest in a GP-relevant geography.
2. **Fund size mismatch** — LP's minimum fund size or typical investment range far exceeds the GP's fund.
3. **Wrong framework** — LP applies PE or credit thinking to venture (focus on IRR, downside protection, cash yield).
4. **Cumulative negatives** — Three or more active negative signals (low bandwidth, delayed timing, structural mismatch, established-fund preference) compound into a rejection.

## Design decisions

- **GP-agnostic architecture.** Every scoring function, filter, and prompt reads from `gp_profile.json`. Swap the profile and re-run for a different fund — no code changes needed.
- **Notion-to-Notion loop.** Reads LP data from a Notion CRM, writes the scored report back to Notion. The output page matches the team's existing workspace format.
- **AI for interpretation, rules for decisions.** Claude extracts structured intelligence from messy call notes and writes human-readable rationales. Filters and scoring are deterministic Python — auditable, tunable, no AI ambiguity in who passes or what score they get.
- **"Hasn't done X" is not "won't do X."** Absence of experience is never treated as an exclusion. An LP who hasn't invested in a geography goes into open questions (conversation starters), not into exclusions (hard refusals). Only explicit stated refusals trigger exclusion filters.
- **Signal source quality matters.** Template checkbox data ("Geography: India") scores lower than conversational signals from a real call discussing India in depth. The extraction assesses source quality, and scoring functions apply a discount multiplier.
- **Enrichment-first pattern synthesis.** The extraction prompt enriches every significant reference (fund names, people, institutions, terms) before synthesizing the LP's investment pattern. This grounds the pattern in actual data rather than generating it independently.
- **Post-extraction cleanup.** If the same topic appears in both exclusions and open questions, it's removed from exclusions. This catches extraction errors where "didn't do X yet" is misclassified as "refuses X."
