import spaces
import torch
import os
import shutil
from pathlib import Path

def ensure_deno_runtime():
    """Ensure Deno standalone binary (>=2.3.0) is installed and on PATH for yt-dlp n-sig challenges."""
    deno_dir = Path("/tmp/bin")
    deno_bin = deno_dir / "deno"
    if os.name != "nt":
        needs_install = True
        if deno_bin.exists():
            try:
                res = subprocess.run([str(deno_bin), "-V"], capture_output=True, text=True)
                # Ensure deno is >= 2.3.0
                if "deno" in res.stdout.lower() and not any(f"2.{i}." in res.stdout for i in (0, 1, 2)):
                    needs_install = False
            except Exception:
                pass
        if needs_install:
            try:
                import urllib.request, zipfile
                deno_dir.mkdir(parents=True, exist_ok=True)
                url = "https://github.com/denoland/deno/releases/download/v2.9.8/deno-x86_64-unknown-linux-gnu.zip"
                zip_path = deno_dir / "deno.zip"
                urllib.request.urlretrieve(url, zip_path)
                with zipfile.ZipFile(zip_path, 'r') as z:
                    z.extractall(deno_dir)
                deno_bin.chmod(0o755)
                zip_path.unlink(missing_ok=True)
                print(f"[runtime] Installed Deno v2.9.8 successfully at {deno_bin}", flush=True)
            except Exception as e:
                print(f"[runtime] Could not auto-install Deno: {e}", flush=True)
        os.environ["PATH"] = f"{deno_dir}:{os.environ.get('PATH', '')}"

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
