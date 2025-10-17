import os
import uuid
import tempfile
import random
import traceback
from typing import Optional
from urllib.parse import quote
import unicodedata

from fastapi import FastAPI, Query, HTTPException, BackgroundTasks
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


def _safe_remove(path: str) -> None:
    """Best-effort remove a file path; swallow errors."""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


def ascii_fallback_filename(name: str, max_length: int = 200) -> str:
    """
    Create an ASCII-only fallback filename by normalizing and replacing
    non-ASCII characters with underscores and truncating to max_length.
    """
    # Normalize (NFKD) and remove combining marks, convert to ascii where possible
    normalized = unicodedata.normalize("NFKD", name)
    # Keep only ASCII characters; replace others with underscore
    ascii_only = "".join((c if ord(c) < 128 else "_") for c in normalized)
    # Collapse sequences of underscores
    ascii_only = "_".join(filter(None, (part for part in ascii_only.split("_"))))
    if not ascii_only:
        ascii_only = "video"
    # truncate and return
    if len(ascii_only) > max_length:
        ascii_only = ascii_only[:max_length]
    return ascii_only


def content_disposition_header(filename: str) -> str:
    """
    Build a Content-Disposition header that includes an ASCII fallback
    filename and an RFC 5987 encoded UTF-8 filename* parameter for Unicode names.
    """
    # Ensure extension part isn't lost
    # Try latin-1 encoding; if it succeeds, use simple filename param (legacy)
    try:
        filename.encode("latin-1")
    except UnicodeEncodeError:
        # Need RFC 5987 encoding for UTF-8
        ascii_name = ascii_fallback_filename(filename)
        # percent-encode the UTF-8 bytes
        quoted = quote(filename, safe="")
        # include both ascii fallback and UTF-8 encoded filename*
        return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'
    else:
        # latin-1 works, safe to use directly
        return f'attachment; filename="{filename}"'


@app.get("/download")
async def download_video(background_tasks: BackgroundTasks, url: str = Query(...), format: str = Query("best"), cookies: Optional[str] = Query(None)):
    """
    Download a video using yt-dlp and stream it back to the client.

    Optional query param:
      - cookies: path to a cookies file on the server OR raw cookie contents
                 (if your deployment supports passing cookies this way).
                 Use cautiously - prefer server-side cookie file handling or secure upload.
    """
    uid = uuid.uuid4().hex[:10]
    temp_dir = tempfile.gettempdir()
    output_template = os.path.join(temp_dir, f"{uid}.%(ext)s")

    # Rotate user-agent
    headers = {'User-Agent': random.choice(USER_AGENTS)}

    # Basic yt-dlp options
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
        # Do not restrict filename on disk to keep the real extension available
    }
    # Initialize temp cookie path variable (used for cleanup if we create one)
    tmp_cookie_path = None

    # If cookies arg is provided and looks like a path on the server, try to use it.
    # NOTE: for security, validate and sanitize if you accept arbitrary paths from clients.
    if cookies:
        # If the path exists, assume it's a cookiefile path
        if os.path.exists(cookies):
            ydl_opts['cookiefile'] = cookies
        else:
            # If the string contains likely cookie format (a simple heuristic),
            # write it to a temp cookies file for yt-dlp to use.
            try:
                tmp_cookie_path = os.path.join(temp_dir, f"{uid}.cookies.txt")
                with open(tmp_cookie_path, "w", encoding="utf-8") as cf:
                    cf.write(cookies)
                # Restrict permissions to owner read/write only
                try:
                    os.chmod(tmp_cookie_path, 0o600)
                except Exception:
                    pass
                ydl_opts['cookiefile'] = tmp_cookie_path
                # Ensure the temp cookie file is removed after response
                background_tasks.add_task(_safe_remove, tmp_cookie_path)
            except Exception:
                # ignore cookie writing errors and proceed without cookiefile
                tmp_cookie_path = None

    # If the client didn't provide cookies, allow environment-based cookies
    # Auto-select order (most reliable first):
    # 1) YT_COOKIE_FILE_PATH (existing file)
    # 2) YT_COOKIE_STRING_B64 (base64-encoded cookie contents)
    # 3) YT_COOKIE_STRING (raw cookie contents)
    # 4) cookiesfrombrowser (YT_COOKIES_FROM_BROWSER)
    # 5) proxy (YT_PROXY)
    if 'cookiefile' not in ydl_opts:
        env_cookie_file = os.getenv('YT_COOKIE_FILE_PATH')
        env_cookie_string_b64 = os.getenv('YT_COOKIE_STRING_B64')
        env_cookie_string = os.getenv('YT_COOKIE_STRING')
        chosen_method = None

        # 1) file on disk
        if env_cookie_file and os.path.exists(env_cookie_file):
            ydl_opts['cookiefile'] = env_cookie_file
            chosen_method = 'env_file'

        # 2) base64 cookie string
        elif env_cookie_string_b64:
            try:
                decoded = None
                try:
                    import base64

                    decoded = base64.b64decode(env_cookie_string_b64).decode('utf-8')
                except Exception:
                    # fall back to treating it as raw if decode fails
                    decoded = env_cookie_string_b64

                tmp_cookie_path = os.path.join(temp_dir, f"{uid}.env.cookies.txt")
                with open(tmp_cookie_path, "w", encoding="utf-8") as cf:
                    cf.write(decoded)
                try:
                    os.chmod(tmp_cookie_path, 0o600)
                except Exception:
                    pass
                ydl_opts['cookiefile'] = tmp_cookie_path
                background_tasks.add_task(_safe_remove, tmp_cookie_path)
                chosen_method = 'env_b64'
            except Exception:
                tmp_cookie_path = None

        # 3) raw cookie string
        elif env_cookie_string:
            try:
                tmp_cookie_path = os.path.join(temp_dir, f"{uid}.env.cookies.txt")
                with open(tmp_cookie_path, "w", encoding="utf-8") as cf:
                    cf.write(env_cookie_string)
                try:
                    os.chmod(tmp_cookie_path, 0o600)
                except Exception:
                    pass
                ydl_opts['cookiefile'] = tmp_cookie_path
                background_tasks.add_task(_safe_remove, tmp_cookie_path)
                chosen_method = 'env_raw'
            except Exception:
                tmp_cookie_path = None

        # 4) cookiesfrombrowser
        cookies_from_browser = os.getenv('YT_COOKIES_FROM_BROWSER')
        if 'cookiefile' not in ydl_opts and cookies_from_browser:
            ydl_opts['cookiesfrombrowser'] = cookies_from_browser
            chosen_method = chosen_method or 'cookiesfrombrowser'

        # Log chosen non-sensitive method for debugging
        if chosen_method:
            print(f"Cookie method selected: {chosen_method}")

    # Optionally support extracting cookies from a local browser profile (if available on the server)
    # Example: set YT_COOKIES_FROM_BROWSER=chrome or firefox
    cookies_from_browser = os.getenv('YT_COOKIES_FROM_BROWSER')
    if 'cookiefile' not in ydl_opts and cookies_from_browser:
        # yt-dlp supports the 'cookiesfrombrowser' option
        ydl_opts['cookiesfrombrowser'] = cookies_from_browser

    # Optionally support using a proxy (residential proxy recommended for bypassing rate-limits)
    # Example: export YT_PROXY=http://username:pass@host:port
    proxy = os.getenv('YT_PROXY') or os.getenv('HTTP_PROXY') or os.getenv('HTTPS_PROXY')
    if proxy:
        ydl_opts['proxy'] = proxy

    actual_file_path = None
    info = None

    try:
        # First try to extract metadata (safe, skip download) to get title/extension if possible
        info_opts = dict(ydl_opts)
        info_opts['skip_download'] = True
        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            # If metadata extraction fails for some sites, we'll attempt a direct download anyway
            info = None

        # Now perform the download (yt-dlp will write to output_template)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Use download() to fetch and write the file
            ydl.download([url])

        # Find the actual file in temp_dir that starts with uid
        for fname in os.listdir(temp_dir):
            if fname.startswith(uid):
                actual_file_path = os.path.join(temp_dir, fname)
                break

        if not actual_file_path or not os.path.exists(actual_file_path):
            raise HTTPException(status_code=500, detail="Download failed or file not found on server.")

        # Determine filename for Content-Disposition
        title = None
        extension = None
        if info:
            title = info.get("title")
            extension = info.get("ext")
        # Fallbacks
        if not title:
            # derive from actual filename if possible
            title = os.path.splitext(os.path.basename(actual_file_path))[0] or f"video_{uid}"
        if not extension:
            # attempt to infer extension from actual file
            extension = os.path.splitext(actual_file_path)[1].lstrip(".") or "mp4"

        # sanitize simple slash/backslash
        title_safe = title.replace("/", "-").replace("\\", "-")
        filename = f"{title_safe}.{extension}"

        # Build Content-Disposition header safely
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

        return StreamingResponse(
            async_iterfile(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": disposition}
        )

    except yt_dlp.utils.DownloadError as e:
        # Provide helpful detail for client while logging full traceback server-side
        tb = traceback.format_exc()
        print("yt-dlp DownloadError:", tb)
        raise HTTPException(status_code=502, detail=f"Download error: {str(e)}")
    except Exception as e:
        tb = traceback.format_exc()
        print("Unhandled error during download:", tb)
        # If a cookie temp file was written, attempt to remove it
        try:
            if tmp_cookie_path and os.path.exists(tmp_cookie_path):
                os.unlink(tmp_cookie_path)
        except Exception:
            pass
        # Provide the message in detail so clients can surface it (avoid leaking secrets)
        raise HTTPException(status_code=500, detail=f"Error during download: {str(e)}")


@app.get("/")
async def root():
    return {"message": "Welcome to the Social Media Video Downloader API. Use /download?url=<video_url>&format=<video_format> to download videos."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))