import os

try:
    import spaces  # type: ignore  # Must be imported first on Hugging Face ZeroGPU
except Exception:
    spaces = None

import uvicorn
from backend.app import app

# Register a minimal Gradio interface at /gradio with an @spaces.GPU handler
# so Hugging Face ZeroGPU startup verification passes while / serves ClipYt!
if spaces is not None:
    try:
        import gradio as gr  # type: ignore

        @spaces.GPU
        def _zerogpu_ready(msg: str = "ok") -> str:
            return msg

        _demo = gr.Interface(fn=_zerogpu_ready, inputs=gr.Text(), outputs=gr.Text())
        app = gr.mount_gradio_app(app, _demo, path="/gradio")
    except Exception:
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
