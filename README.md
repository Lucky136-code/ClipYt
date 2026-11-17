# ClipYT — Free YouTube Heatmap Shorts Creator

**ClipYT** is a free, local-first web application that turns long YouTube videos into ready-to-post Shorts, Reels, and TikToks. Paste any YouTube link to see its real-time **Engagement & Interaction Map**, inspect actual video frames of the most replayed moments, or manually scrub any range with a 60fps live frame preview — then render at full 1080p HD using GPU hardware acceleration.

## Features
- **YouTube Engagement & Interaction Map**: Reads YouTube's native *"Most Replayed"* viewer interaction heatmap and detects the top non-overlapping high-engagement moments automatically.
- **Actual Video Frame Previews**: Extracts real video frames from YouTube storyboards for every detected moment so you can immediately see the scene.
- **Manual Clip Selection (60fps Live Scrubber)**: Dual timeline range sliders with live Start & End video frame previews and editable `MM:SS` / `HH:MM:SS` timestamps.
- **Climax Teaser Hook**: Optionally prepends the peak 2.5-second climax moment at `0:00` as a viral teaser before playing the buildup, with captions automatically synced.
- **Custom Aspect Ratios & Framing**: Export in **9:16 Short**, **1:1 Square**, **4:5 Portrait**, or **16:9 Wide** with **Blur Background**, **Center Crop**, or **Black Bars**.
- **Fast YouTube Auto-Captions**: Uses YouTube's built-in captions (no heavy ML model downloads) with clean bold subtitle presets.
- **GPU-Accelerated Rendering**: Automatically uses NVIDIA NVENC (`h264_nvenc`), Intel QSV, or AMD AMF (with CPU fallback), stream-copy range downloads, and background stream pre-warming for fast 1080p renders.

## Requirements
- Python 3.10+
- [FFmpeg](https://ffmpeg.org/) installed and available on `PATH`
- [Node.js](https://nodejs.org/) (used by `yt-dlp` for YouTube challenge solving)

## Quick Start

```bash
pip install -r requirements.txt
python run.py
```

Open `http://127.0.0.1:8000` in your browser, select your clip length and number of clips, and paste any YouTube link.
