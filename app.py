import os
import spaces
import gradio as gr
import gradio.routes as gr_routes
from backend.app import app as clipyt_api
from backend.video_processor import detect_best_encoder


class ClipYtRouteMiddleware:
    """
    Outermost ASGI middleware that routes /api/*, /clipyt-api/*, and /background*
    straight to ClipYt's FastAPI backend BEFORE Gradio's built-in /api handler.
    """

    def __init__(self, inner_app):
        self.inner_app = inner_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path.startswith("/api/") or path.startswith("/clipyt-api/") or path.startswith("/background"):
                if path.startswith("/clipyt-api/"):
                    scope = dict(scope, path="/api/" + path[len("/clipyt-api/"):])
                await clipyt_api(scope, receive, send)
                return
        await self.inner_app(scope, receive, send)


_orig_create_app = gr_routes.App.create_app


def _patched_create_app(*args, **kwargs):
    gradio_app = _orig_create_app(*args, **kwargs)
    gradio_app.add_middleware(ClipYtRouteMiddleware)
    return gradio_app


gr_routes.App.create_app = staticmethod(_patched_create_app)


@spaces.GPU
def _zerogpu_ready(msg: str) -> str:
    return msg


with gr.Blocks(title="ClipYt Backend") as demo:
    gr.Markdown(
        "# ClipYt Backend is Running\n"
        "Open the live app at **[https://clipyt.vercel.app](https://clipyt.vercel.app/)**."
    )
    _inp = gr.Textbox(visible=False)
    _out = gr.Textbox(visible=False)
    _btn = gr.Button(visible=False)
    _btn.click(fn=_zerogpu_ready, inputs=_inp, outputs=_out)


if __name__ == "__main__":
    detect_best_encoder()
    port = int(os.environ.get("PORT", "7860"))
    demo.launch(server_name="0.0.0.0", server_port=port)
