# Video Downloader

A web-based video downloader with a Python backend (yt-dlp + Flask) and Next.js frontend.

Everything runs on **one terminal, one command**.

## Setup (one-time)

```bash
# 1. Activate your venv
.venv\Scripts\activate    # Windows
source .venv/bin/activate  # Mac/Linux

# 2. Install Python dependencies
pip install flask flask-cors

# 3. Install Node dependencies
cd video-downloader
npm install

# 4. Build the frontend
npx next build
cd ..
```

## Run

```bash
python down.py
```

Open **http://localhost:5000** in your browser. That's it!

## CLI Mode

```bash
python down.py "https://www.youtube.com/watch?v=..."
```

## Architecture

```
[Browser :5000] --> [Flask serves static Next.js + /api/*] --> [yt-dlp + ffmpeg]
```

Flask serves both the frontend UI and the API on port 5000.
