import yt_dlp
import os
import sys
import json
import shutil
import threading
import re
import uuid
import logging
import subprocess
import static_ffmpeg
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from collections import OrderedDict

# Setup ffmpeg
static_ffmpeg.add_paths()
ffmpeg_path = os.path.join(os.path.dirname(static_ffmpeg.__file__), "bin", "win32")

# Use the Windows Downloads folder
downloads_folder = os.path.join(os.path.expanduser("~"), "Downloads")

# Detect aria2c for multi-connection parallel downloading
aria2c_path = shutil.which("aria2c")

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "video-downloader", "out")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
CORS(app)

# Suppress Flask request logging to prevent terminal flooding
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# Store download progress — ordered dict preserves insertion order
download_progress = OrderedDict()

# Global lock to ensure one download at a time (prevents bandwidth splitting)
download_lock = threading.Lock()
download_queue = []
queue_lock = threading.Lock()

# Atomic task_id counter
_id_counter = 0
_id_lock = threading.Lock()


def next_task_id():
    global _id_counter
    with _id_lock:
        _id_counter += 1
        return str(_id_counter)


# ─── Format selection ───────────────────────────────────────

# TV-compatible: H.264 (avc1/h264) video + AAC audio, capped at 1080p.
# Most LCD/TV players can't decode AV1/VP9/HEVC video, so when the
# downloader grabs those the TV shows "video not supported" but still
# plays the (AAC) audio track. This chain prefers formats that need no
# re-encode, falls back to anything, and a safety re-encode to H.264
# happens afterwards if a site only offered AV1/VP9.
TV_COMPAT_FORMAT = (
    "bv*[ext=mp4][vcodec~='^(avc1|h264)'][height<=1080]"
    "+ba[ext=m4a][acodec~='^(mp4a|aac)']/"
    "bv*[ext=mp4][vcodec~='^(avc1|h264)'][height<=1080]+ba/"
    "bv*[vcodec~='^(avc1|h264)']+ba/"
    "b"
)

NORMAL_FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
AUDIO_ONLY_FORMAT = "bestaudio[ext=m4a]/bestaudio"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def get_ydl_options(output_dir=None, task_id=None, tv_compat=True, audio_only=False):
    out_dir = output_dir or downloads_folder
    
    # Determine format based on audio_only flag
    if audio_only:
        format_str = AUDIO_ONLY_FORMAT
        merge_format = "m4a"
    elif tv_compat:
        format_str = TV_COMPAT_FORMAT
        merge_format = "mp4"
    else:
        format_str = NORMAL_FORMAT
        merge_format = "mp4"
    
    options = {
        "format": format_str,
        "merge_output_format": merge_format,
        "ffmpeg_location": ffmpeg_path,
        "js_runtimes": {"node": {}},
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "http_headers": BROWSER_HEADERS,
        "extractor_args": {"generic": {"impersonate": ["chrome"]}},
        "nocheckcertificate": True,
        # aria2c (16 parallel connections) when installed; otherwise the native
        # downloader with 16 concurrent fragments. NEVER fall back to the ffmpeg
        # downloader — it uses a single connection and is very slow.
        "downloader": "aria2c" if aria2c_path else "native",
        "downloader_args": {
            "aria2c": [
                "-x16",
                "-s16",
                "-k1M",
                "--file-allocation=none",
                "--max-overall-download-limit=0",
                "--max-connection-per-server=16",
                "--split=16",
                "--min-split-size=1M",
                "--continue=true",
                "--auto-file-renaming=false",
            ]
        },
        "concurrent_fragment_downloads": 16,
        "fragment_retries": 10,
        "retries": 10,
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [
            lambda d: progress_hook(d, task_id) if task_id else None
        ],
    }
    
    # Skip video post-processing if audio-only
    if audio_only:
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ]
    
    if tv_compat and not audio_only:
        # Move the moov atom to the front of the MP4 — many TVs refuse
        # USB files where the metadata sits at the end of the file.
        options["postprocessor_args"] = {
            "FFmpegMerger": ["-movflags", "+faststart"],
            "FFmpegMuxer": ["-movflags", "+faststart"],
            "FFmpegVideoConvertor": ["-movflags", "+faststart"],
        }
    return options


def get_stream_codec(filepath, stream_type):
    """Return the codec name of the first video ('v') or audio ('a') stream."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", f"{stream_type}:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def ensure_tv_compatible(filepath):
    """Re-encode to H.264 + AAC MP4 if the file isn't already TV-friendly.

    Reached only when a site didn't offer H.264 streams, so it re-encodes
    rarely. Strips subtitle tracks, which confuse many TV players.
    """
    if not os.path.isfile(filepath):
        return filepath
    vcodec = get_stream_codec(filepath, "v")
    acodec = get_stream_codec(filepath, "a")
    if vcodec is None:
        return filepath  # couldn't probe (e.g. audio-only file) — leave as is
    if vcodec == "h264" and acodec == "aac":
        return filepath  # already TV-friendly
    out_path = os.path.splitext(filepath)[0] + "_tv.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", filepath,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-sn",
        out_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60 * 60)
    except Exception:
        pass
    if os.path.isfile(out_path):
        os.replace(out_path, filepath)
    return filepath


def progress_hook(d, task_id):
    if task_id not in download_progress:
        return
    status = download_progress[task_id]
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed")
        eta = d.get("eta")
        percent = (downloaded / total * 100) if total else 0
        status.update({
            "state": "downloading",
            "percent": round(percent, 1),
            "downloaded": downloaded,
            "total": total,
            "speed": speed,
            "eta": eta,
            "filename": d.get("filename", ""),
        })
    elif d["status"] == "finished":
        status.update({"state": "processing", "percent": 100, "filename": d.get("filename", "")})


def run_download(url, task_id, tv_compat=True, audio_only=False):
    last_error = None
    try:
        for attempt in range(2):
            try:
                download_progress[task_id]["state"] = "downloading"
                download_progress[task_id]["percent"] = 0
                options = get_ydl_options(task_id=task_id, tv_compat=tv_compat, audio_only=audio_only)
                if attempt == 1:
                    if audio_only:
                        options["format"] = "bestaudio"
                    else:
                        options["format"] = "bestvideo+bestaudio/best/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
                    options["extractor_args"] = {"generic": {"impersonate": ["chrome"]}}
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "video")
                    ext = info.get("ext", "m4a" if audio_only else "mp4")
                    filename = f"{title}.{ext}"
                    filepath = os.path.join(downloads_folder, filename)
                    requested = info.get("requested_downloads") or []
                    if requested and requested[-1].get("filepath"):
                        filepath = requested[-1]["filepath"]

                    if tv_compat and not audio_only:
                        download_progress[task_id].update({"state": "processing", "percent": 100})
                        ensure_tv_compatible(filepath)

                    download_progress[task_id].update({
                        "state": "completed",
                        "percent": 100,
                        "filename": os.path.basename(filepath),
                        "filepath": filepath,
                        "title": title,
                    })
                    return
            except Exception as e:
                last_error = e
                if (tv_compat or audio_only) and attempt == 0:
                    download_progress[task_id].update({
                        "state": "retrying",
                        "percent": 0,
                        "error": f"Retrying with browser-compatible settings: {str(e)}",
                    })
                    tv_compat = False
                    audio_only = False  # Reset audio_only on retry
                    continue
                raise
    except Exception as e:
        download_progress[task_id].update({"state": "error", "error": str(e), "percent": 0})
    finally:
        download_lock.release()
        process_queue()


def process_queue():
    """Start the next download in the queue if any."""
    with queue_lock:
        if download_queue:
            next_url, next_id, next_tv, next_audio = download_queue.pop(0)
            download_progress[next_id]["state"] = "starting"
            thread = threading.Thread(target=run_download, args=(next_url, next_id, next_tv, next_audio), daemon=True)
            thread.start()


# ─── API Routes ─────────────────────────────────────────────

@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json()
    url = data.get("url", "").strip()
    tv_compat = bool(data.get("tv_compat", True))
    audio_only = bool(data.get("audio_only", False))
    
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    task_id = next_task_id()
    download_progress[task_id] = {
        "state": "queued",
        "percent": 0,
        "url": url,
        "title": url,
        "tv_compat": tv_compat,
        "audio_only": audio_only
    }

    # Try to acquire the download lock immediately
    if download_lock.acquire(blocking=False):
        # We got the lock — start downloading now
        download_progress[task_id]["state"] = "starting"
        thread = threading.Thread(target=run_download, args=(url, task_id, tv_compat, audio_only), daemon=True)
        thread.start()
    else:
        # Another download is running — add to queue
        with queue_lock:
            download_queue.append((url, task_id, tv_compat, audio_only))
        download_progress[task_id]["state"] = "queued"

    return jsonify({"task_id": task_id, "status": "started"})


@app.route("/api/progress/<task_id>")
def get_progress(task_id):
    if task_id not in download_progress:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(download_progress[task_id])


@app.route("/api/queue")
def get_queue():
    """Return the current queue status."""
    with queue_lock:
        queued = [{"task_id": tid, "url": url} for url, tid, _, _ in download_queue]
    # Return active downloads (not completed/error)
    active = {}
    for tid, info in download_progress.items():
        if info.get("state") not in ("completed", "error"):
            active[tid] = info
    return jsonify({"queued": queued, "active": active})


@app.route("/api/formats", methods=["POST"])
def get_formats():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        options = get_ydl_options()
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = []
            for f in info.get("formats", []):
                formats.append({
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution", "audio only"),
                    "fps": f.get("fps"),
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "vcodec": f.get("vcodec", "none"),
                    "acodec": f.get("acodec", "none"),
                })
            return jsonify({
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "formats": formats,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "aria2c": aria2c_path is not None,
        "downloads_folder": downloads_folder,
    })


# ─── Serve Frontend (catch-all for non-API routes) ──────────

@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    file_path = os.path.join(FRONTEND_DIR, path)
    if path and os.path.isfile(file_path):
        return send_from_directory(FRONTEND_DIR, path)
    return send_from_directory(FRONTEND_DIR, "index.html")


if __name__ == "__main__":
    # CLI mode: pass a URL as argument
    if len(sys.argv) > 1:
        url = sys.argv[1]
        task_id = "cli"
        download_progress[task_id] = {"state": "starting", "percent": 0, "url": url}
        tv_compat = "--no-tv" not in sys.argv
        audio_only = "--audio" in sys.argv or "-a" in sys.argv
        dl_name = "aria2c (16 parallel connections!)" if aria2c_path else "native downloader (16 concurrent fragments)"
        print(f"Using downloader: {dl_name}")
        if audio_only:
            print("Mode: Audio-only download (M4A)")
        else:
            print(f"TV-compatible format: {'ON (H.264, max 1080p)' if tv_compat else 'OFF'}  (pass --no-tv to disable)")

        def cli_hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                pct = (downloaded / total * 100) if total else 0
                print(f"\r  Downloading: {pct:.1f}%", end="", flush=True)
            elif d["status"] == "finished":
                print(f"\n  Download complete: {d.get('filename', '')}")

        options = get_ydl_options(task_id=task_id, tv_compat=tv_compat, audio_only=audio_only)
        options["progress_hooks"] = [cli_hook]
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            requested = info.get("requested_downloads") or []
            if requested and requested[-1].get("filepath"):
                filepath = requested[-1]["filepath"]
                if tv_compat and not audio_only:
                    print("\nChecking TV compatibility (H.264/AAC)...")
                    ensure_tv_compatible(filepath)
                print(f"\nSaved: {filepath}")
    else:
        # Web server mode — serves frontend + API on one port
        print("=" * 50)
        print("  Video Downloader")
        print("  http://localhost:5000")
        print(f"  Downloads folder: {downloads_folder}")
        dl_name = "aria2c (16 connections)" if aria2c_path else "native (16 concurrent fragments)"
        print(f"  Downloader: {dl_name}")
        print("=" * 50)
        app.run(host="0.0.0.0", port=5000, debug=True)
