import os
import uuid
import tempfile
import random
import traceback
from typing import Optional
from urllib.parse import quote
import unicodedata

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv

# Load environment variables if present
load_dotenv()

app = FastAPI()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("ALLOWED_ORIGIN", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# A small rotation of user agents to reduce trivial bot detection
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36'
]


def ascii_fallback_filename(name: str, max_length: int = 200) -> str:
    """
    Create an ASCII-only fallback filename by normalizing and replacing
    non-ASCII characters with underscores and truncating to max_length.
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = "".join((c if ord(c) < 128 else "_") for c in normalized)
    ascii_only = "_".join(filter(None, (part for part in ascii_only.split("_"))))
    if not ascii_only:
        ascii_only = "video"
    if len(ascii_only) > max_length:
        ascii_only = ascii_only[:max_length]
    return ascii_only


def content_disposition_header(filename: str) -> str:
    """
    Build a Content-Disposition header that includes an ASCII fallback
    filename and an RFC 5987 encoded UTF-8 filename* parameter for Unicode names.
    """
    try:
        filename.encode("latin-1")
    except UnicodeEncodeError:
        ascii_name = ascii_fallback_filename(filename)
        quoted = quote(filename, safe="")
        return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'
    else:
        return f'attachment; filename="{filename}"'


@app.get("/download")
async def download_video(
    url: str = Query(...),
    format: str = Query("best"),
    cookies: Optional[str] = Query(None),
    reencode: bool = Query(False),
):
    """
    Download a video using yt-dlp and stream it back to the client.

    Query params:
      - url: video url (required)
      - format: yt-dlp format string (optional)
      - cookies: path to a cookies file on the server OR raw cookie contents (optional)
      - reencode: bool flag that forces ffmpeg re-encoding to mp4 (H.264 + AAC) if True
    """
    uid = uuid.uuid4().hex[:10]
    temp_dir = tempfile.gettempdir()
    output_template = os.path.join(temp_dir, f"{uid}.%(ext)s")

    # Rotate user-agent
    headers = {"User-Agent": random.choice(USER_AGENTS)}

    # Prefer mp4 formats when possible to avoid VP9/AV1 containers/codecs on iOS
    preferred_format = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    # Build base yt-dlp options
    ydl_opts = {
        "format": preferred_format if format == "best" else format,
        "outtmpl": output_template,
        "quiet": True,
        "merge_output_format": "mp4",
        "retries": 5,
        "continuedl": True,
        "noprogress": True,
        "http_headers": headers,
        "nocheckcertificate": True,
    }

    # If reencode is requested, configure ffmpeg/yt-dlp to recode to mp4 (h264/aac)
    if reencode:
        # Instruct yt-dlp to re-encode the resulting video to mp4 using ffmpeg.
        # 'recode_video' is a shorthand; for finer control you can use postprocessors and postprocessor_args.
        ydl_opts["recode_video"] = "mp4"
        ydl_opts["prefer_ffmpeg"] = True
        # Optional: enforce codecs via postprocessor args (uncomment if needed)
        # ydl_opts["postprocessor_args"] = ["-c:v", "libx264", "-c:a", "aac", "-preset", "fast"]

    # Cookie handling: accept a server-side path or raw cookie text (caution: security)
    tmp_cookie_path = None
    if cookies:
        if os.path.exists(cookies):
            ydl_opts["cookiefile"] = cookies
        else:
            try:
                tmp_cookie_path = os.path.join(temp_dir, f"{uid}.cookies.txt")
                with open(tmp_cookie_path, "w", encoding="utf-8") as cf:
                    cf.write(cookies)
                ydl_opts["cookiefile"] = tmp_cookie_path
            except Exception:
                tmp_cookie_path = None

    actual_file_path = None
    info = None

    try:
        # Try to extract metadata first to get a good title/extension (skip download)
        info_opts = dict(ydl_opts)
        info_opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            info = None

        # Perform the download
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Locate the downloaded file (the outtmpl begins with uid)
        for fname in os.listdir(temp_dir):
            if fname.startswith(uid):
                actual_file_path = os.path.join(temp_dir, fname)
                break

        if not actual_file_path or not os.path.exists(actual_file_path):
            raise HTTPException(status_code=500, detail="Download failed or file not found on server.")

        # If we didn't get good metadata earlier, try to probe the actual filename
        title = None
        extension = None
        if info:
            title = info.get("title")
            extension = info.get("ext")
        if not title:
            title = os.path.splitext(os.path.basename(actual_file_path))[0] or f"video_{uid}"
        if not extension:
            extension = os.path.splitext(actual_file_path)[1].lstrip(".") or "mp4"

        # Sanitize and build filename
        title_safe = title.replace("/", "-").replace("\\", "-")
        filename = f"{title_safe}.{extension}"

        # Build safe Content-Disposition header
        disposition = content_disposition_header(filename)

        async def async_iterfile(chunk_size: int = 64 * 1024):
            try:
                with open(actual_file_path, "rb") as fh:
                    while True:
                        chunk = fh.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
            finally:
                # Clean up the downloaded temporary file
                try:
                    os.unlink(actual_file_path)
                except Exception:
                    pass
                # Remove temporary cookie file if we wrote one
                try:
                    if tmp_cookie_path and os.path.exists(tmp_cookie_path):
                        os.unlink(tmp_cookie_path)
                except Exception:
                    pass

        return StreamingResponse(
            async_iterfile(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": disposition},
        )

    except yt_dlp.utils.DownloadError as e:
        tb = traceback.format_exc()
        print("yt-dlp DownloadError:", tb)
        # Clean up tmp cookie if any
        try:
            if tmp_cookie_path and os.path.exists(tmp_cookie_path):
                os.unlink(tmp_cookie_path)
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"Download error: {str(e)}")
    except Exception as e:
        tb = traceback.format_exc()
        print("Unhandled error during download:", tb)
        # Clean up tmp cookie if any
        try:
            if tmp_cookie_path and os.path.exists(tmp_cookie_path):
                os.unlink(tmp_cookie_path)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Error during download: {str(e)}")


@app.get("/")
async def root():
    return {"message": "Welcome to the Social Media Video Downloader API. Use /download?url=<video_url>&format=<video_format>&reencode=1 to request re-encoding when necessary."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))