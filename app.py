import os
import spaces
import gradio as gr
import gradio.routes as gr_routes
from backend.app import app as clipyt_api
from backend.video_processor import detect_best_encoder


# Inject ClipYt FastAPI routes (/api/* and /background*) into Gradio's FastAPI app
# BEFORE Gradio's catch-all frontend route, even when ZeroGPU intercepts demo.launch()
_orig_create_app = gr_routes.App.create_app


def _patched_create_app(*args, **kwargs):
    gradio_app = _orig_create_app(*args, **kwargs)
    for route in reversed(clipyt_api.router.routes):
        path = getattr(route, "path", "")
        if path.startswith(("/api/", "/background")):
            gradio_app.router.routes.insert(0, route)
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
