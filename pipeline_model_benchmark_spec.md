# Pipeline Model Benchmark — Claude Code Task Spec

**For:** Claude Code (DGX Spark, `~/projects/surgical-cv/bench/`)
**From:** Planning session (claude.ai Research Planning project)
**Session context:** NAS Infrastructure Unified Plan v12, §2 (Pipeline Architecture)
**Blast radius:** Low (file creation, Python script, SQLite, local Ollama API calls — no git, no systemd, no network config). Run with `--dangerously-skip-permissions`.
**Skill reference:** `ollama` Claude Code skill for all model invocation patterns.

---

## 1. Objective

Empirically select one **dense** model (PI diagnostician role) and one **MoE** model (surgeon-interactive role) from four candidates, using structured JSON output reliability and latency as the primary criteria. Produce evidence, not a decision — final model selection happens during results review with the PI.

Two roles, two task domains:

| Role | Purpose | Task type |
|---|---|---|
| PI diagnostician | Review ffprobe/exiftool/FFmpeg stderr, issue pass/fail verdict with reasons | Complex, reasoning-heavy, latency-tolerant |
| Surgeon-interactive | Extract case metadata from natural-language surgeon dictation | Latency-sensitive, schema-strict, vocabulary-constrained |

---

## 2. Test matrix (7 configurations)

| # | Role | Ollama tag | Thinking mode | Notes |
|---|---|---|---|---|
| 1 | Diagnostician | `qwen3:32b` | ON | Apply Qwen3 thinking template |
| 2 | Diagnostician | `qwen3:32b` | OFF | `/no_think` directive |
| 3 | Diagnostician | `gemma4:31b` | ON | `<|think|>` in system prompt |
| 4 | Diagnostician | `gemma4:31b` | OFF | Omit `<|think|>` token |
| 5 | Surgeon | `qwen3:30b-instruct` | N/A | 2507 instruct variant, non-thinking by design |
| 6 | Surgeon | `gemma4:26b` | ON | MoE, 3.8B active, thinking enabled |
| 7 | Surgeon | `gemma4:26b` | OFF | MoE, thinking disabled |

**Deliberate asymmetry:** Qwen3 has separate weights for thinking vs. instruct (`-thinking-2507` vs. `-instruct-2507`); we use only the instruct weights for the surgeon role. Gemma 4 toggles modes within a single model via control tokens, so we test both modes.

---

## 3. Prerequisites (fail-fast checks)

Before running the benchmark, the script must verify:

1. Ollama daemon is running: `curl -sf http://localhost:11434/api/tags`
2. All four models are pulled and show up in `/api/tags`:
   - `qwen3:32b`
   - `qwen3:30b-instruct`
   - `gemma4:31b`
   - `gemma4:26b`
3. If any model is missing, print the exact `ollama pull <tag>` commands and exit non-zero. Do not attempt auto-pull.
4. SQLite 3.x available (system Python adequate).
5. Disk space: ≥1 GB free in `~/projects/surgical-cv/bench/` for results DB and logs.

---

## 4. Directory layout

```
~/projects/surgical-cv/bench/
├── bench_pipeline_models.py     # Orchestrator (main script)
├── configs.json                 # 7-row test matrix (see §5)
├── test_cases.json              # 20 test cases (see §7 — pre-drafted, copy verbatim)
├── schemas/
│   ├── diagnostician_output.json    # JSON Schema for role A (see §6)
│   └── surgeon_output.json          # JSON Schema for role B (see §6)
├── vocabularies/
│   ├── procedures.json              # 18 items (copy from surgical-cv/config/)
│   ├── approaches.json              # 4 items
│   └── indications.json             # 11 items
├── results/
│   ├── bench_results.sqlite         # Raw results DB (created by script)
│   └── run_log.txt                  # Timestamped execution log
└── bench_report.md                  # Auto-generated pivot tables + flagged cases
```

Copy vocabularies from the frozen `~/projects/surgical-cv/config/*.json` files if they exist; otherwise create from the lists in §7.3.

---

## 5. `configs.json` structure

```json
[
  {
    "config_id": "diag_qwen3_32b_think",
    "role": "diagnostician",
    "model": "qwen3:32b",
    "thinking_mode": "on",
    "system_prompt_file": "prompts/diag_thinking_qwen.txt",
    "ollama_options": {"temperature": 0.1, "num_ctx": 8192}
  },
  {
    "config_id": "diag_qwen3_32b_nothink",
    "role": "diagnostician",
    "model": "qwen3:32b",
    "thinking_mode": "off",
    "system_prompt_file": "prompts/diag_nothink_qwen.txt",
    "ollama_options": {"temperature": 0.1, "num_ctx": 8192}
  }
  // ... 5 more entries for configs 3–7
]
```

**Implementation detail:** Qwen3 toggles thinking via `/no_think` suffix in the user message (per Qwen docs) or via `enable_thinking: false` in the chat template. Gemma 4 toggles via `<|think|>` token presence at the start of the system prompt (verified from ollama.com/library/gemma4:26b page). The script must apply the correct mechanism per model family — do not hardcode one pattern.

---

## 6. Output schemas

### 6.1 Diagnostician output schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["verdict", "issues", "remediation"],
  "properties": {
    "verdict": {"type": "string", "enum": ["pass", "fail"]},
    "issues": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["category", "severity", "detail"],
        "properties": {
          "category": {
            "type": "string",
            "enum": [
              "audio_leak", "metadata_leak", "codec_error",
              "format_error", "empty_output", "duration_mismatch",
              "resolution_drift", "framerate_drift", "file_corruption",
              "none", "other"
            ]
          },
          "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
          "detail": {"type": "string"}
        }
      }
    },
    "remediation": {"type": "string"}
  }
}
```

### 6.2 Surgeon output schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["case_manifest", "needs_clarification", "clarification_prompts"],
  "properties": {
    "case_manifest": {
      "type": "object",
      "required": ["surgeon", "case_date", "or_room", "procedure_name", "approach", "indication", "notes"],
      "properties": {
        "surgeon": {"type": ["string", "null"]},
        "case_date": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "or_room": {"type": ["string", "null"]},
        "procedure_name": {"type": ["string", "null"]},
        "approach": {"type": ["string", "null"]},
        "indication": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]}
      }
    },
    "needs_clarification": {"type": "boolean"},
    "clarification_prompts": {"type": "array", "items": {"type": "string"}}
  }
}
```

Note: `procedure_name`, `approach`, `indication` must either be a literal string from the loaded vocabulary, `"Other"`, or `null`. Vocabulary adherence is a scored metric, not a schema constraint (to distinguish parse failure from vocabulary drift).

---

## 7. Test cases (pre-drafted — copy verbatim into `test_cases.json`)

### 7.1 Diagnostician cases (10)

Each case provides three input blocks (ffprobe stdout, exiftool stdout, FFmpeg stderr) concatenated into a single user message. The model receives the raw tool output and must produce the structured verdict.

| ID | Scenario | Expected verdict | Expected top category |
|---|---|---|---|
| D01 | Clean de-id output — no audio, scrubbed metadata, ffmpeg exit 0 | pass | none |
| D02 | Audio stream present in deid output (deid filter failed) | fail | audio_leak |
| D03 | Exiftool shows `Make: SteCam`, `Model: HD Capture 4000`, `Software: BDV Archive 6.2` | fail | metadata_leak |
| D04 | FFmpeg stderr: `Non-monotonous DTS in output stream 0:0; previous: 1440, current: 720` repeated 200+ times | fail | codec_error |
| D05 | FFmpeg stderr: `Invalid data found when processing input` on segment 3 of 6 | fail | file_corruption |
| D06 | ffprobe shows resolution change from 1920x1080 to 1280x720 mid-stream | fail | resolution_drift |
| D07 | ffprobe on deid file returns zero streams, duration 0.000000 | fail | empty_output |
| D08 | Deid duration 00:10:14, but manifest-recorded concat duration 02:21:12 | fail | duration_mismatch |
| D09 | ffprobe shows `30 fps, 30 tbr` but stream info logs `avg_frame_rate=0/0`; variable framerate confirmed | fail | framerate_drift |
| D10 | File exists, `ls -l` shows 0 bytes, ffmpeg exited 0 (silent failure) | fail | empty_output |

**JSON structure for each case:**

```json
{
  "case_id": "D01",
  "role": "diagnostician",
  "input": {
    "ffprobe_output": "...<realistic ffprobe output>...",
    "exiftool_output": "...<realistic exiftool output>...",
    "ffmpeg_stderr": "...<realistic stderr>..."
  },
  "expected_verdict": "pass",
  "expected_category": "none"
}
```

Claude Code: generate realistic ffprobe/exiftool/stderr text for each case based on the scenarios above. Use the UCD-FIL-001 successful run as the template for "clean" outputs (should be in `~/projects/surgical-cv/` logs or reproducible with `ffprobe` and `exiftool` against the file itself). For failure cases, construct plausible outputs by modification.

### 7.2 Surgeon cases (10)

Each case provides a natural-language dictation string as the user message. The model must extract structured metadata or request clarification.

| ID | Dictation | Expected behavior |
|---|---|---|
| S01 | `"Just finished a robotic LAR for rectal cancer in OR 12. Today is April 16."` | Full extraction, no clarification needed |
| S02 | `"Did an open Hartmann reversal today in OR 8. Patient had recurrent diverticulitis with prior Hartmann six months ago."` | Full extraction, indication = Diverticulitis |
| S03 | `"Had a rough one today. Airway issues at induction, nearly canceled. Eventually got in and did a lap sigmoid for diverticulitis, OR 4."` | Extract procedure/approach/indication/room; route preamble to notes |
| S04 | `"Scope in OR 12."` | needs_clarification = true; prompt for procedure, approach, indication, date |
| S05 | `"Did a TaTME for low rectal cancer, OR 7, robotic-assisted."` | procedure_name = "Other", notes must include "TaTME", approach = Robotic |
| S06 | `"Robotic LAR plus diverting loop ileostomy for rectal cancer, OR 12."` | procedure_name = "Low anterior resection"; loop ileo in notes (not a separate procedure row — row-per-case) |
| S07 | `"Robotic right hemi for cancer today. I can't remember the room — it was the one next to the Aesculap room."` | needs_clarification = true; prompt for or_room specifically |
| S08 | `"TPC with IPAA today for UC, OR 9."` | Canonical mapping: procedure_name = "Total proctocolectomy with IPAA", indication = "Ulcerative colitis" |
| S09 | `"The one I did yesterday, you know which one."` | needs_clarification = true; prompt for all fields (ambiguous date, no case details) |
| S10 | `"Obstructing sigmoid cancer, lap Hartmann in OR 4 this morning."` | Resolve compound indication: primary = Colorectal cancer; put "bowel obstruction" in notes |

**JSON structure for each case:**

```json
{
  "case_id": "S01",
  "role": "surgeon",
  "input": "Just finished a robotic LAR for rectal cancer in OR 12. Today is April 16.",
  "surgeon_username": "sarin",
  "expected_clarification": false,
  "expected_fields": {
    "procedure_name": "Low anterior resection",
    "approach": "Robotic",
    "indication": "Colorectal cancer",
    "or_room": "OR 12"
  }
}
```

`expected_fields` is for automated comparison scoring; partial matches are acceptable (the script scores field-by-field, not all-or-nothing).

### 7.3 Vocabularies (copy into `vocabularies/`)

```json
// procedures.json
["Right hemicolectomy", "Left hemicolectomy", "Sigmoidectomy",
 "Low anterior resection", "Abdominoperineal resection",
 "Total proctocolectomy with IPAA", "Total abdominal colectomy",
 "Transverse colectomy", "Ileocolic resection", "Ventral mesh rectopexy",
 "Hartmann procedure", "Hartmann reversal", "Stoma creation",
 "Stoma reversal", "Small bowel resection", "Diagnostic laparoscopy",
 "Exploratory laparotomy", "Other"]

// approaches.json
["Open", "Laparoscopic", "Robotic", "Hybrid"]

// indications.json
["Colorectal cancer", "Diverticulitis", "Crohn's disease",
 "Ulcerative colitis", "Rectal prolapse", "Benign polyp",
 "Bowel obstruction", "Complex perianal disease",
 "Palliative stoma", "Stoma takedown", "Other"]
```

---

## 8. Script architecture (pseudocode)

```python
# bench_pipeline_models.py — pseudocode, not copy-paste

def main():
    run_preflight_checks()   # §3
    configs = load_json("configs.json")
    cases = load_json("test_cases.json")
    vocabs = load_vocabularies()
    db = init_sqlite("results/bench_results.sqlite")

    total = len(configs) * len(cases) * 3   # 3 trials per pair
    progress = 0

    for config in configs:
        role_cases = [c for c in cases if c["role"] == config["role"]]
        system_prompt = build_system_prompt(config, role_cases[0], vocabs)
        for case in role_cases:
            user_prompt = render_user_prompt(case)
            for trial in [1, 2, 3]:
                t0 = time.time()
                response = call_ollama(
                    model=config["model"],
                    system=system_prompt,
                    user=user_prompt,
                    options=config["ollama_options"],
                    thinking_mode=config["thinking_mode"]
                )
                latency = time.time() - t0

                raw_text = response["message"]["content"]
                thinking_text = extract_thinking_block(raw_text, config)  # Gemma only
                json_str = strip_thinking_and_fences(raw_text)
                parsed, parse_valid = try_parse_json(json_str)
                schema_valid = validate_against_schema(parsed, config["role"])
                vocab_valid = check_vocabulary_adherence(parsed, vocabs, config["role"])
                field_matches = score_field_matches(parsed, case.get("expected_fields"))

                db.insert(
                    config_id=config["config_id"],
                    case_id=case["case_id"],
                    trial=trial,
                    latency_s=latency,
                    input_tokens=response.get("prompt_eval_count"),
                    output_tokens=response.get("eval_count"),
                    raw_response=raw_text,
                    parsed_json=json_str,
                    parse_valid=parse_valid,
                    schema_valid=schema_valid,
                    vocab_valid=vocab_valid,
                    field_matches=field_matches,
                    thinking_block_present=bool(thinking_text)
                )
                progress += 1
                log(f"[{progress}/{total}] {config['config_id']} {case['case_id']} t{trial}: "
                    f"parse={parse_valid} schema={schema_valid} t={latency:.1f}s")

    generate_report(db, "bench_report.md")
    print(f"\nDone. Report: bench_report.md  |  DB: results/bench_results.sqlite")

if __name__ == "__main__":
    main()
```

**Ollama invocation:** use the `ollama` Python client (preferred) or the `/api/chat` REST endpoint. Use non-streaming mode (streaming complicates thinking-block extraction and the planning session already flagged streaming-related tool-call failures in the upstream architecture doc).

**Temperature:** 0.1 across all configs for reproducibility. Do not tune per-model.

**Timeout:** 180 s per call. On timeout, record as `parse_valid=False, latency_s=180, raw_response="TIMEOUT"`.

---

## 9. SQLite schema

```sql
CREATE TABLE results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  config_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  trial INTEGER NOT NULL,
  latency_s REAL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  raw_response TEXT,
  parsed_json TEXT,
  parse_valid INTEGER,         -- 0/1
  schema_valid INTEGER,        -- 0/1
  vocab_valid INTEGER,         -- 0/1, nullable for diagnostician
  field_matches INTEGER,       -- count of correct fields, nullable
  thinking_block_present INTEGER,  -- 0/1
  timestamp TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_config ON results(config_id);
CREATE INDEX idx_case ON results(case_id);
```

---

## 10. Report format (`bench_report.md`)

Four tables, one flagged-responses section:

**Table 1 — Diagnostician reliability** (configs 1–4):

| Config | JSON parse % | Schema adherence % | Mean latency (s) | p95 latency (s) | Thinking block detected % |

**Table 2 — Surgeon reliability** (configs 5–7):

| Config | JSON parse % | Schema adherence % | Vocabulary adherence % | Field match rate | Mean latency (s) | p95 latency (s) |

**Table 3 — Variance across trials** (all configs):

| Config | Cases with identical JSON across 3 trials | Cases with schema-valid in all 3 | Cases with schema-valid in 0 of 3 |

**Table 4 — Failure mode breakdown** (all configs):

| Config | Parse failures | Schema failures | Vocab failures | Timeouts |

**Flagged responses section:** For each config, show the 3 worst failures (parse + schema + unexpected output). Print `case_id`, `trial`, `raw_response` (truncated to 500 chars), and the failure reason. This is the human-review surface — the PI will scan these during results discussion.

---

## 11. Success criteria

- Script exits 0 with all 420 rows present in `results/bench_results.sqlite`
- `bench_report.md` exists and contains all four tables fully populated
- No config has >20% timeout rate (if it does, note at top of report with `⚠️ UNRELIABLE`)
- Every config tested against every case in its role bucket, 3 trials each
- Runtime < 4 hours wall clock (if it exceeds this, abort and report partial results)

---

## 12. Non-goals (do not do)

- Do not tune prompts per-model. All diagnostician configs use the same system prompt; all surgeon configs use the same system prompt. Prompt engineering is a separate future pass.
- Do not pick a winning model. Produce tables, highlight outliers, stop.
- Do not judge semantic correctness of natural-language fields (`detail`, `remediation`, `notes`). Those surface in the flagged-responses section for human review.
- Do not retry on failure. A failure is a data point.
- Do not pull models. The preflight check fails fast and instructs the user to pull manually.

---

## 13. Handoff notes to Claude Code

1. Use the `ollama` skill for model invocation patterns — do not reinvent the API wrapper.
2. Use the `new-project` skill conventions if `~/projects/surgical-cv/bench/` does not yet exist; otherwise place files in the existing directory without overwriting.
3. After completing the run, do not commit results to git. The planning session will review `bench_report.md` first; commit is a separate decision.
4. If any preflight check fails, print a single clear remediation line and exit non-zero. No partial runs.
5. On the first end-to-end dry run, reduce trials to 1 (set `TRIALS = 1` at the top of the script) to confirm the harness works before committing to 90+ minutes. Once the dry run passes, set `TRIALS = 3` and run for real.

---

## 14. Planning-session questions deferred to post-run review

After Claude Code completes the benchmark and surfaces `bench_report.md`, the PI will discuss with the planning session:

- Whether automated metrics are sufficient or manual semantic grading of the top 2 finalists is needed
- Whether any config shows disqualifying behavior (e.g., frequent hallucinated vocabulary terms, systematic schema drift)
- Final model selection (one dense for diagnostician, one MoE for surgeon-interactive)
- Whether thinking mode should be runtime-configurable in `pipeline.py` or hardcoded per role

These are not for Claude Code to answer.

---

**End of spec.**
