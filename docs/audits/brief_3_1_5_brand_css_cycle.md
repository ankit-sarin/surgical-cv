# Brief #3.1.5 — Brand CSS layout-transition cycle (post-mortem)

**Date:** 2026-05-16
**Subject:** `effect_update_depth_exceeded` cycle in surgeon app My Cases tab.
**Disposition:** fixed in commit landing alongside this file.

## Symptom

Chrome DevTools Console fired Svelte's
`Uncaught Error: effect_update_depth_exceeded` at roughly 13 errors/sec
while the My Cases tab was active. Clicking the tab caused a burst of
~2,600 additional errors before the page became unresponsive. Other
tabs (Intake, Action Required) loaded clean. The error stack matched
Svelte 5's reactive flush (`flush → Pr → process → #l → #u`).

## Investigation trail (briefs #3.1 → #3.1.4)

Five prior iterations narrowed the cycle to a structural CSS issue
without yet pinpointing the rule. Summary:

- **#3.1** introduced the 50-slot pre-allocated card pool.
  `effect_update_depth_exceeded` fired immediately on My Cases mount.
- **#3.1.1** retired per-slot `case_id_state` instances. State count
  dropped from 84 to 16; bug persisted.
- **#3.1.2** dropped the `expanded_state.change → render` re-render
  bridge. LCP improved 7.21s → 3.33s; cycle persisted.
- **#3.1.3** added per-session render-output memoization with
  `gr.skip()` for unchanged outputs. Steady-state ticks collapsed to
  ~1 component update; cycle persisted.
- **#3.1.4** replaced the slot pool with `@gr.render` dynamic
  mounting. Component count dropped 258→62, dependency count 84→25,
  render outputs 104→4. Cycle persisted.

After **#3.1.4** the user provided the diagnostic anchor that closed
the case: *"tabs worked correctly before brand styling was applied —
when the UI was 'primitive.'"* The bug was in the CSS itself, not the
component graph or reactive topology.

## Root cause

Two CSS declarations on `.ds-card-expandable` in
`app/badges_html.py`:`MY_CASES_CSS`:

```css
.ds-card-expandable {
  cursor: pointer;
  transition: box-shadow 120ms ease, transform 120ms ease;
}
.ds-card-expandable:hover {
  box-shadow: 0 2px 6px rgba(44, 44, 44, 0.08);
  transform: translateY(-1px);
}
```

The mechanism:

1. `transform: translateY(-1px)` on hover creates a new compositor
   layer and forces a layout shift.
2. The `transition: transform 120ms ease` declaration causes the
   layer's geometry to interpolate over 120ms instead of resolving
   immediately.
3. Gradio's frontend uses ResizeObserver on rendered components for
   layout calculations.
4. While the transform is in-flight, ResizeObserver fires repeated
   callbacks reporting the still-changing geometry. Each callback
   triggers a reactive effect in Svelte 5.
5. The effect re-renders the card; the re-render preserves the
   `.ds-card-expandable` class; the `:hover` state remains; the
   transition re-triggers; goto 4.

Svelte 5 detects the recursion via flush-depth tracking and throws
`effect_update_depth_exceeded`. The thrown error doesn't break the
loop — the next ResizeObserver tick re-enters and Svelte throws
again, hence the steady ~13/sec rate. The cycle is unique to the My
Cases tab because Action Required's cards use the
`.ds-card-severity-*` family without composing `.ds-card-expandable`
or its transition.

## Fix

Strip the layout-affecting hover affordance. Keep the brand identity
by retaining the on-hover box-shadow (paint-only, no compositor
layer, no transition), drop the transform and the transition
declaration. Net result:

```css
.ds-card-expandable {
  cursor: pointer;
}
.ds-card-expandable:hover {
  box-shadow: 0 2px 6px rgba(44, 44, 44, 0.08);
}
```

Synced into both `app/badges_html.py` and the brand skill at
`~/.claude/skills/digitalsurgeon-brand/assets/gradio-theme.css`.

## Regression guard

`tests/test_my_cases.py::test_surgeon_css_has_no_transform_or_layout_transitions_on_cards`
scans `SURGEON_CSS` for the forbidden patterns:

- `transform: translateY` — compositor-shifting hover affordance
- `transition: transform` — animating a compositor property
- `transition: box-shadow 120ms ease, transform` — the original
  combined declaration
- `transition: max-height` — common pattern for accordion-style
  expansion, has the same ResizeObserver-race signature
- `transition: height ` (with trailing space to avoid matching
  `min-height` / `max-height`) — same as above

Color and opacity transitions stay legal — they're paint-only and
don't trigger ResizeObserver.

## Lesson

CSS transitions on layout-affecting properties (`transform`,
`height`, `max-height`, `width`, `padding`, `margin`) are
fundamentally incompatible with Svelte 5's reactive flush over
dynamically-mounted components. The transition itself doesn't cause
the cycle — it gives ResizeObserver a window during which the
geometry changes, and the observer's callbacks during that window are
what feed the reactive cycle.

Safe alternatives:

- Discrete class toggles driven by server-emitted state (instant,
  no interpolation, no ResizeObserver race).
- Paint-only transitions on `color`, `background-color`,
  `border-color`, `opacity` — no layout, no compositor layer
  change.
- Static affordances: cursor change, box-shadow on hover (no
  transition), filter brightness.

The brand skill should document this constraint so future apps
pulling from the same theme don't reintroduce the pattern.
