# Brief #3.1.6 — Gradio lazy tab-mount Svelte cycle (root cause + fix)

**Date:** 2026-05-16
**Subject:** `effect_update_depth_exceeded` cycle in the surgeon app.
**Disposition:** fixed by `render_children=True` on every `gr.Tab`.

## Symptom

Chrome DevTools Console fired
`Uncaught Error: effect_update_depth_exceeded` whenever the user
clicked **any** non-initially-active tab — confirmed against both My
Cases and Action Required tabs. Initial page load on the Intake tab
(the default-active tab) stayed clean. The browser's `[Violation]`
warning reported `'click' handler took 295ms` on tab activation —
Svelte 5 aborts the cycle at its flush-depth limit, so the page
isn't truly hung, just degraded with a thrown error.

## Investigation trail

Briefs #3.1 through #3.1.5 each isolated a structural suspect inside
the My Cases tab content and applied a structural fix. The cycle
persisted through every iteration because **the cycle source was
never inside My Cases content** — it was in the Gradio `gr.Tab`
component's lazy-mount behaviour, common to every tab.

- **#3.1** retired `gr.DataFrame` for HTML cards. Reduced reach but
  cycle remained.
- **#3.1.1** removed per-slot `gr.State` fanout. State count
  84 → 16. No effect on cycle.
- **#3.1.2** dropped `expanded_state.change → render` re-render
  bridge. LCP improved; cycle persisted.
- **#3.1.3** added per-session memoization with `gr.skip()`. Cycle
  persisted.
- **#3.1.4** moved cards to `@gr.render` dynamic mounting. Component
  count 258 → 62, dependency count 84 → 25. Cycle persisted.
- **#3.1.5** stripped `transform`/`transition` from
  `.ds-card-expandable`. Cycle persisted.

The bisection that landed the diagnosis (this brief):

- **Step 1.** Disabled `css=` and `theme=` on `mount_gradio_app`
  for the surgeon app. Cycle persisted. CSS ruled out.
- **Step 4a.** Replaced rich card HTML with primitive `<div>` text.
  Cycle persisted. Markup content ruled out.
- **Step 4b.** Removed `@gr.render` block entirely; mounted cards as
  a single static `gr.HTML`. Cycle persisted. `@gr.render` ruled
  out.
- **AR tab probe.** User clicked Action Required (which has the same
  `@gr.render` pattern but 0 attention items in production).
  Console hung the same way. **This was the decisive observation** —
  the cycle was triggered by tab activation itself, not by anything
  in the tab content.

## Root cause

`gr.Tab` defaults to `render_children=False`, which **lazy-mounts**
the tab's subtree on first activation. When the user clicks a
non-initially-active tab for the first time, Gradio's frontend
synchronously hydrates the entire hidden subtree in a single
microtask. With a non-trivial subtree (multiple `gr.HTML`,
`gr.State`, `gr.Markdown`, `gr.Timer`), the hydration triggers a
cascade of Svelte 5 reactive effects whose depth exceeds the flush
limit. Svelte throws `effect_update_depth_exceeded` to abort the
cycle, the page recovers, but the broken error fires every time the
tab activates.

Upstream confirmation:

- [gradio-app/gradio#13285](https://github.com/gradio-app/gradio/issues/13285)
  — "Page Freezes/Unresponsive with effect_update_depth_exceeded on
  Tab Switch in 6.11.0" (closed)
- [gradio-app/gradio#13198](https://github.com/gradio-app/gradio/issues/13198)
  — "With Gradio 6.11.0 tab switching completely freezes compared to
  6.10.0" (open)
- [gradio-app/gradio#12891](https://github.com/gradio-app/gradio/issues/12891)
  — "Performance Issue: Very slow first tab switch / accordion
  expand with many lazy-mounted children"

The lazy-mount feature was added in Gradio 6.6.0 (PR #12906 — "Lazy
load sub-tab and accordion components") as a performance optimization
for apps with very large tabs. The feature pays the hydration cost on
first activation instead of initial load. For apps with non-trivial
tabs and Svelte 5 reactivity, that synchronous hydration trips the
flush-depth limit.

## Fix

Set `render_children=True` on every `gr.Tab(...)` in `build_surgeon_app()`:

```python
with gr.Tabs():
    with gr.Tab("Intake", render_children=True):
        # ... Intake content
    with gr.Tab("My Cases", render_children=True):
        _build_my_cases(blocks)
    with gr.Tab("Action Required", render_children=True):
        _build_action_required(blocks)
```

`render_children=True` forces eager mounting during the initial page
load. The browser pays the hydration cost once, on a render pass
Svelte handles cleanly. Subsequent tab switches are pure visibility
toggles — no re-hydration, no effect cascade, no cycle.

Tradeoff: marginally slower initial page load (all three tabs
hydrate up front instead of just Intake). For our app — three small
tabs — this is unmeasurable in practice. The behaviour matches what
Gradio defaulted to **before** PR #12906 in 6.6.0.

## Regression guard

`tests/test_my_cases.py::test_all_surgeon_tabs_eager_render_children`
walks the built `Blocks` and asserts every `gr.Tab` has
`render_children=True`. Any new tab added without this flag will
fail the test before it can ship.

## Lesson

Briefs #3.1 through #3.1.5 spent six iterations chasing structural
suspects inside My Cases. The actual bug was in a Gradio default
that changed in 6.6.0 and affects **every tab in the app**, not
just My Cases. The diagnostic that finally found it took three
deploys: (1) disable all custom CSS, (2) primitive markup, (3)
remove `@gr.render` entirely. Each step ruled out one of our own
surfaces. The decisive evidence was the AR tab probe — proving the
cycle is per-tab-activation, not per-tab-content.

Going forward: when a frontend error survives multiple structural
fixes in our own code, check upstream Gradio issues with the exact
error string. The pattern `is:issue effect_update_depth_exceeded`
returned the two reproductions of this bug; reading them earlier
would have shortened the chain by five briefs.
