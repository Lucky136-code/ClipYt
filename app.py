import spaces
import torch
import gradio as gr
import os
import uvicorn
from backend.app import app

zero = torch.Tensor([0]).cuda()

@spaces.GPU
def greet(n=1):
    return f"ClipYT ZeroGPU Online: {zero + n}"

demo = gr.Interface(
    fn=greet,
    inputs=gr.Number(label="GPU Ping"),
    outputs=gr.Text(label="GPU Output"),
    title="🎬 ClipYT Cloud Backend Engine",
    description="Official 24/7 cloud backend for ClipYT (https://clipyt.vercel.app)"
)

# Mount Gradio at /gradio so root / and /api remain 100% pure FastAPI for Vercel
app = gr.mount_gradio_app(app, demo, path="/gradio")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
