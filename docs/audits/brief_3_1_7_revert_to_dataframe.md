# Brief #3.1.7 — Revert My Cases to gr.DataFrame (root cause + fix)

**Date:** 2026-05-16
**Subject:** `effect_update_depth_exceeded` cycle on tab activation.
**Disposition:** fixed by reverting My Cases to the gr.DataFrame +
detail-panel pattern from 7b29277. AR's @gr.render tab kept intact.

## Symptom

`effect_update_depth_exceeded` fired in Chrome DevTools Console
every time the user clicked **any non-initially-active tab** (My
Cases or Action Required) on the surgeon app. The 295ms violation
warning was the click handler's duration — the page truly hung
(unresponsive UI) until Svelte aborted the cycle internally; the
abort fired repeatedly on each activation.

## Investigation trail (briefs #3.1 → #3.1.6)

Seven iterations chased structural causes inside My Cases content
and the Gradio reactive graph:

| Brief | Hypothesis | Outcome |
|---|---|---|
| #3.1 | gr.DataFrame had its own recursion bug (#12947). Switched to HTML cards. | Introduced this cycle. |
| #3.1.1 | Per-slot `case_id_state` fanout | State count 84 → 16; cycle persisted. |
| #3.1.2 | `expanded_state.change` re-render bridge | Dropped; cycle persisted. |
| #3.1.3 | Server-side render memoization with `gr.skip()` | Steady-state ticks collapsed; cycle persisted. |
| #3.1.4 | Pre-allocated 50-slot pool fanout | Replaced with @gr.render; cycle persisted. |
| #3.1.5 | CSS `.ds-card-expandable` transition + transform | Stripped; cycle persisted. |
| #3.1.6 | Gradio `gr.Tab(render_children=False)` lazy-mount | Set `render_children=True`; cycle persisted. |

After #3.1.6 the user reported the cycle still firing. They proposed
git-bisect through our own history. The bisect found:

| Commit | My Cases content | AR content | Result |
|---|---|---|---|
| 7b29277 | gr.DataFrame + brand | 1 Markdown placeholder | Clean |
| 3aba4e5 | gr.DataFrame + brand | 10-slot HTML card pool | Clean |
| 2b5a167-stripped | 1 Markdown placeholder | 10-slot HTML card pool | Clean |
| 2b5a167-full | 50-slot HTML card pool | 10-slot HTML card pool | **Both hang** |
| a33e55a (HEAD) | @gr.render dynamic HTML cards | @gr.render dynamic HTML cards | **Both hang** |

The discriminator is **HTML cards in the My Cases tab**. Component
count, slot pool size, and @gr.render-vs-pre-allocated all turned
out to be red herrings. Whenever My Cases used HTML cards (any
flavour), activating **either** tab triggered the cycle. Whenever
My Cases used gr.DataFrame or a single gr.Markdown, both tabs were
clean.

The most striking finding: at 2b5a167-stripped, AR's 10-slot HTML
card pool was clean — the **same** AR code that hung at 2b5a167-full
and at a33e55a. Having HTML cards in My Cases somehow infects
AR's tab activation.

The Brief #3.1 commit message claimed the new HTML-card pattern
would "kill DataFrame hang" (referring to Gradio issue #12947 in
the original gr.DataFrame component). It instead introduced a
different — and more persistent — Svelte 5 cycle that we spent
seven briefs chasing.

## Root cause

Some interaction between Gradio 6.14's frontend and Svelte 5's
reactive flush, triggered specifically by HTML-card markup in the
My Cases tab when the tab is activated. We were not able to
precisely localize the trigger in Gradio's bundle. Suspected:

- Lazy hydration of HTML-card subtrees on first activation produces
  a depth-exceeded Svelte effect cycle.
- The interaction may involve our specific markup
  (`<article>` with nested `<header>` / `<svg>` etc.) plus
  Gradio's component reconciler, but stripping markup to a single
  `<div>` did not break the cycle, so the markup alone isn't the
  trigger.
- Component count crosses a threshold in some configurations
  (~300 components at 2b5a167-full), but a33e55a hung with only 62
  components, so count is not the sole discriminator.

The precise mechanism is upstream-Gradio-internal. Filing
[gradio-app/gradio#12947](https://github.com/gradio-app/gradio/issues/12947),
[#13198](https://github.com/gradio-app/gradio/issues/13198), and
[#13285](https://github.com/gradio-app/gradio/issues/13285) all
report the same family of effect_update_depth_exceeded tab-switch
issues but none with our exact reproducer. A minimal upstream repro
is future work; the in-app fix is to avoid the trigger entirely.

## Fix

Revert My Cases to the gr.DataFrame + detail-panel pattern from
commit 7b29277, ported forward into the current codebase. Specifically:

- `_build_my_cases` rebuilds the `gr.DataFrame(headers=...,
  datatype=..., elem_id="my-cases-df")` with the 7-column shape:
  UCD-FIL-ID / Date / Procedure / Approach / Indication / Status /
  Updated. Status is HTML-rendered for the brand badge.
- A `gr.Group(elem_id="my-cases-detail")` below the DataFrame holds
  the detail panel: SVG pipeline timeline, metadata markdown,
  segments list, timestamp list. Initially hidden; revealed on row
  select via `cases_df.select(render_detail, ...)`.
- `render_my_cases(request) → 5-tuple` matches the original 7b29277
  signature exactly.
- `render_detail(evt: gr.SelectData, request)` consumes the row-
  select event and updates the detail panel.

Action Required's @gr.render tab is **kept intact**. The bisect
showed AR works correctly when My Cases doesn't have HTML cards;
the cycle source was My Cases content, not AR's @gr.render block.

CSS retired alongside the revert:

- `.ds-card-expandable` (cursor + hover shadow — was for the HTML
  card hover affordance)
- `.ds-card-expansion*` family (the expansion body, dividers, list
  styling)
- `.ds-card-status-*` family (six per-state border-left colors)

CSS restored:

- DataFrame row-hover + active-row CSS targeting `[data-testid*=
  "dataframe"] tbody tr` (was removed in #3.1.5 when DataFrames
  weren't in use).

Surface change:

- Component count: 62 → 64 (DataFrame + detail panel + 4 detail
  markdowns/HTML).
- gr.State count: 16 → 14 (My Cases dropped `expanded_state` +
  `visible_cases_state`; gr.DataFrame is event-driven via
  `.select()`, not state-driven).
- Test count: 1078 → 1059 (the HTML-card-specific tests in
  test_my_cases.py are deleted; the gr.DataFrame ones from
  7b29277 are restored).

## What we lose

The per-card click-to-expand inline expansion is gone. Instead,
clicking a row in the DataFrame reveals a detail panel below the
table with the same content (pipeline timeline, metadata, segments,
timestamps). This is the UX pattern that worked at 7b29277.

Brand styling is preserved where it matters:

- Status badge HTML still renders in the DataFrame's Status
  column (per-cell HTML datatype).
- SVG pipeline timeline still renders in the detail panel.
- Tab indicator + identity line + Fraunces typography all still
  apply.
- DataFrame rows get brand-teal hover + active-row highlighting
  (CSS restored from 7b29277).

## Regression guard

Two assertions lock the revert in place:

- `tests/test_my_cases.py::test_my_cases_blocks_carries_dataframe_with_status_column`
  — the DataFrame must exist with the canonical 7-column header.
- `tests/test_intake_section5.py::test_intake_tab_state_count_unchanged_at_thirteen`
  — Intake tab state seam count locked at 13, distinct from
  AR's 1 + My Cases's 0. Re-adding gr.State seams to My Cases
  would surface here.

## Lesson

When the user reports a feature was working before a specific
change and is broken now, **git-bisect through our own history first
before chasing structural causes inside the changed code**. We
spent six briefs proposing increasingly complex fixes to a tab
that was working at 7b29277. The git-bisect — proposed by the user
in the same brief that ultimately resolved it — found the regression
boundary (2b5a167) in three deploys.

When upstream framework bugs are suspected (as we did with Gradio
#13285, #13198, #12891), confirm the bug exists in the framework
**with a minimal repro** before applying flag-flips like
`render_children=True`. Upstream issues referencing the same error
string can have multiple root causes; a workaround for one may not
fix another.

The DataFrame approach at 7b29277 was the right baseline all along;
the Brief #3.1 commit message ("kill DataFrame hang") referred to a
DataFrame-specific bug (#12947) that may have been Gradio-version-
specific and is no longer reproducible in 6.14. We don't know
because we never tested. The HTML-card pivot bought us seven
briefs of complexity for no benefit; reverting was the right call.
