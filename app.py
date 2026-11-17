import os
import time
import spaces
import gradio as gr
from backend.app import app as clipyt_api
from backend.video_processor import detect_best_encoder


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

    # Launch via official Gradio demo.launch() so Hugging Face ZeroGPU startup passes
    launch_res = demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        prevent_thread_lock=True,
    )

    # Inject ClipYt FastAPI routes (/api/* and /background*) directly onto Gradio's FastAPI server
    fastapi_app = getattr(demo, "app", None)
    if fastapi_app is None and isinstance(launch_res, tuple):
        fastapi_app = launch_res[0]

    if fastapi_app is not None:
        for route in reversed(clipyt_api.router.routes):
            path = getattr(route, "path", "")
            if path.startswith(("/api/", "/background")):
                fastapi_app.router.routes.insert(0, route)

    # Keep main thread alive while Gradio/Uvicorn serves requests
    while True:
        time.sleep(3600)
