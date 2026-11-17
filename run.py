import uvicorn
import webbrowser
import threading
import time


def open_browser(url: str):
    time.sleep(1.5)
    webbrowser.open(url)


if __name__ == "__main__":
    host = "127.0.0.1"
    port = 8000
    url = f"http://{host}:{port}"

    print("=" * 60)
    print("  ClipYT — Free YouTube Heatmap Shorts Generator")
    print("=" * 60)
    print(f"  Dashboard URL : {url}")
    print("  Press CTRL+C to stop the server.")
    print("=" * 60)

    threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    uvicorn.run("backend.app:app", host=host, port=port, reload=False)
