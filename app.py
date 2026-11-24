import spaces
import torch
import os
import shutil
from pathlib import Path

def ensure_deno_runtime():
    """Ensure Deno or Node runtime is available for yt-dlp JS challenges."""
    if shutil.which("deno") or shutil.which("node"):
        return
    deno_dir = Path("/tmp/bin")
    deno_bin = deno_dir / "deno"
    if deno_bin.exists():
        os.environ["PATH"] = f"{deno_dir}:{os.environ.get('PATH', '')}"
        return
    if os.name != "nt":
        try:
            import urllib.request, zipfile
            deno_dir.mkdir(parents=True, exist_ok=True)
            url = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
            zip_path = deno_dir / "deno.zip"
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(deno_dir)
            deno_bin.chmod(0o755)
            zip_path.unlink(missing_ok=True)
            os.environ["PATH"] = f"{deno_dir}:{os.environ.get('PATH', '')}"
            print(f"[runtime] Installed Deno at {deno_bin}", flush=True)
        except Exception as e:
            print(f"[runtime] Could not auto-install Deno: {e}", flush=True)

ensure_deno_runtime()

import gradio as gr
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
