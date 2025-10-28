import os
import uuid
import tempfile
import random
import traceback
from typing import Optional
from urllib.parse import quote
import unicodedata

from fastapi import FastAPI, Query, HTTPException, BackgroundTasks, UploadFile, File, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv
import base64


load_dotenv()

app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("ALLOWED_ORIGIN", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


    headers = {'User-Agent': random.choice(USER_AGENTS)}

  
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
   
    tmp_cookie_path = None

   
    if cookies:
      
        if os.path.exists(cookies):
            ydl_opts['cookiefile'] = cookies
        else:
           
            try:
                tmp_cookie_path = os.path.join(temp_dir, f"{uid}.cookies.txt")
                with open(tmp_cookie_path, "w", encoding="utf-8") as cf:
                    cf.write(cookies)
              
                try:
                    os.chmod(tmp_cookie_path, 0o600)
                except Exception:
                    pass
                ydl_opts['cookiefile'] = tmp_cookie_path
              
                background_tasks.add_task(_safe_remove, tmp_cookie_path)
            except Exception:
              
                tmp_cookie_path = None

  
    if 'cookiefile' not in ydl_opts:
        env_cookie_file = os.getenv('YT_COOKIE_FILE_PATH')
        env_cookie_string_b64 = os.getenv('YT_COOKIE_STRING_B64')
        env_cookie_string = os.getenv('YT_COOKIE_STRING')
        chosen_method = None

      
        if env_cookie_file and os.path.exists(env_cookie_file):
            ydl_opts['cookiefile'] = env_cookie_file
            chosen_method = 'env_file'

     
        elif env_cookie_string_b64:
            try:
                decoded = None
                try:
                    import base64

                    decoded = base64.b64decode(env_cookie_string_b64).decode('utf-8')
                except Exception:
                   
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

      
        cookies_from_browser = os.getenv('YT_COOKIES_FROM_BROWSER')
        if 'cookiefile' not in ydl_opts and cookies_from_browser:
            ydl_opts['cookiesfrombrowser'] = cookies_from_browser
            chosen_method = chosen_method or 'cookiesfrombrowser'

        
        if chosen_method:
            print(f"Cookie method selected: {chosen_method}")

    cookies_from_browser = os.getenv('YT_COOKIES_FROM_BROWSER')
    if 'cookiefile' not in ydl_opts and cookies_from_browser:
     
        ydl_opts['cookiesfrombrowser'] = cookies_from_browser

 
    proxy = os.getenv('YT_PROXY') or os.getenv('HTTP_PROXY') or os.getenv('HTTPS_PROXY')
    if proxy:
        ydl_opts['proxy'] = proxy

    actual_file_path = None
    info = None

    try:
      
        info_opts = dict(ydl_opts)
        info_opts['skip_download'] = True
        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
         
            info = None

      
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
           
            ydl.download([url])

        for fname in os.listdir(temp_dir):
            if fname.startswith(uid):
                actual_file_path = os.path.join(temp_dir, fname)
                break

        if not actual_file_path or not os.path.exists(actual_file_path):
            raise HTTPException(status_code=500, detail="Download failed or file not found on server.")

       
        title = None
        extension = None
        if info:
            title = info.get("title")
            extension = info.get("ext")
        
        if not title:
           
            title = os.path.splitext(os.path.basename(actual_file_path))[0] or f"video_{uid}"
        if not extension:
           
            extension = os.path.splitext(actual_file_path)[1].lstrip(".") or "mp4"

       
        title_safe = title.replace("/", "-").replace("\\", "-")
        filename = f"{title_safe}.{extension}"

      
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
        
        tb = traceback.format_exc()
        print("yt-dlp DownloadError:", tb)
        raise HTTPException(status_code=502, detail=f"Download error: {str(e)}")
    except Exception as e:
        tb = traceback.format_exc()
        print("Unhandled error during download:", tb)
       
        try:
            if tmp_cookie_path and os.path.exists(tmp_cookie_path):
                os.unlink(tmp_cookie_path)
        except Exception:
            pass
       
        raise HTTPException(status_code=500, detail=f"Error during download: {str(e)}")


@app.get("/")
async def root():
    return {"message": "Welcome to the Social Media Video Downloader API. Use /download?url=<video_url>&format=<video_format> to download videos."}


@app.post('/admin/cookies')
async def admin_upload_cookies(background_tasks: BackgroundTasks, file: UploadFile | None = File(None), cookies_b64: str | None = None, authorization: str | None = Header(None)):
    """Admin endpoint to upload or set the server cookie file.

    - Accepts multipart file upload (field `file`) or JSON/form field `cookies_b64` (base64).
    - Requires header: Authorization: Bearer <ADMIN_TOKEN>
    - Writes to file specified by YT_COOKIE_FILE_PATH or default /srv/secrets/youtube_cookies.txt
    """
    admin_token = os.getenv('ADMIN_TOKEN')
    if not admin_token:
        raise HTTPException(status_code=403, detail='Admin token not configured on server.')

    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Missing Authorization header')

    token = authorization.split(' ', 1)[1]
    if token != admin_token:
        raise HTTPException(status_code=403, detail='Invalid admin token')

    dest_path = os.getenv('YT_COOKIE_FILE_PATH') or os.path.join(tempfile.gettempdir(), 'youtube_cookies.txt')


    try:
        if file:
            with open(dest_path, 'wb') as out_f:
                content = await file.read()
                out_f.write(content)
        elif cookies_b64:
            try:
                decoded = base64.b64decode(cookies_b64)
            except Exception:
              
                decoded = cookies_b64.encode('utf-8')
            with open(dest_path, 'wb') as out_f:
                out_f.write(decoded)
        else:
            raise HTTPException(status_code=400, detail='No file or cookies_b64 provided')

        try:
            os.chmod(dest_path, 0o600)
        except Exception:
            pass

       

        print('Admin cookie file updated; stored at', dest_path)
        return {'status': 'ok', 'path': dest_path}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print('Error saving admin cookie file:', tb)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))