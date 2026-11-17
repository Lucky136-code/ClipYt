import os
import uvicorn
from backend.app import app

# Satisfy Hugging Face ZeroGPU startup check if Space runs on ZeroGPU hardware
try:
    import spaces  # type: ignore

    @spaces.GPU
    def _zerogpu_ready():
        return True
except Exception:
    pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
