# LP Match — Commands Reference

Quick reference for running the pipeline, managing GP profiles, and querying the CRM.

## Setup (one-time)

```bash
pip install anthropic notion-client python-dotenv
```

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY`, `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `NOTION_PARENT_PAGE_ID`.

Create a Notion integration at https://www.notion.so/my-integrations and grant it access to the LP CRM database and the report parent page.

See README.md for variable descriptions and scoring methodology.

## Terminal commands

### Full pipeline

```bash
python3 main.py
```

| Command | What it does | Notes |
|---|---|---|
| `python3 main.py` | Runs all six stages end-to-end | 3–5 min; prints Notion report URL at the end |

### GP management

```bash
python3 gp.py
```

| Command | What it does | Notes |
|---|---|---|
| `python3 gp.py` | Interactive CLI for adding, listing, switching, and deleting GP profiles | See "Inside gp.py" below |

### Query chatbot

```bash
python3 query.py
```

| Command | What it does | Notes |
|---|---|---|
| `python3 query.py` | Interactive chatbot over extracted + scored LPs | Requires `main.py` to have run; see "Inside query.py" below |

### Individual pipeline stages (debugging)

| Command | What it does | Notes |
|---|---|---|
| `python3 extract.py` | Claude extraction for all LPs | Resumes automatically on re-run |
| `python3 extract.py "LP Name"` | Extract one LP and print to stdout | No file save |
| `python3 filter.py` | Apply hard disqualification filters | Reads extracted JSON |
| `python3 score.py` | Score and rank passed LPs | Deterministic, no AI |
| `python3 rationale.py` | Generate partner-ready rationales | Top-5 scored + all rejected |
| `python3 notion_writer.py` | Write the formatted report page to Notion | Last stage |

### Cache management

Outputs are named `<stage>_<active-gp-slug>.json` under `output/`. Active slug is in `gp_profiles/_active.json`.

| Command | What it does | Notes |
|---|---|---|
| `cat gp_profiles/_active.json` | Show the currently active GP slug | Use before removing files |
| `ls output/` | List all cached artifacts | Shows which slugs have runs |
| `rm output/extracted_profiles_<slug>.json` | Force re-extraction for one GP | Use after changing a GP profile |
| `rm output/*_<slug>.json` | Remove all stages for one GP | Full clean re-run for that slug |
| `rm output/*.json` | Remove every cached artifact | All GPs, all stages |

## Inside gp.py

| Subcommand | What it does |
|---|---|
| `list` | List all GP profiles; `*` marks the active one |
| `show <name>` | Print a GP profile's structured fields |
| `add` | Paste free-text description; Claude structures it into the canonical schema |
| `switch <name>` | Switch the active GP; offers to re-run the pipeline |
| `delete <name>` | Delete a profile (asks confirmation; can't delete active) |
| `help` | Show command list |
| `exit` / `quit` | Leave the REPL |

## Inside query.py

### Slash commands

| Command | What it does |
|---|---|
| `/help` | Show command list |
| `/lp <name>` | Full profile for one LP (partial match, case-insensitive) |
| `/rank` | Ranked list of passed LPs with match % and buying profile |
| `/rejected` | Rejected LPs with gate + reason |
| `/gp` | Show the active GP profile |
| `/gp list` | List all GP profiles |
| `/gp switch <name>` | Switch active GP; offers to re-run the pipeline |
| `/gp add` | Add a new GP from pasted text |
| `/reload` | Reload `extracted_profiles` + `scored_results` JSON |
| `/clear` | Reset the 6-turn conversation memory |
| `/exit` / `/quit` | Exit |

### Natural language

Anything that doesn't start with `/` goes to Claude with the last 6 turns of conversation history, the full set of extracted LP summaries, and scoring data. Answers cite match % or quotes, and append a `[Source: ...]` footer.

## Typical workflows

### Full run against current GP

```bash
python3 gp.py            # confirm active GP via `list`, then `exit`
python3 main.py          # full pipeline → Notion report URL
python3 query.py         # ask questions against the fresh data
```

### Switching to a new GP

```bash
python3 gp.py
> add                    # paste GP description, save
> switch <new-slug>      # prompts to re-run pipeline; answer y
> exit
```

Or from inside the chatbot:

```bash
python3 query.py
> /gp switch <new-slug>
> /reload
```

### Quick LP lookup before a call

```bash
python3 query.py
> /lp weizmann
> what's his main objection?
> /exit
```

### Answering a partner's ad-hoc question

```bash
python3 query.py
> /rank
> why is #2 ranked above #3?
> compare GEM and UVIMCO
> /exit
```

## File structure reference

| File | Purpose |
|---|---|
| `main.py` | Full pipeline orchestration |
| `gp.py` | GP profile CRUD REPL |
| `query.py` | Natural-language chatbot over LPs |
| `notion_reader.py` | Stage 1: fetch LPs from Notion CRM |
| `extract.py` | Stage 2: Claude extraction |
| `filter.py` | Stage 3: hard disqualification filters |
| `score.py` | Stage 4: weighted scoring |
| `rationale.py` | Stage 5: Claude rationale generation |
| `notion_writer.py` | Stage 6: report page written to Notion |
| `config.py` | API keys, model, weights, output path helper |
| `gp_profiles/` | One JSON per GP; `_active.json` points at the live slug |
| `output/` | Per-stage JSON artifacts, named `<stage>_<slug>.json` |

See README.md for scoring methodology, hard filter definitions, and design decisions.
