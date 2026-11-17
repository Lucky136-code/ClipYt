import os
import uvicorn
import spaces
import gradio as gr
from backend.app import app as clipyt_api
from backend.video_processor import detect_best_encoder


class ClipYtRouteMiddleware:
    """
    Wraps the final ASGI app passed into Uvicorn so /api/*, /clipyt-api/*, and
    /background* ALWAYS reach ClipYt's FastAPI backend before Gradio 6's router.
    """

    def __init__(self, inner_app):
        self.inner_app = inner_app

    def __getattr__(self, name):
        return getattr(self.inner_app, name)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path.startswith("/api/") or path.startswith("/clipyt-api/") or path.startswith("/background"):
                if path.startswith("/clipyt-api/"):
                    scope = dict(scope, path="/api/" + path[len("/clipyt-api/"):])
                await clipyt_api(scope, receive, send)
                return
        await self.inner_app(scope, receive, send)


# Hook uvicorn.Config so Gradio 6's final server app is wrapped at the outermost ASGI layer
_orig_uvicorn_config_init = uvicorn.Config.__init__


def _patched_uvicorn_config_init(self, app, *args, **kwargs):
    if callable(app) and not isinstance(app, ClipYtRouteMiddleware):
        app = ClipYtRouteMiddleware(app)
    return _orig_uvicorn_config_init(self, app, *args, **kwargs)


uvicorn.Config.__init__ = _patched_uvicorn_config_init


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
