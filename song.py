from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
import sqlite3
import yt_dlp
import os
import re
import requests
import mimetypes

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = "downloads"
DB_PATH = "music_library.db"
BASE_URL = "http://localhost:8000"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Serve downloads via custom streaming endpoint instead of StaticFiles
# so we can handle Range requests properly


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE NOT NULL,
            file_path TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', "_", name.strip())
    name = re.sub(r'[^\w\-.]', "", name)
    return name[:200]


def get_song_from_db(title: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT file_path FROM songs WHERE title = ?", (title,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def save_song_to_db(title: str, file_path: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO songs (title, file_path) VALUES (?, ?)",
        (title, file_path),
    )
    conn.commit()
    conn.close()


YOUTUBE_API_KEY = "AIzaSyAGzUftmFs8inGmhd4jdgUdDSHmmXVoVOY"


@app.get("/search")
async def search_songs(q: str):
    if not q or len(q.strip()) < 2:
        return {"results": []}

    search_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "maxResults": 5,
        "q": q,
        "type": "video",
        "key": YOUTUBE_API_KEY,
    }

    try:
        response = requests.get(search_url, params=params)
        response.raise_for_status()
        data = response.json()
        results = []

        for item in data.get("items", []):
            title = item.get("snippet", {}).get("title", "")
            title = (title
                     .replace("&quot;", '"')
                     .replace("&#39;", "'")
                     .replace("&amp;", "&"))
            video_id = item.get("id", {}).get("videoId", "")
            if title and video_id:
                results.append({"title": title, "id": video_id})

        return {"results": results}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@app.get("/stream")
async def stream_audio(request: Request, filename: str):
    """Stream audio with proper Range request support so browsers can seek."""
    file_path = os.path.join(DOWNLOAD_DIR, filename)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    # Prevent directory traversal
    abs_path = os.path.realpath(file_path)
    abs_dir  = os.path.realpath(DOWNLOAD_DIR)
    if not abs_path.startswith(abs_dir):
        raise HTTPException(status_code=403, detail="Forbidden")

    file_size = os.path.getsize(file_path)

    # Detect mime type
    ext = os.path.splitext(filename)[1].lower()
    mime_map = {
        ".webm": "audio/webm",
        ".m4a":  "audio/mp4",
        ".mp4":  "audio/mp4",
        ".opus": "audio/ogg; codecs=opus",
        ".ogg":  "audio/ogg",
        ".mp3":  "audio/mpeg",
    }
    media_type = mime_map.get(ext, "application/octet-stream")

    # Parse Range header
    range_header = request.headers.get("range")
    chunk_size = 1024 * 512  # 512 KB chunks

    if range_header:
        # e.g. "bytes=0-1023"
        range_val = range_header.strip().replace("bytes=", "")
        parts = range_val.split("-")
        start = int(parts[0]) if parts[0] else 0
        end   = int(parts[1]) if parts[1] else file_size - 1
        end   = min(end, file_size - 1)
        length = end - start + 1

        def iter_file():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(chunk_size, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            "Content-Range":  f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges":  "bytes",
            "Content-Length": str(length),
        }
        return StreamingResponse(iter_file(), status_code=206,
                                 headers=headers, media_type=media_type)

    # No Range header — stream entire file
    def iter_full():
        with open(file_path, "rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    headers = {
        "Accept-Ranges":  "bytes",
        "Content-Length": str(file_size),
    }
    return StreamingResponse(iter_full(), status_code=200,
                             headers=headers, media_type=media_type)


@app.get("/get-song")
async def get_song(title: str):
    """Check DB, download from YouTube if missing, return streamable URL."""

    # 1. Return cached file if it exists
    existing_path = get_song_from_db(title)
    if existing_path and os.path.exists(existing_path):
        filename = os.path.basename(existing_path)
        return {"file_url": f"{BASE_URL}/stream?filename={requests.utils.quote(filename)}", "title": title}

    # 2. Download via yt-dlp (no FFmpeg needed)
    safe_title = sanitize_filename(title)
    output_template = os.path.join(DOWNLOAD_DIR, f"{safe_title}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
        "outtmpl": output_template,
        "quiet": False,
        "no_warnings": False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(f"ytsearch1:{title}", download=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

    # 3. Find the downloaded file
    downloaded_file = None
    for f in sorted(os.listdir(DOWNLOAD_DIR)):
        if f.startswith(safe_title) and not f.endswith(".part") and not f.endswith(".ytdl"):
            downloaded_file = os.path.join(DOWNLOAD_DIR, f)
            break

    if not downloaded_file:
        raise HTTPException(status_code=500, detail="File not found after download.")

    # 4. Save to DB and return the streamable URL
    save_song_to_db(title, downloaded_file)
    filename = os.path.basename(downloaded_file)
    return {"file_url": f"{BASE_URL}/stream?filename={requests.utils.quote(filename)}", "title": title}