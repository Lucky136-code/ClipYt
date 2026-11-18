"""
ClipYt Public Server Launcher
Runs the backend on your laptop and connects it to https://clipyt.vercel.app
so users worldwide are served directly by your laptop's CPU/GPU!
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VERCEL_JSON_PATH = BASE_DIR / "vercel.json"
CLOUDFLARED_PATH = Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe")

if not CLOUDFLARED_PATH.exists():
    which_cf = shutil.which("cloudflared")
    if which_cf:
        CLOUDFLARED_PATH = Path(which_cf)
    else:
        print("[ERROR] cloudflared.exe not found.")
        print("Please install cloudflared or place cloudflared.exe in your PATH.")
        sys.exit(1)


def is_port_open(port: int = 8000) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.8)
        return s.connect_ex(('127.0.0.1', port)) == 0


def is_server_healthy(port: int = 8000) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/")
        with urllib.request.urlopen(req, timeout=1.5) as res:
            return res.status in (200, 404)
    except Exception:
        return False


def kill_process_on_port(port: int = 8000):
    try:
        out = subprocess.check_output(f'netstat -ano | findstr :{port}', shell=True, text=True)
        for line in out.strip().splitlines():
            if "LISTENING" in line:
                pid = line.strip().split()[-1]
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
        time.sleep(1.2)
    except Exception:
        pass


def update_vercel_json_destination(tunnel_url: str) -> bool:
    """Updates vercel.json with the laptop's public tunnel URL and pushes to GitHub."""
    if not VERCEL_JSON_PATH.exists():
        print(f"[ERROR] vercel.json not found at {VERCEL_JSON_PATH}")
        return False

    try:
        content = json.loads(VERCEL_JSON_PATH.read_text(encoding="utf-8"))
        updated = False
        target_dest = f"{tunnel_url}/api/:path*"

        for rule in content.get("rewrites", []):
            if rule.get("source") == "/api/:path*":
                if rule.get("destination") != target_dest:
                    rule["destination"] = target_dest
                    updated = True
                break

        if updated:
            VERCEL_JSON_PATH.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
            print(f"[OK] Updated vercel.json destination -> {target_dest}")

            # Auto git commit & push to deploy on Vercel
            print("[INFO] Pushing updated routing to GitHub for Vercel auto-deploy...")
            subprocess.run(["git", "add", "vercel.json"], cwd=str(BASE_DIR), check=True)
            subprocess.run(
                ["git", "commit", "-m", f"chore(deploy): point API to laptop tunnel {tunnel_url}"],
                cwd=str(BASE_DIR),
                capture_output=True,
            )
            push_res = subprocess.run(["git", "push", "origin", "main"], cwd=str(BASE_DIR), capture_output=True, text=True)
            if push_res.returncode == 0:
                print("[OK] Pushed to GitHub! Vercel is now routing to your laptop.")
            else:
                print(f"[WARN] Git push output: {push_res.stderr.strip()[:200]}")
        else:
            print("[INFO] vercel.json is already pointing to this tunnel.")

        return True
    except Exception as e:
        print(f"[WARN] Could not auto-update vercel.json: {e}")
        return False


def main():
    print("=" * 72)
    print("  🚀 ClipYt Laptop Server — Serving Worldwide Users from Your Laptop")
    print("=" * 72)

    # 1. Start local FastAPI Uvicorn server on port 8000
    server_proc = None
    if is_port_open(8000):
        if is_server_healthy(8000):
            print("[1/3] Backend server is already running and healthy on port 8000. Reusing it! ✓")
        else:
            print("[1/3] Port 8000 is occupied by an unresponsive process. Restarting it cleanly...")
            kill_process_on_port(8000)
            server_cmd = [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8000"]
            server_proc = subprocess.Popen(server_cmd, cwd=str(BASE_DIR))
            time.sleep(1.5)
    else:
        print("[1/3] Starting backend server on port 8000...")
        server_cmd = [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8000"]
        server_proc = subprocess.Popen(server_cmd, cwd=str(BASE_DIR))
        time.sleep(1.5)

    # 2. Start Cloudflare Tunnel to expose port 8000 to public HTTPS
    print("[2/3] Establishing Cloudflare HTTPS public tunnel...")
    tunnel_cmd = [str(CLOUDFLARED_PATH), "tunnel", "--url", "http://127.0.0.1:8000"]
    tunnel_proc = subprocess.Popen(
        tunnel_cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tunnel_url = None
    t0 = time.time()
    for line in tunnel_proc.stdout:
        m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
        if m:
            tunnel_url = m.group(0)
            break
        if time.time() - t0 > 25:
            break

    if not tunnel_url:
        print("[ERROR] Failed to obtain Cloudflare tunnel URL.")
        if server_proc:
            server_proc.terminate()
        tunnel_proc.terminate()
        sys.exit(1)

    print(f"[OK] Public Tunnel established: {tunnel_url}")

    # 3. Connect Vercel to your laptop tunnel
    print("[3/3] Connecting https://clipyt.vercel.app to your laptop...")
    update_vercel_json_destination(tunnel_url)

    print()
    print("=" * 72)
    print("  🎉 CLIPYT IS NOW LIVE FOR ALL USERS WORLDWIDE!")
    print("=" * 72)
    print(f"  🌐 Live Public Website : https://clipyt.vercel.app")
    print(f"  🔗 Laptop Public API   : {tunnel_url}")
    print(f"  💻 Local Dashboard     : http://127.0.0.1:8000")
    print()
    print("  All user requests on clipyt.vercel.app are being processed")
    print("  directly on your laptop (zero cloud limits, zero bans!).")
    print()
    print("  [KEEP THIS TERMINAL WINDOW OPEN while serving users]")
    print("  [Press Ctrl+C when you want to stop]")
    print("=" * 72)
    print()

    # Handle graceful shutdown
    def _shutdown(sig=None, frame=None):
        print("\n[INFO] Shutting down Cloudflare tunnel...")
        try:
            tunnel_proc.terminate()
        except Exception:
            pass
        if server_proc:
            try:
                server_proc.terminate()
            except Exception:
                pass
        print("[OK] Public server stopped cleanly. Goodbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            time.sleep(1)
            if server_proc and server_proc.poll() is not None:
                print("\n[WARN] Local backend server stopped.")
                break
            if tunnel_proc.poll() is not None:
                print("\n[WARN] Cloudflare tunnel stopped.")
                break
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()
