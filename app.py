import os
import uvicorn
from backend.app import app

try:
    import gradio as gr
    demo = gr.Blocks(title="ClipYT Backend Engine")
    with demo:
        gr.Markdown("# 🎬 ClipYT Backend API is Live 24/7\n\n- **Frontend:** [https://clipyt.vercel.app](https://clipyt.vercel.app)\n- **API Status:** Running Healthy 🚀")
    app = gr.mount_gradio_app(app, demo, path="/gradio")
except Exception:
    pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
