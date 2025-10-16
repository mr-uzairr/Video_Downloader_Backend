import os
import uuid
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv
from typing import Optional

app = FastAPI()

# Load environment variables from .env file
load_dotenv()

# CORS configuration
app.add_middleware(CORSMiddleware,
    allow_origins=[os.getenv("ALLOWED_ORIGIN")],  # Adjust this to your needs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/download")
async def download_video(url: str = Query(...), format: str = Query("best"), cookies: Optional[str] = Query(None)):
    try:
        # Extract metadata
        # Use a reasonable User-Agent to reduce 429s
        extract_opts = {
            'quiet': True,
            'skip_download': True,
            'nocheckcertificate': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/116.0.0.0 Safari/537.36',
            }
        }

        # If cookies are provided (as a string of cookie file path on server or raw cookies),
        # pass them to yt-dlp via the 'cookiefile' option. The client can upload a cookie file
        # to the server separately and pass its path here. We keep this optional and small.
        if cookies:
            extract_opts['cookiefile'] = cookies

        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "video").replace("/", "-").replace("\\", "-")
            extension = info.get('ext') or 'mp4'
            filename = f"{title}.{extension}"

        # Create a unique output template
        uid = uuid.uuid4().hex[:8]
        output_template = f"/tmp/{uid}.%(ext)s"

        # Improved download options: retries, rate-limiting disabled, headers
        ydl_opts = {
            'format': format,
            'outtmpl': output_template,
            'quiet': True,
            'merge_output_format': 'mp4',
            'retries': 10,
            'sleep_interval_requests': 0,
            'continuedl': True,
            'noprogress': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/116.0.0.0 Safari/537.36',
            }
        }

        if cookies:
            ydl_opts['cookiefile'] = cookies

        # Download the video using yt-dlp Python API
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.download([url])

        # Find actual downloaded file
        actual_file_path = None
        for f in os.listdir("/tmp"):
            if f.startswith(uid):
                actual_file_path = os.path.join("/tmp", f)
                break

        if not actual_file_path or not os.path.exists(actual_file_path):
            raise HTTPException(status_code=500, detail="Download failed or file not found.")

        # Stream file in buffered chunks and ensure cleanup
        async def async_iterfile(chunk_size: int = 64 * 1024):
            try:
                with open(actual_file_path, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
            finally:
                # Best-effort cleanup
                try:
                    os.unlink(actual_file_path)
                except Exception:
                    pass

        return StreamingResponse(
            async_iterfile(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during download: {str(e)}")

@app.get("/")
async def root():
    return {"message": "Welcome to the Social Media Video Downloader API. Use /download?url=<video_url>&format=<video_format> to download videos."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)