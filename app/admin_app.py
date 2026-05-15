"""Admin Gradio Blocks app — tab shell only, no business logic.

Two tabs per v18: Global Dashboard / Action Required. Tab content and event
handlers land in future specs. Identity rendered the same way as the surgeon
app — Gradio doesn't share FastAPI's session, so we read the cookie via
``gr.Request`` on page load.
"""

from __future__ import annotations

import gradio as gr

from app.auth import identity_string_for_request


def _identity(request: gr.Request) -> str:
    return identity_string_for_request(request)


def build_admin_app() -> gr.Blocks:
    with gr.Blocks(
        title="Admin — surgical-cv", analytics_enabled=False
    ) as blocks:
        identity_md = gr.Markdown()
        with gr.Tabs():
            with gr.Tab("Global Dashboard"):
                gr.Markdown("**Global Dashboard** — coming soon.")
            with gr.Tab("Action Required"):
                gr.Markdown("**Action Required** — coming soon.")
        blocks.load(_identity, None, identity_md)
    return blocks
