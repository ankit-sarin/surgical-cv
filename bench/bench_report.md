# Pipeline Model Benchmark — Report

_Generated: 2026-04-19T06:09:38_
_Trials per (config, case): 1_

## Table 1 — Diagnostician reliability

| Config | JSON parse % | Schema adherence % | Mean latency (s) | p95 latency (s) | Thinking block detected % |
|---|---|---|---|---|---|
| diag_gemma4_31b_nothink | 100.0% | 100.0% | 70.6 | 116.8 | 0.0% |
| diag_gemma4_31b_think | 100.0% | 100.0% | 75.3 | 102.7 | 0.0% |
| diag_qwen3_32b_nothink | 100.0% | 100.0% | 55.6 | 67.2 | 0.0% |
| diag_qwen3_32b_think | 100.0% | 100.0% | 57.2 | 92.8 | 0.0% |

## Table 2 — Surgeon reliability

| Config | JSON parse % | Schema adherence % | Vocabulary adherence % | Field match rate | Mean latency (s) | p95 latency (s) |
|---|---|---|---|---|---|---|
| surg_gemma4_26b_nothink | 100.0% | 100.0% | 100.0% | 100.0% | 30.1 | 75.1 |
| surg_gemma4_26b_think | 100.0% | 100.0% | 100.0% | 100.0% | 23.3 | 38.7 |
| surg_qwen3_30b_instruct | 100.0% | 100.0% | 100.0% | 93.9% | 2.4 | 6.5 |

## Table 3 — Variance across trials

| Config | Identical JSON across all trials | Schema-valid in all trials | Schema-valid in 0 of trials |
|---|---|---|---|
| diag_gemma4_31b_nothink | 10 | 10 | 0 |
| diag_gemma4_31b_think | 10 | 10 | 0 |
| diag_qwen3_32b_nothink | 10 | 10 | 0 |
| diag_qwen3_32b_think | 10 | 10 | 0 |
| surg_gemma4_26b_nothink | 10 | 10 | 0 |
| surg_gemma4_26b_think | 10 | 10 | 0 |
| surg_qwen3_30b_instruct | 10 | 10 | 0 |

## Table 4 — Failure mode breakdown

| Config | Parse failures | Schema failures | Vocab failures | Timeouts |
|---|---|---|---|---|
| diag_gemma4_31b_nothink | 0 | 0 | 0 | 0 |
| diag_gemma4_31b_think | 0 | 0 | 0 | 0 |
| diag_qwen3_32b_nothink | 0 | 0 | 0 | 0 |
| diag_qwen3_32b_think | 0 | 0 | 0 | 0 |
| surg_gemma4_26b_nothink | 0 | 0 | 0 | 0 |
| surg_gemma4_26b_think | 0 | 0 | 0 | 0 |
| surg_qwen3_30b_instruct | 0 | 0 | 0 | 0 |

## Flagged responses (top 3 worst per config)

### diag_gemma4_31b_nothink

- **D01 trial 1** — ok
  - `{   "verdict": "pass",   "issues": [     {       "category": "none",       "severity": "info",       "detail": "Output is clean: audio stream was successfully removed, no identifying metadata is present, and duration and resolution match the input."     }   ],   "remediation": "No action required" }`
- **D02 trial 1** — ok
  - `{   "verdict": "fail",   "issues": [     {       "category": "audio_leak",       "severity": "critical",       "detail": "The de-identified output contains an audio stream (Stream #0:1, aac), which violates the requirement that a clean de-id has no audio stream."     }   ],   "remediation": "Modify the FFmpeg command to exclude audio by adding the '-an' flag or removing the audio stream mapping." }`
- **D03 trial 1** — ok
  - `{   "verdict": "fail",   "issues": [     {       "category": "metadata_leak",       "severity": "critical",       "detail": "The de-identified output contains identifying camera and software metadata: Make (SteCam), Model (HD Capture 4000), and Software (BDV Archive 6.2)."     }   ],   "remediation": "Update the FFmpeg command to include '-map_metadata -1' to strip all global metadata from the output file." }`

### diag_gemma4_31b_think

- **D01 trial 1** — ok
  - `{   "verdict": "pass",   "issues": [     {       "category": "none",       "severity": "info",       "detail": "The de-identified output contains only the video stream with no audio leak, no identifying metadata, and maintains the expected duration and resolution."     }   ],   "remediation": "No action required" }`
- **D02 trial 1** — ok
  - `{   "verdict": "fail",   "issues": [     {       "category": "audio_leak",       "severity": "critical",       "detail": "The de-identified output contains an audio stream (Stream #0:1, aac), which violates the requirement that de-identified videos must have no audio."     }   ],   "remediation": "Modify the FFmpeg command to exclude the audio stream, for example by adding the '-an' flag or removing the audio stream mapping." }`
- **D03 trial 1** — ok
  - `{   "verdict": "fail",   "issues": [     {       "category": "metadata_leak",       "severity": "critical",       "detail": "Identifying camera and software metadata remains in the output: Make (SteCam), Model (HD Capture 4000), and Software (BDV Archive 6.2)."     }   ],   "remediation": "Re-run the de-identification process with the FFmpeg flag '-map_metadata -1' to strip all global metadata from the output file." }`

### diag_qwen3_32b_nothink

- **D01 trial 1** — ok
  - `{   "verdict": "pass",   "issues": [     {       "category": "none",       "severity": "info",       "detail": "De-identified output has no audio stream, no identifying metadata, valid h264/MP4 format, correct duration (2h21m12s), and consistent 1920x1080 resolution. FFmpeg process completed without errors."     }   ],   "remediation": "No action required" }`
- **D02 trial 1** — ok
  - `{   "verdict": "fail",   "issues": [     {       "category": "audio_leak",       "severity": "critical",       "detail": "Audio stream (aac, 128 kb/s) remains in de-identified output; audio streams must be removed during de-identification."     }   ],   "remediation": "Modify the de-identification pipeline to explicitly disable audio stream copying (e.g., use FFmpeg's `-an` flag) and reprocess the video." }`
- **D03 trial 1** — ok
  - `{   "verdict": "fail",   "issues": [     {       "category": "metadata_leak",       "severity": "critical",       "detail": "Exiftool output contains identifying metadata: Make='SteCam', Model='HD Capture 4000', Software='BDV Archive 6.2'"     }   ],   "remediation": "Ensure metadata stripping process explicitly removes Make, Model, Software, and other identifying fields during de-identification. Verify with exiftool -all= before final output." }`

### diag_qwen3_32b_think

- **D01 trial 1** — ok
  - `{   "verdict": "pass",   "issues": [     {       "category": "none",       "severity": "info",       "detail": "No audio stream present, no identifying metadata leaks, correct resolution (1920x1080), consistent 30fps, and valid MP4 container format with expected duration."     }   ],   "remediation": "" }`
- **D02 trial 1** — ok
  - `{   "verdict": "fail",   "issues": [     {       "category": "audio_leak",       "severity": "critical",       "detail": "Audio stream (AAC 128kbps) remains in de-identified output"     }   ],   "remediation": "Re-run de-identification with audio stream removal (e.g., add '-an' flag to FFmpeg command)" }`
- **D03 trial 1** — ok
  - `{   "verdict": "fail",   "issues": [     {       "category": "metadata_leak",       "severity": "critical",       "detail": "Make (SteCam), Model (HD Capture 4000), and Software (BDV Archive 6.2) metadata fields remain in de-identified output"     },     {       "category": "duration_mismatch",       "severity": "warning",       "detail": "ExifTool reports Media Duration as 1:55:42, while ffprobe shows 1:55:41.80 (0.2s discrepancy)"     }   ],   "remediation": "Modify de-identification pipeline `

### surg_gemma4_26b_nothink

- **S01 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-16",     "or_room": "OR 12",     "procedure_name": "Low anterior resection",     "approach": "Robotic",     "indication": "Colorectal cancer",     "notes": null   },   "needs_clarification": false,   "clarification_prompts": [] }`
- **S02 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-19",     "or_room": "OR 8",     "procedure_name": "Hartmann reversal",     "approach": "Open",     "indication": "Diverticulitis",     "notes": "recurrent; prior Hartmann six months ago"   },   "needs_clarification": false,   "clarification_prompts": [] }`
- **S03 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-19",     "or_room": "OR 4",     "procedure_name": "Sigmoidectomy",     "approach": "Laparoscopic",     "indication": "Diverticulitis",     "notes": "Airway issues at induction, nearly canceled."   },   "needs_clarification": false,   "clarification_prompts": [] }`

### surg_gemma4_26b_think

- **S01 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-16",     "or_room": "OR 12",     "procedure_name": "Low anterior resection",     "approach": "Robotic",     "indication": "Colorectal cancer",     "notes": null   },   "needs_clarification": false,   "clarification_prompts": [] }`
- **S02 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-19",     "or_room": "OR 8",     "procedure_name": "Hartmann reversal",     "approach": "Open",     "indication": "Diverticulitis",     "notes": "recurrent; prior Hartmann six months ago"   },   "needs_clarification": false,   "clarification_prompts": [] }`
- **S03 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-19",     "or_room": "OR 4",     "procedure_name": "Sigmoidectomy",     "approach": "Laparoscopic",     "indication": "Diverticulitis",     "notes": "Airway issues at induction, nearly canceled."   },   "needs_clarification": false,   "clarification_prompts": [] }`

### surg_qwen3_30b_instruct

- **S01 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-16",     "or_room": "OR 12",     "procedure_name": "Low anterior resection",     "approach": "Robotic",     "indication": "Colorectal cancer",     "notes": "Robotic LAR for rectal cancer"   },   "needs_clarification": false,   "clarification_prompts": [] }`
- **S02 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-19",     "or_room": "OR 8",     "procedure_name": "Hartmann reversal",     "approach": "Open",     "indication": "Diverticulitis",     "notes": "Recurrent diverticulitis; prior Hartmann procedure six months ago"   },   "needs_clarification": false,   "clarification_prompts": [] }`
- **S03 trial 1** — ok
  - `{   "case_manifest": {     "surgeon": "sarin",     "case_date": "2026-04-19",     "or_room": "OR 4",     "procedure_name": "Sigmoidectomy",     "approach": "Laparoscopic",     "indication": "Diverticulitis",     "notes": "Airway issues at induction, nearly canceled"   },   "needs_clarification": false,   "clarification_prompts": [] }`
