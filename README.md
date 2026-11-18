# ClipYt — AI YouTube Live & Shorts Creator Studio

ClipYt is a creator tool that reformats long-form YouTube live streams and videos into viral 9:16 vertical Shorts/Reels with AI subtitles, active facecam detection, and split-screen layouts.

🌐 **Website:** [https://clipyt.vercel.app](https://clipyt.vercel.app/)

## Quick Start (Run on Laptop)

1. Make sure Python 3.10+, FFmpeg, and Node.js are installed on your computer.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Start the server:
   ```bash
   python run.py
   ```
4. ClipYt will automatically launch in your browser at `http://127.0.0.1:8000`.

## Features
- **🔥 Auto-Viral Live Detection:** Scans live chat activity to pinpoint the highest-energy moments.
- **⚡ 3s Climax Hook + 30s Clip:** Automatically cuts the peak and prepends a 3-second hook in front of the clip.
- **🎯 60/40 Split-Screen:** Auto-frames the streamer facecam (bottom 40%) and gameplay/display (top 60%).
- **🗣️ Hinglish & English AI Subtitles:** Transcribes spoken Hindi/English with Whisper without Arabic/Urdu misdetections.
- **🛡️ Concurrency Semaphore:** Built-in queue management prevents memory overload under heavy traffic.
