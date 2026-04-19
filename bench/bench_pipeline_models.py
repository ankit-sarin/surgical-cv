"""Pipeline model benchmark orchestrator.

Implements the 7-config × 20-case × N-trial sweep described in
~/projects/surgical-cv/pipeline_model_benchmark_spec.md.
"""

import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime
from pathlib import Path

import httpx
import jsonschema
import ollama

# Set to 3 for production run. Start at 1 for dry-run validation.
TRIALS = 1

BENCH_DIR = Path(__file__).resolve().parent
CONFIGS_PATH = BENCH_DIR / "configs.json"
CASES_PATH = BENCH_DIR / "test_cases.json"
SCHEMAS_DIR = BENCH_DIR / "schemas"
VOCAB_DIR = BENCH_DIR / "vocabularies"
RESULTS_DIR = BENCH_DIR / "results"
DB_PATH = RESULTS_DIR / "bench_results.sqlite"
LOG_PATH = RESULTS_DIR / "run_log.txt"
REPORT_PATH = BENCH_DIR / "bench_report.md"

REQUIRED_MODELS = ["qwen3:32b", "qwen3:30b-instruct", "gemma4:31b", "gemma4:26b"]
OLLAMA_HOST = "http://localhost:11434"
CALL_TIMEOUT_S = 180
MIN_FREE_GB = 1.0

DIAGNOSTICIAN_SCHEMA_TEXT = """{
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
          "category": {"type": "string", "enum": ["audio_leak", "metadata_leak", "codec_error", "format_error", "empty_output", "duration_mismatch", "resolution_drift", "framerate_drift", "file_corruption", "none", "other"]},
          "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
          "detail": {"type": "string"}
        }
      }
    },
    "remediation": {"type": "string"}
  }
}"""

SURGEON_SCHEMA_TEXT = """{
  "type": "object",
  "required": ["case_manifest", "needs_clarification", "clarification_prompts"],
  "properties": {
    "case_manifest": {
      "type": "object",
      "required": ["surgeon", "case_date", "or_room", "procedure_name", "approach", "indication", "notes"],
      "properties": {
        "surgeon": {"type": ["string", "null"]},
        "case_date": {"type": ["string", "null"], "pattern": "^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},
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
}"""

PROCEDURES = ["Right hemicolectomy", "Left hemicolectomy", "Sigmoidectomy",
              "Low anterior resection", "Abdominoperineal resection",
              "Total proctocolectomy with IPAA", "Total abdominal colectomy",
              "Transverse colectomy", "Ileocolic resection", "Ventral mesh rectopexy",
              "Hartmann procedure", "Hartmann reversal", "Stoma creation",
              "Stoma reversal", "Small bowel resection", "Diagnostic laparoscopy",
              "Exploratory laparotomy", "Other"]
APPROACHES = ["Open", "Laparoscopic", "Robotic", "Hybrid"]
INDICATIONS = ["Colorectal cancer", "Diverticulitis", "Crohn's disease",
               "Ulcerative colitis", "Rectal prolapse", "Benign polyp",
               "Bowel obstruction", "Complex perianal disease",
               "Palliative stoma", "Stoma takedown", "Other"]


DIAGNOSTICIAN_SYSTEM_PROMPT = f"""You are a QA reviewer for a surgical-video de-identification pipeline. Each case provides three blocks of tool output produced when an OR video was de-identified: (1) ffprobe of the de-identified output, (2) exiftool of the de-identified output, and (3) the FFmpeg stderr from the de-identification run.

Your job: decide whether the de-identified output is acceptable, identify any specific issues, and recommend a remediation. A clean de-id has no audio stream, no identifying metadata (no Make, Model, Software, GPS, serials), no codec/format errors, expected duration and resolution, and a non-empty output file.

Issue categories (use exactly one of these strings per issue):
- audio_leak: an audio stream is present in the de-id output
- metadata_leak: identifying camera/software/location metadata remains
- codec_error: codec-level failure (e.g., non-monotonous DTS, decode errors)
- format_error: container or stream format problem
- empty_output: output file is empty or has zero streams
- duration_mismatch: output duration does not match expected/manifest duration
- resolution_drift: resolution changes mid-stream or differs from input
- framerate_drift: framerate inconsistent or variable when constant expected
- file_corruption: input or intermediate file unreadable / truncated
- none: no issues (verdict pass; issues array contains a single entry with category="none")
- other: anything that does not fit the above

Severity: "critical" (must fix before release), "warning" (should review), "info" (FYI only).

Respond with ONLY a single JSON object that conforms to this schema. No prose, no markdown fences, no explanation outside the JSON:

{DIAGNOSTICIAN_SCHEMA_TEXT}

If verdict is "pass", issues should contain exactly one entry: {{"category": "none", "severity": "info", "detail": "<brief summary of why the output is clean>"}} and remediation should be "" or "No action required".
"""


SURGEON_SYSTEM_PROMPT = f"""You are an intake assistant for a colorectal-surgery research database. The user is the operating surgeon, dictating a brief natural-language description of a case they just finished. Your job is to extract structured case metadata or, if the dictation is ambiguous or incomplete, request clarification.

Allowed values (use these exact strings — never paraphrase, never invent new ones):

procedure_name (one of):
{json.dumps(PROCEDURES)}

approach (one of):
{json.dumps(APPROACHES)}

indication (one of):
{json.dumps(INDICATIONS)}

Rules:
- Map common abbreviations and synonyms to the canonical vocabulary string. Examples: "LAR" -> "Low anterior resection"; "right hemi" -> "Right hemicolectomy"; "TPC with IPAA" -> "Total proctocolectomy with IPAA"; "lap" -> "Laparoscopic"; "robotic-assisted" -> "Robotic". Map indications by primary diagnosis: "rectal cancer" / "sigmoid cancer" / "colon cancer" -> "Colorectal cancer"; "UC" -> "Ulcerative colitis".
- If a procedure does not map to any vocabulary item, set procedure_name = "Other" and put the spoken term in notes.
- Each case is one row. If the surgeon mentions a secondary procedure (e.g., "plus diverting loop ileostomy"), keep procedure_name as the primary procedure and put the secondary in notes.
- Resolve compound or qualifier phrases by primary diagnosis; put qualifiers (e.g., "obstructing", "recurrent") in notes.
- case_date format must be YYYY-MM-DD. If the surgeon says "today", "yesterday", or a partial date like "April 16", convert relative to today's date. If you cannot determine the date confidently, set it to null.
- or_room: use the format "OR <number>" (e.g., "OR 12"). If the surgeon describes the room without a number ("the one next to the Aesculap room"), set or_room = null and add a clarification prompt.
- surgeon: copy the surgeon_username field provided in the user message verbatim into surgeon. If no username is provided, set surgeon = null.
- notes: free-text field for anything that does not fit a structured slot — preamble, secondary procedures, qualifiers, untranscribable terms.
- If any required structured field is missing or ambiguous, set needs_clarification = true and list one short question per missing field in clarification_prompts. If everything is determinable, set needs_clarification = false and clarification_prompts = [].

Respond with ONLY a single JSON object that conforms to this schema. No prose, no markdown fences, no explanation outside the JSON:

{SURGEON_SCHEMA_TEXT}
"""


# ---------- preflight ----------

def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fail(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def run_preflight_checks() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5.0)
        r.raise_for_status()
        tags_payload = r.json()
    except Exception as e:
        fail(f"Ollama daemon not reachable at {OLLAMA_HOST} ({e}). Start it with: ollama serve")

    installed = {m.get("name", "") for m in tags_payload.get("models", [])}
    installed |= {n.split(":")[0] for n in installed}
    missing = [m for m in REQUIRED_MODELS if m not in installed]
    if missing:
        print("ERROR: missing Ollama models. Pull them with:", file=sys.stderr)
        for m in missing:
            print(f"  ollama pull {m}", file=sys.stderr)
        sys.exit(2)

    if sqlite3.sqlite_version_info < (3, 0, 0):
        fail(f"SQLite >= 3 required, found {sqlite3.sqlite_version}")

    free_gb = shutil.disk_usage(BENCH_DIR).free / (1024 ** 3)
    if free_gb < MIN_FREE_GB:
        fail(f"Need >= {MIN_FREE_GB} GB free in {BENCH_DIR}, only {free_gb:.2f} GB available")

    log(f"Preflight OK. Free disk: {free_gb:.1f} GB. Models: {sorted(REQUIRED_MODELS)}")


# ---------- loaders ----------

def load_json(path: Path):
    with open(path) as f:
        return json.load(f)


def load_vocabularies() -> dict:
    return {
        "procedures": load_json(VOCAB_DIR / "procedures.json"),
        "approaches": load_json(VOCAB_DIR / "approaches.json"),
        "indications": load_json(VOCAB_DIR / "indications.json"),
    }


def load_schemas() -> dict:
    return {
        "diagnostician": load_json(SCHEMAS_DIR / "diagnostician_output.json"),
        "surgeon": load_json(SCHEMAS_DIR / "surgeon_output.json"),
    }


# ---------- DB ----------

def init_sqlite(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          config_id TEXT NOT NULL,
          case_id TEXT NOT NULL,
          trial INTEGER NOT NULL,
          latency_s REAL,
          input_tokens INTEGER,
          output_tokens INTEGER,
          raw_response TEXT,
          parsed_json TEXT,
          parse_valid INTEGER,
          schema_valid INTEGER,
          vocab_valid INTEGER,
          field_matches INTEGER,
          thinking_block_present INTEGER,
          timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_config ON results(config_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_case ON results(case_id)")
    conn.commit()
    return conn


def insert_result(conn: sqlite3.Connection, **row) -> None:
    conn.execute("""
        INSERT INTO results
          (config_id, case_id, trial, latency_s, input_tokens, output_tokens,
           raw_response, parsed_json, parse_valid, schema_valid, vocab_valid,
           field_matches, thinking_block_present)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["config_id"], row["case_id"], row["trial"], row["latency_s"],
        row["input_tokens"], row["output_tokens"], row["raw_response"],
        row["parsed_json"], row["parse_valid"], row["schema_valid"],
        row["vocab_valid"], row["field_matches"], row["thinking_block_present"],
    ))
    conn.commit()


# ---------- prompt assembly ----------

def system_prompt_for(role: str) -> str:
    if role == "diagnostician":
        return DIAGNOSTICIAN_SYSTEM_PROMPT
    if role == "surgeon":
        return SURGEON_SYSTEM_PROMPT
    raise ValueError(f"unknown role: {role}")


def render_user_prompt(case: dict) -> str:
    if case["role"] == "diagnostician":
        i = case["input"]
        return (
            "=== ffprobe output ===\n"
            f"{i['ffprobe_output']}\n\n"
            "=== exiftool output ===\n"
            f"{i['exiftool_output']}\n\n"
            "=== ffmpeg stderr ===\n"
            f"{i['ffmpeg_stderr']}\n"
        )
    dictation = case["input"]
    header_lines = []
    if case.get("surgeon_username"):
        header_lines.append(f"surgeon_username: {case['surgeon_username']}")
    header_lines.append(f"today: {date.today().isoformat()}")
    return "\n".join(header_lines) + "\n\nDictation:\n" + dictation


def apply_thinking_mode(system: str, user: str, config: dict) -> tuple[str, str]:
    """Return (system, user) with the per-family thinking-mode toggle applied.

    - Qwen3 thinking ON: no change (thinks by default).
    - Qwen3 thinking OFF: append /no_think to the user message.
    - Gemma4 thinking ON: prepend <|think|> to the system prompt.
    - Gemma4 thinking OFF: no <|think|> token.
    - Qwen3 instruct (surgeon role): use as-is.
    """
    model = config["model"]
    mode = config.get("thinking_mode", "off")
    is_qwen_instruct = "instruct" in model
    is_qwen = model.startswith("qwen")
    is_gemma = model.startswith("gemma")

    if is_qwen_instruct:
        return system, user
    if is_qwen:
        if mode == "off":
            user = user.rstrip() + "\n\n/no_think"
        return system, user
    if is_gemma:
        if mode == "on":
            system = "<|think|>\n" + system
        return system, user
    return system, user


# ---------- ollama call ----------

def call_ollama(client: ollama.Client, model: str, system: str, user: str, options: dict) -> dict:
    return client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        stream=False,
        options=options,
    )


# ---------- response parsing ----------

THINK_QWEN_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
THINK_GEMMA_RE = re.compile(r"<\|channel>thought\n(.*?)<channel\|>", re.DOTALL | re.IGNORECASE)
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_thinking_block(raw: str) -> str:
    parts = []
    for m in THINK_QWEN_RE.finditer(raw):
        parts.append(m.group(1).strip())
    for m in THINK_GEMMA_RE.finditer(raw):
        parts.append(m.group(1).strip())
    return "\n\n".join(p for p in parts if p)


def strip_thinking_and_fences(raw: str) -> str:
    text = THINK_QWEN_RE.sub("", raw)
    text = THINK_GEMMA_RE.sub("", text)
    fences = FENCE_RE.findall(text)
    if fences:
        text = max(fences, key=len)
    return text.strip()


def extract_json_object(text: str) -> str:
    """Return the first balanced {...} substring, or the whole text if none found."""
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def try_parse_json(text: str):
    candidate = extract_json_object(text)
    try:
        return json.loads(candidate), True
    except (json.JSONDecodeError, ValueError):
        return None, False


# ---------- scoring ----------

def validate_against_schema(parsed, schema) -> bool:
    if parsed is None:
        return False
    try:
        jsonschema.validate(parsed, schema)
        return True
    except jsonschema.ValidationError:
        return False


def check_vocabulary_adherence(parsed, vocabs) -> int | None:
    if parsed is None or not isinstance(parsed, dict):
        return 0
    manifest = parsed.get("case_manifest")
    if not isinstance(manifest, dict):
        return 0
    checks = [
        ("procedure_name", vocabs["procedures"]),
        ("approach", vocabs["approaches"]),
        ("indication", vocabs["indications"]),
    ]
    for field, vocab in checks:
        v = manifest.get(field)
        if v is None or v == "Other":
            continue
        if v not in vocab:
            return 0
    return 1


def score_field_matches(parsed, expected: dict | None) -> int | None:
    if expected is None:
        return None
    if parsed is None or not isinstance(parsed, dict):
        return 0
    manifest = parsed.get("case_manifest")
    if not isinstance(manifest, dict):
        return 0
    matches = 0
    for k, v in expected.items():
        actual = manifest.get(k)
        if actual is None or v is None:
            if actual == v:
                matches += 1
            continue
        if str(actual).strip().lower() == str(v).strip().lower():
            matches += 1
    return matches


# ---------- report ----------

def _pct(num: int, denom: int) -> str:
    return f"{(100.0 * num / denom):.1f}%" if denom else "n/a"


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = max(0, int(round(0.95 * (len(s) - 1))))
    return s[k]


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def generate_report(db_path: Path, report_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM results").fetchall()]
    conn.close()

    if not rows:
        report_path.write_text("# Pipeline Model Benchmark — Report\n\n_No results in DB._\n")
        return

    by_config: dict[str, list[dict]] = {}
    for r in rows:
        by_config.setdefault(r["config_id"], []).append(r)

    config_role: dict[str, str] = {}
    for cfg in load_json(CONFIGS_PATH):
        config_role[cfg["config_id"]] = cfg["role"]

    diag_configs = sorted(c for c in by_config if config_role.get(c) == "diagnostician")
    surg_configs = sorted(c for c in by_config if config_role.get(c) == "surgeon")
    all_configs = diag_configs + surg_configs

    lines: list[str] = []
    lines.append("# Pipeline Model Benchmark — Report")
    lines.append("")
    lines.append(f"_Generated: {datetime.now().isoformat(timespec='seconds')}_")
    lines.append(f"_Trials per (config, case): {TRIALS}_")
    lines.append("")

    unreliable = []
    for cid in all_configs:
        rs = by_config[cid]
        timeouts = sum(1 for r in rs if r["raw_response"] == "TIMEOUT")
        if rs and timeouts / len(rs) > 0.20:
            unreliable.append(cid)
    if unreliable:
        lines.append("> ⚠️ UNRELIABLE — >20% timeout rate: " + ", ".join(unreliable))
        lines.append("")

    lines.append("## Table 1 — Diagnostician reliability")
    lines.append("")
    lines.append("| Config | JSON parse % | Schema adherence % | Mean latency (s) | p95 latency (s) | Thinking block detected % |")
    lines.append("|---|---|---|---|---|---|")
    for cid in diag_configs:
        rs = by_config[cid]
        n = len(rs)
        parse_ok = sum(1 for r in rs if r["parse_valid"])
        schema_ok = sum(1 for r in rs if r["schema_valid"])
        thinking = sum(1 for r in rs if r["thinking_block_present"])
        lats = [r["latency_s"] for r in rs if r["latency_s"] is not None]
        lines.append(
            f"| {cid} | {_pct(parse_ok, n)} | {_pct(schema_ok, n)} | "
            f"{_mean(lats):.1f} | {_p95(lats):.1f} | {_pct(thinking, n)} |"
        )
    lines.append("")

    cases_index = load_json(CASES_PATH)
    expected_count_by_case = {
        c["case_id"]: len(c.get("expected_fields") or {})
        for c in cases_index
        if c.get("expected_fields")
    }

    lines.append("## Table 2 — Surgeon reliability")
    lines.append("")
    lines.append("| Config | JSON parse % | Schema adherence % | Vocabulary adherence % | Field match rate | Mean latency (s) | p95 latency (s) |")
    lines.append("|---|---|---|---|---|---|---|")
    for cid in surg_configs:
        rs = by_config[cid]
        n = len(rs)
        parse_ok = sum(1 for r in rs if r["parse_valid"])
        schema_ok = sum(1 for r in rs if r["schema_valid"])
        vocab_ok = sum(1 for r in rs if r["vocab_valid"])
        matches_total = sum(r["field_matches"] for r in rs if r["field_matches"] is not None)
        denom = sum(
            expected_count_by_case.get(r["case_id"], 0)
            for r in rs if r["field_matches"] is not None
        )
        field_rate = _pct(int(matches_total), int(denom)) if denom else "n/a"
        lats = [r["latency_s"] for r in rs if r["latency_s"] is not None]
        lines.append(
            f"| {cid} | {_pct(parse_ok, n)} | {_pct(schema_ok, n)} | "
            f"{_pct(vocab_ok, n)} | {field_rate} | {_mean(lats):.1f} | {_p95(lats):.1f} |"
        )
    lines.append("")

    lines.append("## Table 3 — Variance across trials")
    lines.append("")
    lines.append("| Config | Identical JSON across all trials | Schema-valid in all trials | Schema-valid in 0 of trials |")
    lines.append("|---|---|---|---|")
    for cid in all_configs:
        rs = by_config[cid]
        by_case: dict[str, list[dict]] = {}
        for r in rs:
            by_case.setdefault(r["case_id"], []).append(r)
        identical = 0
        all_schema = 0
        none_schema = 0
        for case_id, trials in by_case.items():
            jsons = [t["parsed_json"] for t in trials]
            if len(trials) >= TRIALS and len(set(jsons)) == 1:
                identical += 1
            schema_count = sum(1 for t in trials if t["schema_valid"])
            if schema_count == len(trials) and len(trials) > 0:
                all_schema += 1
            if schema_count == 0:
                none_schema += 1
        lines.append(f"| {cid} | {identical} | {all_schema} | {none_schema} |")
    lines.append("")

    lines.append("## Table 4 — Failure mode breakdown")
    lines.append("")
    lines.append("| Config | Parse failures | Schema failures | Vocab failures | Timeouts |")
    lines.append("|---|---|---|---|---|")
    for cid in all_configs:
        rs = by_config[cid]
        parse_fail = sum(1 for r in rs if not r["parse_valid"])
        schema_fail = sum(1 for r in rs if not r["schema_valid"])
        vocab_fail = sum(1 for r in rs if r["vocab_valid"] == 0)
        timeouts = sum(1 for r in rs if r["raw_response"] == "TIMEOUT")
        lines.append(f"| {cid} | {parse_fail} | {schema_fail} | {vocab_fail} | {timeouts} |")
    lines.append("")

    lines.append("## Flagged responses (top 3 worst per config)")
    lines.append("")
    for cid in all_configs:
        rs = by_config[cid]
        ranked = sorted(
            rs,
            key=lambda r: (
                0 if r["raw_response"] == "TIMEOUT" else 1,
                r["parse_valid"] or 0,
                r["schema_valid"] or 0,
                (r["vocab_valid"] if r["vocab_valid"] is not None else 1),
            ),
        )
        worst = ranked[:3]
        if not worst:
            continue
        lines.append(f"### {cid}")
        lines.append("")
        for r in worst:
            reasons = []
            if r["raw_response"] == "TIMEOUT":
                reasons.append("timeout")
            if not r["parse_valid"]:
                reasons.append("parse_fail")
            if not r["schema_valid"]:
                reasons.append("schema_fail")
            if r["vocab_valid"] == 0:
                reasons.append("vocab_fail")
            reason = ", ".join(reasons) or "ok"
            snippet = (r["raw_response"] or "")[:500].replace("\n", " ")
            lines.append(f"- **{r['case_id']} trial {r['trial']}** — {reason}")
            lines.append(f"  - `{snippet}`")
        lines.append("")

    report_path.write_text("\n".join(lines))


# ---------- main ----------

def main() -> None:
    run_preflight_checks()
    configs = load_json(CONFIGS_PATH)
    cases = load_json(CASES_PATH)
    vocabs = load_vocabularies()
    schemas = load_schemas()

    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = init_sqlite(DB_PATH)

    client = ollama.Client(host=OLLAMA_HOST, timeout=CALL_TIMEOUT_S)

    total = sum(
        len([c for c in cases if c["role"] == cfg["role"]]) * TRIALS
        for cfg in configs
    )
    progress = 0
    log(f"Starting benchmark: {len(configs)} configs × cases × {TRIALS} trial(s) = {total} calls")

    for config in configs:
        role = config["role"]
        role_cases = [c for c in cases if c["role"] == role]
        base_system = system_prompt_for(role)

        for case in role_cases:
            base_user = render_user_prompt(case)
            system, user = apply_thinking_mode(base_system, base_user, config)

            for trial in range(1, TRIALS + 1):
                progress += 1
                t0 = time.time()
                raw_text = ""
                input_tokens = None
                output_tokens = None
                try:
                    response = call_ollama(client, config["model"], system, user, config.get("ollama_options", {}))
                    latency = time.time() - t0
                    raw_text = response.get("message", {}).get("content", "") or ""
                    input_tokens = response.get("prompt_eval_count")
                    output_tokens = response.get("eval_count")
                except (httpx.TimeoutException, httpx.ReadTimeout, TimeoutError):
                    latency = float(CALL_TIMEOUT_S)
                    raw_text = "TIMEOUT"
                except ollama.ResponseError as e:
                    latency = time.time() - t0
                    raw_text = f"OLLAMA_ERROR: {e}"
                except Exception as e:
                    latency = time.time() - t0
                    raw_text = f"ERROR: {type(e).__name__}: {e}"

                if raw_text == "TIMEOUT":
                    parsed, parse_valid = None, False
                    schema_valid = False
                    json_str = ""
                    thinking_text = ""
                else:
                    thinking_text = extract_thinking_block(raw_text)
                    json_str = strip_thinking_and_fences(raw_text)
                    parsed, parse_valid = try_parse_json(json_str)
                    schema_valid = parse_valid and validate_against_schema(parsed, schemas[role])

                vocab_valid = check_vocabulary_adherence(parsed, vocabs) if role == "surgeon" else None
                field_matches = score_field_matches(parsed, case.get("expected_fields")) if role == "surgeon" else None

                insert_result(
                    conn,
                    config_id=config["config_id"],
                    case_id=case["case_id"],
                    trial=trial,
                    latency_s=latency,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    raw_response=raw_text,
                    parsed_json=json_str,
                    parse_valid=int(bool(parse_valid)),
                    schema_valid=int(bool(schema_valid)),
                    vocab_valid=vocab_valid,
                    field_matches=field_matches,
                    thinking_block_present=int(bool(thinking_text)),
                )
                log(
                    f"[{progress}/{total}] {config['config_id']} {case['case_id']} t{trial}: "
                    f"parse={bool(parse_valid)} schema={bool(schema_valid)} t={latency:.1f}s"
                )

    conn.close()
    generate_report(DB_PATH, REPORT_PATH)
    print(f"\nDone. Report: {REPORT_PATH}  |  DB: {DB_PATH}")


if __name__ == "__main__":
    main()
