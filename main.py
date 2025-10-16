import os
import uuid
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv
from typing import Optional
import tempfile

app = FastAPI()

# Load environment variables
load_dotenv()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("ALLOWED_ORIGIN", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# User-Agent list to rotate headers
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36'
]

import random

@app.get("/download")
async def download_video(url: str = Query(...), format: str = Query("best")):
    try:
        # Generate a unique ID for this download
        uid = uuid.uuid4().hex[:8]

        # Create a temporary file for download
        temp_dir = tempfile.gettempdir()
        output_template = os.path.join(temp_dir, f"{uid}.%(ext)s")

        # Random User-Agent to reduce bot detection
        headers = {
            'User-Agent': random.choice(USER_AGENTS)
        }

        ydl_opts = {
            'format': format,
            'outtmpl': output_template,
            'quiet': True,
            'merge_output_format': 'mp4',
            'retries': 10,
            'continuedl': True,
            'noprogress': True,
            'http_headers': headers,
            'nocheckcertificate': True,
        }

        # Download using yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # Determine actual downloaded file
        actual_file_path = None
        for f in os.listdir(temp_dir):
            if f.startswith(uid):
                actual_file_path = os.path.join(temp_dir, f)
                break

        if not actual_file_path or not os.path.exists(actual_file_path):
            raise HTTPException(status_code=500, detail="Download failed or file not found.")

        # Extract filename for response
        filename = info.get("title", f"video_{uid}").replace("/", "-") + ".mp4"

        # Stream the file and clean up after
        async def async_iterfile(chunk_size: int = 64 * 1024):
            try:
                with open(actual_file_path, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    os.unlink(actual_file_path)
                except Exception:
                    pass

        return StreamingResponse(
            async_iterfile(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=500, detail=f"Download error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/")
async def root():
    return {"message": "Welcome to the Social Media Video Downloader API. Use /download?url=<video_url>&format=<video_format> to download videos."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
