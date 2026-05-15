"""Surgeon Gradio Blocks app — tab shell only, no business logic.

Three tabs per v18: Intake / My Cases / Action Required. Each tab carries a
``{tab name} — coming soon`` placeholder. An identity line renders the
authenticated user via the existing session cookie (Gradio doesn't share
FastAPI's session natively; we read ``app_session`` from the request headers
on page load via ``gr.Request``).

Future tab-content specs replace the Markdown placeholders with their real
components and event handlers. The mount path (/app) and role enforcement
(via ``mount_gradio_app(auth_dependency=...)``) live in ``app/main.py``.
"""

from __future__ import annotations

import gradio as gr

from app.auth import identity_string_for_request


def _identity(request: gr.Request) -> str:
    return identity_string_for_request(request)


def build_surgeon_app() -> gr.Blocks:
    with gr.Blocks(
        title="Surgeon — surgical-cv", analytics_enabled=False
    ) as blocks:
        identity_md = gr.Markdown()
        with gr.Tabs():
            with gr.Tab("Intake"):
                gr.Markdown("**Intake** — coming soon.")
            with gr.Tab("My Cases"):
                gr.Markdown("**My Cases** — coming soon.")
            with gr.Tab("Action Required"):
                gr.Markdown("**Action Required** — coming soon.")
        blocks.load(_identity, None, identity_md)
    return blocks
