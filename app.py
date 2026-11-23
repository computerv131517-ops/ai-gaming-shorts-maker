import os
import base64
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import shutil
import tempfile
import zipfile
import subprocess
import hashlib
import hmac
import secrets
import random
import json
import uuid
import requests
from pathlib import Path
from urllib.parse import urlparse
import yt_dlp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import cv2
import numpy as np
import streamlit as st
import imageio_ffmpeg

st.set_page_config(page_title="AI Gaming Shorts Maker", page_icon="🎮", layout="wide")

# Streamlit Community Cloud storage is temporary. This folder keeps uploads
# available across app reruns/navigation while this app instance remains alive.
UPLOAD_DIR = Path(tempfile.gettempdir()) / "ai_gaming_shorts_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

st.markdown("""
<style>
.main {background: #0b1020;}
.block-container {padding-top: 2rem; max-width: 1100px;}
.hero {padding: 1.5rem; border: 1px solid #26334f; border-radius: 18px;
       background: linear-gradient(120deg,#17233c,#101827);}
.hero h1 {margin:0; color:#fff;}
.hero p {color:#b9c6dc; margin-bottom:0;}
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="hero"><h1>🎮 AI Gaming Shorts Maker</h1>'
    '<p>Upload first. Create Shorts only when you press the processing button.</p></div>',
    unsafe_allow_html=True
)
st.write("")

if "page" not in st.session_state:
    st.session_state.page = "upload"
if "selected_video" not in st.session_state:
    st.session_state.selected_video = ""
if "background_job_id" not in st.session_state:
    st.session_state.background_job_id = st.query_params.get("bg_job", "")

UPLOAD_RETENTION_SECONDS = 60 * 60  # Automatically remove saved source videos after 1 hour.

def cleanup_expired_uploads():
    now = time.time()
    video_extensions = {".mp4", ".mov", ".mkv", ".avi"}
    for path in UPLOAD_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in video_extensions:
            try:
                if now - path.stat().st_mtime >= UPLOAD_RETENTION_SECONDS:
                    path.unlink()
                    if st.session_state.get("selected_video") == str(path):
                        st.session_state.selected_video = ""
            except FileNotFoundError:
                pass

def list_saved_videos():
    cleanup_expired_uploads()
    return sorted(
        [p for p in UPLOAD_DIR.iterdir() if p.is_file() and p.suffix.lower() in
         {".mp4", ".mov", ".mkv", ".avi"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

def save_uploaded_video(uploaded_file):
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(uploaded_file.name).name)
    digest = hashlib.sha256(uploaded_file.getbuffer()).hexdigest()[:12]
    destination = UPLOAD_DIR / f"{digest}_{safe_name}"

    if destination.exists():
        return destination

    uploaded_file.seek(0)
    total = max(int(uploaded_file.size), 1)
    progress = st.progress(0, text="Saving original video without compression...")
    written = 0
    save_started = time.time()
    with open(destination, "wb") as target:
        while True:
            chunk = uploaded_file.read(8 * 1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
            written += len(chunk)
            fraction = min(written / total, 1.0)
            elapsed = time.time() - save_started
            eta = (elapsed / max(fraction, 0.01)) * (1 - fraction)
            progress.progress(fraction, text=f"Saving original video: {fraction * 100:.1f}% · approx. {_format_wait(eta)} remaining")
    progress.empty()
    return destination

def delete_source_video_after_download(source_path):
    """Remove the original upload after a generated clip/ZIP is requested for download."""
    try:
        source = Path(source_path)
        if source.is_file():
            source.unlink()
        # Keep the path in session state so the app can still locate the generated clips.
        st.session_state.source_deleted_after_download = True
    except FileNotFoundError:
        st.session_state.source_deleted_after_download = True
    except OSError as error:
        st.session_state.source_delete_error = str(error)


def _format_wait(seconds):
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"



def background_config():
    """Read private worker credentials from Streamlit Cloud Secrets."""
    try:
        return {
            "supabase_url": str(st.secrets.get("SUPABASE_URL", "")).strip().rstrip("/"),
            "supabase_key": str(st.secrets.get("SUPABASE_SECRET_KEY", "")).strip(),
            "github_token": str(st.secrets.get("GITHUB_TOKEN", "")).strip(),
        }
    except Exception:
        return {"supabase_url": "", "supabase_key": "", "github_token": ""}


def background_headers(config):
    key = config["supabase_key"]
    return {"apikey": key, "Authorization": "Bearer " + key}


def submit_background_job(video_path, category):
    """Upload the source to private Supabase Storage and dispatch one GitHub runner."""
    config = background_config()
    if not all(config.values()):
        raise RuntimeError("Background worker secrets missing hain. Streamlit Secrets mein SUPABASE_URL, SUPABASE_SECRET_KEY aur GITHUB_TOKEN check karo.")
    job_id = uuid.uuid4().hex
    object_path = f"jobs/{job_id}/input.mp4"
    upload_url = config["supabase_url"] + "/storage/v1/object/gaming-videos/" + object_path
    headers = background_headers(config)
    headers.update({"Content-Type": "video/mp4", "x-upsert": "true"})
    size = Path(video_path).stat().st_size
    with open(video_path, "rb") as source:
        response = requests.post(upload_url, headers=headers, data=source, timeout=(30, 1800))
    response.raise_for_status()

    dispatch_url = "https://api.github.com/repos/computerv131517-ops/ai-gaming-shorts-maker/actions/workflows/process-video.yml/dispatches"
    github_headers = {
        "Authorization": "Bearer " + config["github_token"],
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    dispatch = requests.post(
        dispatch_url, headers=github_headers,
        json={"ref": "main", "inputs": {"job_id": job_id, "category": str(category)}},
        timeout=30
    )
    if dispatch.status_code not in (200, 201, 204):
        raise RuntimeError(f"GitHub worker start nahi hua (HTTP {dispatch.status_code}): {dispatch.text[:350]}")
    return job_id, size


def fetch_background_status(job_id):
    config = background_config()
    if not config["supabase_url"] or not config["supabase_key"]:
        raise RuntimeError("Supabase Secrets missing hain.")
    url = config["supabase_url"] + f"/storage/v1/object/authenticated/gaming-videos/jobs/{job_id}/status.json"
    response = requests.get(url, headers=background_headers(config), timeout=30)
    if response.status_code == 404:
        return {"state": "queued", "message": "Worker start hone ka wait ho raha hai."}
    response.raise_for_status()
    return response.json()


def download_background_thumbnails(job_id, thumbnail_paths):
    config = background_config()
    target_dir = UPLOAD_DIR / "background_jobs" / job_id / "thumbnails"
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for remote_path in thumbnail_paths:
        name = Path(remote_path).name
        destination = target_dir / name
        if not destination.exists():
            url = config["supabase_url"] + f"/storage/v1/object/authenticated/gaming-videos/jobs/{job_id}/thumbnails/{name}"
            response = requests.get(url, headers=background_headers(config), timeout=(30, 180))
            response.raise_for_status()
            destination.write_bytes(response.content)
        downloaded.append(destination)
    return downloaded


def download_background_clips(job_id, clip_paths):
    config = background_config()
    target_dir = UPLOAD_DIR / "background_jobs" / job_id / "clips"
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for remote_path in clip_paths:
        name = Path(remote_path).name
        destination = target_dir / name
        if not destination.exists():
            url = config["supabase_url"] + f"/storage/v1/object/authenticated/gaming-videos/jobs/{job_id}/clips/{name}"
            response = requests.get(url, headers=background_headers(config), timeout=(30, 900))
            response.raise_for_status()
            destination.write_bytes(response.content)
        downloaded.append(destination)
    return downloaded


def download_video_from_url(video_url):
    """Download a public video with live percentage and ETA when available."""
    parsed = urlparse(video_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Valid public http/https video link paste karo.")

    host = parsed.hostname.lower().split(":")[0]
    allowed_domains = (
        "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
        "twitch.tv", "streamable.com"
    )
    if not any(host == domain or host.endswith("." + domain) for domain in allowed_domains):
        raise ValueError(
            "Abhi supported sites: YouTube, Vimeo, Dailymotion, Twitch aur Streamable. "
            "Private, login-only, paywalled ya DRM-protected videos supported nahi hain."
        )

    url_hash = hashlib.sha256(video_url.strip().encode("utf-8")).hexdigest()[:12]
    output_template = str(UPLOAD_DIR / f"url_{url_hash}.%(ext)s")
    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "max_filesize": 1024 * 1024 * 1024,
        "merge_output_format": "mp4",
        # yt-dlp needs an ffmpeg executable to merge separate video/audio streams.
        # imageio-ffmpeg is already a project dependency and supplies its path.
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
    }
    # Do not force a YouTube player client; let the current yt-dlp extractor
    # select supported clients and formats for this request.
    download_progress = st.progress(0, text="Link se video details mil rahi hain...")
    download_started = time.time()

    def _download_hook(data):
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes", 0)
            if total:
                fraction = min(max(float(done) / max(float(total), 1), 0), 1)
                speed = data.get("speed") or 0
                eta = data.get("eta")
                if eta is None and speed:
                    eta = max(0, (total - done) / speed)
                eta_text = _format_wait(eta) if eta is not None else "calculating..."
                download_progress.progress(fraction, text=f"Video download: {fraction * 100:.1f}% · approx. {eta_text} remaining")
            else:
                elapsed = _format_wait(time.time() - download_started)
                download_progress.progress(0, text=f"Video download chal raha hai · elapsed {elapsed} · total size unavailable")
        elif status == "finished":
            download_progress.progress(1.0, text="Download complete · file merge/verification chal rahi hai...")

    options["progress_hooks"] = [_download_hook]
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(video_url.strip(), download=True)
            downloaded = Path(downloader.prepare_filename(info))
        if not downloaded.exists():
            mp4_path = downloaded.with_suffix(".mp4")
            if mp4_path.exists():
                downloaded = mp4_path
        if not downloaded.exists() or downloaded.stat().st_size == 0:
            raise RuntimeError("Video download complete nahi hui. Link public aur supported hona chahiye.")
        download_progress.progress(1.0, text="✅ Video download 100% complete")
        return downloaded
    except Exception as error:
        download_progress.empty()
        error_text = str(error)
        if "HTTP Error 403" in error_text or "403: Forbidden" in error_text:
            message = (
                "YouTube ne video-data request ko HTTP 403 se reject kar diya. "
                "Ye hosting/IP restriction ya video access limitation ho sakti hai; "
                "app ise bypass nahi karegi. Agar video aapki hai ya aapke paas permission hai, "
                "toh original video file upload karke clipping karein. "
                "Details: " + error_text[:350]
            )
        else:
            message = (
                "Video fetch nahi ho paayi. Link check karo; kuch sites download ko restrict karti hain "
                "ya format change kar deti hain. Details: " + error_text[:500]
            )
        raise RuntimeError(message) from error

YOUTUBE_UPLOAD_SCOPE = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
YOUTUBE_REDIRECT_URI = "https://ai-gaming-shorts-maker.streamlit.app"


def youtube_oauth_config():
    try:
        cfg = st.secrets.get("google_oauth", {})
        client_id = cfg.get("client_id", "")
        client_secret = cfg.get("client_secret", "")
    except Exception:
        return None
    if not client_id or not client_secret:
        return None
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [YOUTUBE_REDIRECT_URI],
        }
    }


def make_youtube_oauth_state(client_secret):
    # Signed state survives Google's redirect even if Streamlit creates a new session.
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(
        client_secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return nonce + "." + signature


def valid_youtube_oauth_state(state, client_secret):
    if not state or "." not in state:
        return False
    nonce, supplied_signature = state.rsplit(".", 1)
    expected_signature = hmac.new(
        client_secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


def _youtube_token_cipher():
    """
    Derive an encryption key from the private Supabase service key.
    The OAuth token is encrypted before it is stored in the private bucket.
    """
    config = background_config()
    secret = config.get("supabase_key", "")
    if not secret:
        raise RuntimeError("Persistent YouTube login ke liye Supabase Secrets configure karo.")
    derived = hashlib.sha256(
        (secret + "|ai-gaming-shorts-maker|youtube-oauth-v1").encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def save_persistent_youtube_credentials(token_json):
    config = background_config()
    if not config.get("supabase_url") or not config.get("supabase_key"):
        raise RuntimeError("Streamlit Secrets mein SUPABASE_URL aur SUPABASE_SECRET_KEY missing hain.")
    encrypted = _youtube_token_cipher().encrypt(token_json.encode("utf-8"))
    url = config["supabase_url"] + "/storage/v1/object/gaming-videos/youtube-auth/credentials.enc"
    headers = background_headers(config)
    headers.update({
        "Content-Type": "application/octet-stream",
        "x-upsert": "true",
    })
    response = requests.post(url, headers=headers, data=encrypted, timeout=30)
    response.raise_for_status()


def load_persistent_youtube_credentials():
    config = background_config()
    if not config.get("supabase_url") or not config.get("supabase_key"):
        return None
    url = config["supabase_url"] + "/storage/v1/object/authenticated/gaming-videos/youtube-auth/credentials.enc"
    response = requests.get(url, headers=background_headers(config), timeout=20)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    try:
        return _youtube_token_cipher().decrypt(response.content).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as error:
        raise RuntimeError("Saved YouTube login decrypt nahi ho paaya. Google account dobara connect karo.") from error


def delete_persistent_youtube_credentials():
    config = background_config()
    if not config.get("supabase_url") or not config.get("supabase_key"):
        return
    url = config["supabase_url"] + "/storage/v1/object/gaming-videos/youtube-auth/credentials.enc"
    response = requests.delete(url, headers=background_headers(config), timeout=20)
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


def restore_persistent_youtube_credentials():
    if st.session_state.get("youtube_credentials"):
        return
    try:
        token_json = load_persistent_youtube_credentials()
        if token_json:
            st.session_state.youtube_credentials = token_json
            st.session_state.youtube_persistence_error = None
    except Exception as error:
        st.session_state.youtube_persistence_error = str(error)[:300]


def handle_youtube_oauth_callback():
    code = st.query_params.get("code")
    returned_state = st.query_params.get("state")
    if not code:
        return
    config = youtube_oauth_config()
    client_secret = config["web"]["client_secret"] if config else ""
    if not config or not valid_youtube_oauth_state(returned_state, client_secret):
        st.session_state.youtube_oauth_error = (
            "Google sign-in state verification failed. Please return to the app and try Connect YouTube again."
        )
        st.query_params.clear()
        return
    try:
        flow = Flow.from_client_config(
            config, scopes=YOUTUBE_UPLOAD_SCOPE, state=returned_state,
            autogenerate_code_verifier=False
        )
        flow.redirect_uri = YOUTUBE_REDIRECT_URI
        flow.fetch_token(code=code)
        token_json = flow.credentials.to_json()
        st.session_state.youtube_credentials = token_json
        save_persistent_youtube_credentials(token_json)
        st.session_state.youtube_persistence_error = None
        st.session_state.youtube_oauth_error = None
    except Exception as error:
        st.session_state.youtube_oauth_error = "Google connection/persistent save failed: " + str(error)[:300]
    st.query_params.clear()
    st.rerun()


def get_youtube_service():
    token_json = st.session_state.get("youtube_credentials")
    if not token_json:
        token_json = load_persistent_youtube_credentials()
        if token_json:
            st.session_state.youtube_credentials = token_json
    if not token_json:
        return None
    credentials = Credentials.from_authorized_user_info(
        json.loads(token_json), scopes=YOUTUBE_UPLOAD_SCOPE
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_json = credentials.to_json()
        st.session_state.youtube_credentials = token_json
        save_persistent_youtube_credentials(token_json)
    if not credentials.valid:
        raise RuntimeError("YouTube session expire ho gaya. Google account dobara connect karo.")
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def get_gemini_api_key():
    # A key entered in the sidebar is kept only in this Streamlit session.
    session_key = st.session_state.get("gemini_api_key_input", "").strip()
    if session_key:
        return session_key
    try:
        return st.secrets.get('gemini', {}).get('api_key', '') or st.secrets.get('GEMINI_API_KEY', '')
    except Exception:
        return os.environ.get('GEMINI_API_KEY', '')


def load_video_metadata(job_dir):
    try:
        with open(Path(job_dir) / 'video_metadata.json', 'r', encoding='utf-8') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def generate_basic_video_metadata(video_path, job_dir, category):
    """Create simple shared metadata locally, without an external AI key."""
    existing = load_video_metadata(job_dir)
    if existing:
        return existing

    category_key = str(category or "Auto (all categories)").strip().lower()
    source_name = Path(video_path).stem
    source_name = re.sub(r"[_-]+", " ", source_name)
    source_name = re.sub(r"[^A-Za-z0-9 ]+", " ", source_name)
    source_name = re.sub(r"\\s+", " ", source_name).strip()
    if not source_name or re.fullmatch(r"[0-9a-fA-F]{8,}", source_name):
        source_name = ""

    category_data = {
        "gaming": ("Gaming Highlights", ["#Gaming", "#Gameplay", "#Gamer", "#Shorts"]),
        "sports": ("Sports Highlights", ["#Sports", "#Highlights", "#SportsShorts", "#Shorts"]),
        "vlog / travel": ("Vlog & Travel Highlights", ["#Vlog", "#Travel", "#Explore", "#Shorts"]),
        "podcast / talking": ("Podcast Highlights", ["#Podcast", "#Talking", "#Clips", "#Shorts"]),
        "education / tutorial": ("Learning Highlights", ["#Education", "#Learning", "#Tutorial", "#Shorts"]),
        "reaction / comedy": ("Funny & Reaction Highlights", ["#Reaction", "#Funny", "#Comedy", "#Shorts"]),
        "other": ("Video Highlights", ["#Highlights", "#Video", "#Clips", "#Shorts"]),
        "auto (all categories)": ("Video Highlights", ["#Highlights", "#Clips", "#Trending", "#Shorts"]),
    }
    label, hashtags = category_data.get(category_key, category_data["auto (all categories)"])
    subject = source_name[:55].strip()
    title = f"{subject} | {label} #Shorts" if subject else f"{label} #Shorts"
    title = title[:95]
    description_subject = f" from {subject}" if subject else ""
    description = (
        f"Watch these {label.lower()}{description_subject}. "
        "This video has been split into short clips for easy viewing. "
        "Enjoy the highlights!"
    )
    metadata = {"title": title, "description": description, "hashtags": hashtags}
    save_json_atomic(Path(job_dir) / "video_metadata.json", metadata)
    return metadata


def generate_video_metadata(video_path, job_dir, category):
    existing = load_video_metadata(job_dir)
    if existing:
        return existing
    api_key = get_gemini_api_key()
    if not api_key:
        return generate_basic_video_metadata(video_path, job_dir, category)
    from google import genai
    client = genai.Client(api_key=api_key)
    uploaded_file = None
    try:
        st.info('AI poori original video analyse karke common metadata bana raha hai. Ismein kuch waqt lag sakta hai.')
        wait_started = time.time()
        wait_status = st.status("⏳ Gemini video processing ka wait ho raha hai...", expanded=True)
        uploaded_file = client.files.upload(file=str(video_path))
        started = time.time()
        while not uploaded_file.state or uploaded_file.state.name != 'ACTIVE':
            if uploaded_file.state and uploaded_file.state.name == 'FAILED':
                raise RuntimeError('Gemini video processing fail ho gayi.')
            if time.time() - started > 1800:
                raise TimeoutError('Video analysis 30 minutes se zyada le rahi hai. Dobara try karo.')
            elapsed_text = _format_wait(time.time() - wait_started)
            wait_status.update(label=f"⏳ Gemini processing... · elapsed {elapsed_text} · provider exact % share nahi karta", state="running")
            time.sleep(5)
            uploaded_file = client.files.get(name=uploaded_file.name)
        wait_status.update(label="✅ Gemini video processing complete", state="complete")
        prompt = f'''Analyze the ENTIRE uploaded video, including visual scenes and available audio context.
Selected category: {category}.
Create ONE shared metadata set for all Shorts cut from this source video.
Return ONLY valid JSON with keys title, description, hashtags.
title: catchy, accurate YouTube Shorts title, max 95 characters.
description: engaging 2-4 sentence summary; do not invent events, names, scores or claims.
hashtags: array of 5-8 relevant hashtags, each starting with #. Include #Shorts.
If the subject/game is unclear, stay general and accurate.'''
        response = client.models.generate_content(model='gemini-3.8-flash', contents=[uploaded_file, prompt])
        raw = (response.text or '').strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
            if raw.lower().startswith('json'):
                raw = raw[4:].strip()
        data = json.loads(raw)
        title = str(data.get('title', '')).strip()[:95]
        description = str(data.get('description', '')).strip()
        tags = data.get('hashtags', [])
        if isinstance(tags, str):
            tags = tags.split()
        tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        tags = [tag if tag.startswith('#') else '#' + tag.lstrip('#') for tag in tags[:8]]
        if not title or not description or not tags:
            raise ValueError('AI response mein title, description ya hashtags missing hain.')
        metadata = {'title': title, 'description': description, 'hashtags': tags}
        save_json_atomic(Path(job_dir) / 'video_metadata.json', metadata)
        return metadata
    finally:
        if uploaded_file is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass


def show_video_metadata(metadata):
    if not metadata:
        return
    st.markdown('### ✨ AI-generated common YouTube metadata')
    st.caption('Ek hi metadata set is original video ki sabhi generated clips ke liye hai.')
    st.markdown('**Title**')
    st.code(metadata.get('title', ''))
    st.markdown('**Description**')
    st.code(metadata.get('description', ''))
    st.markdown('**Hashtags**')
    st.code(' '.join(metadata.get('hashtags', [])))

def get_next_youtube_publish_time(youtube):
    """Continue the channel-wide queue, with a random 3.5–5 hour interval."""
    now = datetime.now(timezone.utc)
    latest_future = None
    channel_response = youtube.channels().list(
        part="contentDetails", mine=True, maxResults=1
    ).execute()
    channels = channel_response.get("items", [])
    if channels:
        uploads_playlist = (
            channels[0].get("contentDetails", {})
            .get("relatedPlaylists", {}).get("uploads")
        )
        page_token = None
        while uploads_playlist:
            page = youtube.playlistItems().list(
                part="contentDetails", playlistId=uploads_playlist,
                maxResults=50, pageToken=page_token
            ).execute()
            video_ids = [
                item.get("contentDetails", {}).get("videoId")
                for item in page.get("items", [])
                if item.get("contentDetails", {}).get("videoId")
            ]
            for offset in range(0, len(video_ids), 50):
                details = youtube.videos().list(
                    part="status", id=",".join(video_ids[offset:offset + 50])
                ).execute()
                for item in details.get("items", []):
                    publish_at = item.get("status", {}).get("publishAt")
                    if not publish_at:
                        continue
                    try:
                        scheduled = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if scheduled > now and (latest_future is None or scheduled > latest_future):
                        latest_future = scheduled
            page_token = page.get("nextPageToken")
            if not page_token:
                break

    # Keep one shared queue across different source videos/jobs on this app instance.
    queue_path = UPLOAD_DIR / "youtube_global_queue.json"
    try:
        with open(queue_path, "r", encoding="utf-8") as file:
            queue_data = json.load(file)
        saved_last = datetime.fromisoformat(queue_data["last_publish_at"].replace("Z", "+00:00"))
        if saved_last > now and (latest_future is None or saved_last > latest_future):
            latest_future = saved_last
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass

    base = latest_future if latest_future and latest_future > now else now
    return base + timedelta(minutes=random.randint(210, 300))


def generate_ai_thumbnail(clip_path, job_dir, title_hint=""):
    """Generate a 16:9 AI thumbnail using a representative frame from this clip."""
    api_key = get_gemini_api_key()
    if not api_key:
        return None
    thumbnail_dir = Path(job_dir) / "thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_path = thumbnail_dir / (Path(clip_path).stem + "_ai.jpg")
    if thumbnail_path.is_file() and thumbnail_path.stat().st_size > 2048:
        return thumbnail_path

    capture = cv2.VideoCapture(str(clip_path))
    if not capture.isOpened():
        return None
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_count * 0.45)))
    ok, frame = capture.read()
    capture.release()
    if not ok:
        return None
    frame = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        return None

    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key)
    prompt = (
        "Create a polished, highly clickable YouTube gaming video thumbnail in 16:9 landscape. "
        "Use the supplied gameplay frame as the visual reference for the same game scene and mood. "
        "Make the central action clear, cinematic, vibrant, high contrast, crisp and professional. "
        "Do not add logos, watermarks, fake characters, fake game UI, or any text/letters. "
        "Do not misrepresent the event. Thumbnail context/title: " + str(title_hint)[:180]
    )
    response = client.models.generate_content(
        model="gemini-3.1-flash-image",
        contents=[
            prompt,
            types.Part.from_bytes(data=encoded.tobytes(), mime_type="image/jpeg"),
        ],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="16:9", image_size="1K"),
        ),
    )
    for part in response.parts:
        if getattr(part, "inline_data", None):
            generated = part.as_image()
            generated.save(str(thumbnail_path))
            if thumbnail_path.is_file() and thumbnail_path.stat().st_size > 2048:
                return thumbnail_path
    return None


def apply_youtube_thumbnail(youtube, video_id, clip_path, job_dir, title_hint=""):
    """Try to generate and set a custom thumbnail; upload remains valid if this step fails."""
    try:
        thumbnail_path = generate_ai_thumbnail(clip_path, job_dir, title_hint)
        if thumbnail_path:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg")
            ).execute()
            return True
    except Exception as thumbnail_error:
        st.warning(
            "Video upload ho gayi, lekin AI thumbnail set nahi ho paaya: "
            + str(thumbnail_error)[:240]
        )
    return False


def upload_clip_now_to_youtube(youtube, clip_path, status_path, metadata=None):
    """Upload one history clip immediately as Private, without a scheduled publishAt."""
    clip_path = Path(clip_path)
    if not clip_path.is_file() or clip_path.stat().st_size == 0:
        raise RuntimeError(f"{clip_path.name} missing hai ya empty hai.")

    try:
        with open(status_path, "r", encoding="utf-8") as file:
            uploaded = json.load(file)
        if not isinstance(uploaded, dict):
            uploaded = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        uploaded = {}

    saved = uploaded.get(clip_path.name)
    saved_id = saved.get("video_id") if isinstance(saved, dict) else saved
    if saved_id:
        return saved_id, "already uploaded"

    meta = metadata or {}
    body = {
        "snippet": {
            "title": ((meta.get("title") or clip_path.stem.replace("_", " ") + " #Shorts"))[:100],
            "description": ((meta.get("description") or "Created with AI Gaming Shorts Maker.")
                            + "\n\n" + " ".join(meta.get("hashtags", [])))[:5000],
            "categoryId": "20",
            "tags": [str(tag).strip().lstrip("#") for tag in (meta.get("hashtags", []) or [])
                     if str(tag).strip()][:30],
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(
        str(clip_path), mimetype="video/mp4",
        chunksize=8 * 1024 * 1024, resumable=True
    )
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media
    )
    response = None
    while response is None:
        _, response = request.next_chunk(num_retries=3)

    video_id = response.get("id") if response else None
    if not video_id:
        raise RuntimeError("YouTube upload ke baad video ID nahi mila.")

    uploaded[clip_path.name] = {
        "video_id": video_id,
        "publish_at": None,
        "upload_mode": "direct_private",
    }
    save_json_atomic(status_path, uploaded)
    return video_id, "uploaded privately"


def upload_clips_to_youtube(youtube, clip_paths, status_path, metadata=None):
    """Upload at most two pending clips per run and leave the rest queued locally."""
    try:
        with open(status_path, "r", encoding="utf-8") as file:
            uploaded = json.load(file)
        if not isinstance(uploaded, dict):
            uploaded = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        uploaded = {}

    pending = []
    for candidate in clip_paths:
        candidate = Path(candidate)
        saved = uploaded.get(candidate.name)
        saved_id = saved.get("video_id") if isinstance(saved, dict) else saved
        if candidate.is_file() and not saved_id:
            pending.append(candidate)

    if not pending:
        st.info("Queue mein koi pending clip nahi hai.")
        return []

    batch = pending[:2]
    remaining = max(0, len(pending) - len(batch))
    results = []
    status_box = st.status(
        f"⏳ Is batch mein {len(batch)} tak videos upload/schedule hongi...",
        expanded=True
    )
    progress = st.progress(0, text=f"Queue batch · 0/{len(batch)}")
    upload_started = time.time()
    try:
        next_publish = get_next_youtube_publish_time(youtube)
        status_box.write(
            f"📅 First slot: {next_publish.astimezone(ZoneInfo('Asia/Kolkata')).strftime('%d %b %Y, %I:%M %p IST')}"
        )
        for index, clip_path in enumerate(batch, start=1):
            if not clip_path.is_file() or clip_path.stat().st_size == 0:
                raise RuntimeError(f"{clip_path.name} missing hai ya empty hai.")

            publish_at = next_publish.isoformat(timespec="seconds").replace("+00:00", "Z")
            local_slot = next_publish.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
            meta = metadata or {}
            body = {
                "snippet": {
                    "title": (meta.get("title") or clip_path.stem.replace("_", " ") + " #Shorts")[:100],
                    "description": (
                        (meta.get("description") or "Created with AI Gaming Shorts Maker.")
                        + "\n\n" + " ".join(meta.get("hashtags", []))
                    )[:5000],
                    "categoryId": "20",
                    "tags": [
                        str(tag).strip().lstrip("#")
                        for tag in (meta.get("hashtags", []) or [])
                        if str(tag).strip()
                    ][:30],
                },
                "status": {
                    "privacyStatus": "private",
                    "publishAt": publish_at,
                    "selfDeclaredMadeForKids": False,
                },
            }

            status_box.write(f"🔄 **{clip_path.name}** — Upload ho rahi hai; publish slot: **{local_slot}**")
            media = MediaFileUpload(
                str(clip_path), mimetype="video/mp4",
                chunksize=8 * 1024 * 1024, resumable=True
            )
            request = youtube.videos().insert(
                part="snippet,status", body=body, media_body=media
            )
            response = None
            while response is None:
                chunk_status, response = request.next_chunk(num_retries=3)
                if chunk_status is not None:
                    fraction = ((index - 1) + float(chunk_status.progress())) / len(batch)
                    elapsed = time.time() - upload_started
                    eta = (elapsed / max(fraction, 0.01)) * (1 - fraction)
                    progress.progress(
                        min(fraction, 1.0),
                        text=f"Uploading {clip_path.name}: {chunk_status.progress() * 100:.0f}% · approx. {_format_wait(eta)} remaining"
                    )

            video_id = response.get("id") if response else None
            if not video_id:
                raise RuntimeError(f"{clip_path.name} upload ke baad YouTube video ID nahi mila.")

            # Save upload state first. If thumbnail generation fails, the successful
            # video upload is still recorded and will never be sent twice.
            uploaded[clip_path.name] = {
                "video_id": video_id,
                "publish_at": publish_at,
                "thumbnail_status": "pending",
            }
            save_json_atomic(status_path, uploaded)

            thumb_ok = apply_youtube_thumbnail(
                youtube, video_id, clip_path, Path(status_path).parent,
                meta.get("title", "")
            )
            uploaded[clip_path.name]["thumbnail_status"] = "set" if thumb_ok else "not_set"
            save_json_atomic(status_path, uploaded)

            # Delete the local clip only after YouTube has confirmed the upload.
            try:
                clip_path.unlink()
            except OSError as delete_error:
                st.warning(f"{clip_path.name} upload ho gayi, local copy delete nahi hui: {delete_error}")

            # Shared channel queue checkpoint, used when another gameplay source is processed.
            queue_path = UPLOAD_DIR / "youtube_global_queue.json"
            save_json_atomic(queue_path, {"last_publish_at": publish_at, "last_video_id": video_id})

            results.append((clip_path.name, video_id, f"scheduled · {local_slot}"))
            status_box.write(f"✅ **{clip_path.name}** — Scheduled: **{local_slot}**")
            status_box.write(f"https://www.youtube.com/watch?v={video_id}")
            status_box.write("🖼️ AI thumbnail set." if thumb_ok else "ℹ️ Thumbnail unavailable; video upload is saved.")
            progress.progress(index / len(batch), text=f"Uploaded · {index}/{len(batch)}")
            if index < len(batch):
                next_publish += timedelta(minutes=random.randint(210, 300))

        if remaining:
            status_box.write(f"📦 {remaining} clip(s) abhi queue mein saved hain. Agle batch ke liye baad mein button dabana.")
        status_box.update(
            label=f"✅ Batch complete — {len(results)} video(s) uploaded/scheduled",
            state="complete", expanded=True
        )
        progress.progress(1.0, text=f"✅ Batch complete · {len(results)} uploaded · {remaining} queued")
    except Exception as error:
        status_box.update(label="⚠️ Upload/scheduling ruk gaya — pending clips safe rakhi gayi hain.", state="error", expanded=True)
        error_text = str(error)
        if "quotaExceeded" in error_text:
            st.error("YouTube API quota khatam ho gayi hai. Pending clips delete nahi ki gayi hain.")
        elif "uploadLimitExceeded" in error_text:
            st.error("YouTube channel upload limit reach ho gayi. Pending clips safe hain; limit clear hone par next batch try karna.")
        else:
            st.error(f"YouTube upload/scheduling error: {error_text[:500]}")
    return results

def save_json_atomic(path, data):
    temp_path = str(path) + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file)
    os.replace(temp_path, path)


# Handle the Google redirect only after all helper functions are defined.
handle_youtube_oauth_callback()
restore_persistent_youtube_credentials()

def get_job_dir(video_path, vertical, category):
    digest = hashlib.sha256()
    with open(video_path, "rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    settings = f"viral-edit-semantic-variable-duration-v3|{int(vertical)}|{category}"
    job_id = hashlib.sha256((digest.hexdigest() + settings).encode()).hexdigest()[:20]
    folder = UPLOAD_DIR / "processing_jobs" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder

CLIP_HISTORY_RETENTION_SECONDS = 60 * 60

def cleanup_expired_clip_history():
    """Remove generated clip jobs one hour after their newest clip was created."""
    jobs_root = UPLOAD_DIR / "processing_jobs"
    if not jobs_root.exists():
        return
    now = time.time()
    for job_dir in jobs_root.iterdir():
        if not job_dir.is_dir():
            continue
        clips_dir = job_dir / "clips"
        clips = list(clips_dir.glob("Gaming_Short_*.mp4")) if clips_dir.exists() else []
        if not clips:
            continue
        newest_clip_time = max(path.stat().st_mtime for path in clips)
        # Never expire a job while any clip is still waiting for YouTube upload.
        status_path = job_dir / "youtube_uploads.json"
        try:
            with open(status_path, "r", encoding="utf-8") as file:
                saved_uploads = json.load(file)
            if not isinstance(saved_uploads, dict):
                saved_uploads = {}
        except (OSError, ValueError, json.JSONDecodeError):
            saved_uploads = {}
        pending_clips = []
        for clip in clips:
            saved = saved_uploads.get(clip.name)
            saved_id = saved.get("video_id") if isinstance(saved, dict) else saved
            if not saved_id:
                pending_clips.append(clip)
        if pending_clips:
            continue
        if now - newest_clip_time >= CLIP_HISTORY_RETENTION_SECONDS:
            shutil.rmtree(job_dir, ignore_errors=True)


def list_clip_history():
    cleanup_expired_clip_history()
    jobs_root = UPLOAD_DIR / "processing_jobs"
    if not jobs_root.exists():
        return []
    history = []
    for job_dir in jobs_root.iterdir():
        clips_dir = job_dir / "clips"
        if not job_dir.is_dir() or not clips_dir.exists():
            continue
        clips = sorted(clips_dir.glob("Gaming_Short_*.mp4"))
        if clips:
            newest_clip_time = max(path.stat().st_mtime for path in clips)
            history.append({
                "job_dir": job_dir,
                "clips": clips,
                "created_at": min(path.stat().st_mtime for path in clips),
                "expires_at": newest_clip_time + CLIP_HISTORY_RETENTION_SECONDS,
            })
    return sorted(history, key=lambda item: item["created_at"], reverse=True)


def normalize_signal(values):
    values = np.asarray(values, dtype=np.float32)
    if values.size == 0:
        return values
    low = float(np.percentile(values, 10))
    high = float(np.percentile(values, 90))
    if high - low < 1e-6:
        return np.zeros_like(values)
    return np.clip((values - low) / (high - low), 0.0, 1.0)

def extract_audio_activity(video_path, sample_times):
    """Measure short-window audio energy. This is an activity signal, not speech understanding."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [ffmpeg, "-v", "error", "-i", str(video_path), "-vn",
               "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"]
    try:
        result = subprocess.run(command, capture_output=True, check=True)
        audio = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
        samples_per_window = 4000
        energy = []
        for idx in range(len(sample_times)):
            chunk = audio[idx * samples_per_window:(idx + 1) * samples_per_window]
            energy.append(float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0)
        return normalize_signal(energy)
    except Exception:
        return np.zeros(len(sample_times), dtype=np.float32)

def analyse_video_with_gemini(video_path, duration, category, captions_path=None):
    """Use Gemini video understanding to find real, context-complete highlights."""
    api_key = get_gemini_api_key()
    if not api_key:
        return None

    from google import genai
    client = genai.Client(api_key=api_key)
    uploaded_file = None
    try:
        st.info("🧠 Gemini AI visuals + audio analyse kar raha hai: laughter, shouting, surprise, excitement, frustration aur gameplay highlights...")
        wait_started = time.time()
        wait_status = st.status("⏳ Gemini gameplay video process kar raha hai...", expanded=True)
        uploaded_file = client.files.upload(file=str(video_path))
        started = time.time()
        while not uploaded_file.state or uploaded_file.state.name != "ACTIVE":
            if uploaded_file.state and uploaded_file.state.name == "FAILED":
                raise RuntimeError("Gemini video processing fail ho gayi.")
            if time.time() - started > 1800:
                raise TimeoutError("Gemini video analysis 30 minutes se zyada le rahi hai.")
            elapsed_text = _format_wait(time.time() - wait_started)
            wait_status.update(label=f"⏳ Gemini gameplay processing... · elapsed {elapsed_text} · exact % available nahi hai", state="running")
            time.sleep(5)
            uploaded_file = client.files.get(name=uploaded_file.name)
        wait_status.update(label="✅ Gemini video upload/processing complete", state="complete")

        ai_status = st.status("🧠 Gemini ab highlights, reactions aur captions analyse kar raha hai... API response ka wait ho raha hai.", expanded=True)
        ai_started = time.time()
        prompt = f"""
You are an expert gameplay highlight editor. Watch and understand the ENTIRE video,
including visual events, game context, reactions and available audio.
Video duration: {duration:.2f} seconds.
Selected category: {category}.

Find every genuinely strong, distinct, short-form-worthy moment. Analyse BOTH the visuals
AND the audio carefully; do not rely only on action on screen.

For gaming, look for kills, multi-kills, clutch attempts/results, close escapes, intense
fights, funny mistakes, unexpected events, impressive skill, wins and strong reactions.
Pay special attention to human vocal reactions when audible:
- laughter, giggling, laughing so hard that speech breaks
- shouting, yelling, screaming, loud excited callouts or celebrations
- surprise, gasps, shocked reactions and disbelief
- frustration, anger, complaining or disappointed reactions
- nervousness, suspense, fear, relief and excitement
- emotional speech, teasing, playful banter and funny conversations

Use the actual audio to locate the beginning and end of these reactions. Distinguish a
person's voice from game sound effects, music and crowd/game audio where possible.
Do not label an emotion just because the gameplay looks intense: it must be supported
by audible speech/vocal cues, or clearly visible facial/body cues if a person is shown.
Do not guess what a person feels or invent reactions. If there is no clear human reaction,
focus on the gameplay event. For other categories, detect equivalent audible and visible
emotional moments such as laughter, surprise, excitement, frustration and heartfelt reactions.

For each highlight choose precise start_seconds and end_seconds:
- Include enough lead-in to understand what is happening and the complete payoff/reaction.
- For a laugh, shout or emotional reaction, include the setup and the full reaction, not just
  the loudest instant; preserve a little context before and after it.
- Do not cut in the middle of an action, sentence, joke, laugh or reaction.
- Let each moment have its natural duration; do NOT force a common duration.
- Each clip must be at least 10 seconds and no longer than 180 seconds.
- Prefer one complete event/reaction, not arbitrary chunks of unrelated footage.
- Avoid filler, repetitive scenes and overlapping/duplicate moments.
- Return all distinct strong moments; do not invent moments just to increase the count.
- Timestamps must be within the source video's duration.

Also transcribe clearly audible human speech for on-screen captions. Return short readable caption
segments with approximate start_seconds and end_seconds. Do not invent words; omit unclear speech.

Return ONLY valid JSON in this exact structure:
{{"highlights":[{{"start_seconds":12.5,"end_seconds":48.0,"event":"laughing / excited reaction","reason":"Briefly describe the audible or visible cue and the context","confidence":0.93}}],
"captions":[{{"start_seconds":13.1,"end_seconds":14.8,"text":"No way, that was close!"}}]}}
If no genuinely strong moments exist, return {{"highlights":[],"captions":[]}}.
"""
        ai_status.write("Video analysis request bhej di gayi hai. Gemini ka response aane tak wait karein; is stage ka exact percentage provider nahi deta.")
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=[uploaded_file, prompt]
        )
        ai_elapsed = _format_wait(time.time() - ai_started)
        ai_status.update(label=f"✅ Gemini highlight analysis response aa gaya · {ai_elapsed}", state="complete")
        raw = (response.text or "").strip()
        if raw.startswith("\x60\x60\x60"):
            raw = raw.split("\n", 1)[-1].rsplit("\x60\x60\x60", 1)[0].strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        data = json.loads(raw)
        raw_highlights = data.get("highlights", [])
        if not isinstance(raw_highlights, list):
            raise ValueError("Gemini response ka highlights format valid nahi hai.")

        # Keep only clear, bounded speech segments for optional burned-in captions.
        if captions_path:
            clean_captions = []
            raw_captions = data.get("captions", [])
            for caption in raw_captions if isinstance(raw_captions, list) else []:
                if not isinstance(caption, dict):
                    continue
                try:
                    cap_start = max(0.0, float(caption["start_seconds"]))
                    cap_end = min(float(duration), float(caption["end_seconds"]))
                    cap_text = re.sub(r"\s+", " ", str(caption.get("text", ""))).strip()
                except (KeyError, TypeError, ValueError):
                    continue
                if cap_text and 0 <= cap_start < cap_end <= duration and cap_end - cap_start <= 12:
                    clean_captions.append({
                        "start_seconds": cap_start, "end_seconds": cap_end,
                        "text": cap_text[:180]
                    })
            save_json_atomic(captions_path, clean_captions)

        candidates = []
        for item in raw_highlights:
            if not isinstance(item, dict):
                continue
            try:
                start_time = max(0.0, float(item["start_seconds"]))
                end_time = min(float(duration), float(item["end_seconds"]))
                confidence = float(item.get("confidence", 0.75))
            except (KeyError, TypeError, ValueError):
                continue
            clip_length = end_time - start_time
            if clip_length < 10.0 or clip_length > 180.0:
                continue
            if not (0.0 <= start_time < end_time <= duration):
                continue
            confidence = min(1.0, max(0.0, confidence))
            candidates.append((confidence, start_time, end_time))

        # Keep the strongest non-overlapping detections; no fixed clip-count slider.
        candidates.sort(key=lambda item: item[0], reverse=True)
        chosen = []
        for score, start_time, end_time in candidates:
            if all(end_time <= old_start or start_time >= old_end
                   for _, old_start, old_end in chosen):
                chosen.append((score, start_time, end_time))
        chosen.sort(key=lambda item: item[1])
        if chosen:
            st.success(f"Gemini ne {len(chosen)} distinct gameplay moments identify kiye.")
            return chosen
        st.warning("Gemini ko clear complete highlights nahi mile; visual/audio detection fallback use hogi.")
        return None
    except Exception as error:
        if "ai_status" in locals():
            ai_status.update(label=f"⚠️ Gemini highlight analysis request fail hui: {str(error)[:220]}", state="error")
        raise
    finally:
        if uploaded_file is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass


def analyse_video(video_path, checkpoint_file, category):
    """Detect highlight-like moments and choose a separate 10-180 second duration."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError("Video open nahi ho paayi. MP4 file try karo.")
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not fps or not frame_count:
        cap.release()
        raise ValueError("Video ka FPS ya duration read nahi ho paaya.")
    duration = frame_count / fps
    if duration < 10:
        cap.release()
        raise ValueError("Video kam se kam 10 seconds ki honi chahiye.")

    # Prefer semantic AI understanding when a Gemini key is available.
    try:
        semantic_chosen = analyse_video_with_gemini(
            video_path, duration, category,
            str(Path(checkpoint_file).parent / "captions.json")
        )
        if semantic_chosen:
            try:
                os.remove(checkpoint_file)
            except FileNotFoundError:
                pass
            st.session_state["last_analysis_mode"] = "Gemini semantic AI"
            return duration, semantic_chosen
    except Exception as semantic_error:
        st.warning(f"Gemini semantic analysis use nahi ho paayi; visual/audio fallback chalega: {semantic_error}")
    st.session_state["last_analysis_mode"] = "Visual + audio fallback"

    sample_times = np.arange(0, duration, 0.5)
    audio_scores = extract_audio_activity(video_path, sample_times)
    previous, previous_hist = None, None
    motion_scores, scene_scores, score_times = [], [], []
    next_idx = 0
    if os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file, "r", encoding="utf-8") as file:
                saved = json.load(file)
            next_idx = int(saved.get("next_idx", 0))
            motion_scores = [float(x) for x in saved.get("motion_scores", [])]
            scene_scores = [float(x) for x in saved.get("scene_scores", [])]
            score_times = [float(x) for x in saved.get("score_times", [])]
            previous_data, hist_data = saved.get("previous"), saved.get("previous_hist")
            previous = np.array(previous_data, dtype=np.uint8) if previous_data else None
            previous_hist = np.array(hist_data, dtype=np.float32) if hist_data else None
            if len(motion_scores) != len(score_times) or len(scene_scores) != len(score_times):
                raise ValueError("Old checkpoint format")
            next_idx = min(next_idx, len(sample_times))
            st.info(f"Analysis checkpoint se resume ho raha hai: {next_idx}/{len(sample_times)} samples.")
        except Exception:
            next_idx, motion_scores, scene_scores, score_times = 0, [], [], []
            previous, previous_hist = None, None

    progress = st.progress(min(next_idx / max(len(sample_times), 1), 1.0),
                           text="Multi-signal video analysis resume ho raha hai...")
    analysis_started = time.time()
    completed_before = next_idx
    for idx in range(next_idx, len(sample_times)):
        timestamp = sample_times[idx]
        cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp * 1000))
        ok, frame = cap.read()
        if ok:
            small = cv2.resize(frame, (320, 180))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            hist = cv2.calcHist([gray], [0], None, [32], [0, 256]).flatten()
            hist = hist / max(float(hist.sum()), 1.0)
            if previous is not None:
                motion_scores.append(float(cv2.absdiff(previous, gray).mean()))
                scene_scores.append(float(np.abs(previous_hist - hist).sum() / 2.0))
                score_times.append(float(timestamp))
            previous, previous_hist = gray, hist
        if (idx + 1) % 10 == 0 or idx == len(sample_times) - 1:
            save_json_atomic(checkpoint_file, {
                "next_idx": idx + 1, "motion_scores": motion_scores,
                "scene_scores": scene_scores, "score_times": score_times,
                "previous": previous.tolist() if previous is not None else None,
                "previous_hist": previous_hist.tolist() if previous_hist is not None else None
            })
        if idx % 5 == 0 or idx == len(sample_times) - 1:
            fraction = (idx + 1) / max(len(sample_times), 1)
            processed_now = idx + 1 - completed_before
            elapsed = time.time() - analysis_started
            remaining = max(0, len(sample_times) - (idx + 1))
            eta = (elapsed / max(processed_now, 1)) * remaining
            progress.progress(fraction, text=f"Video analysis: {fraction * 100:.1f}% · {idx + 1}/{len(sample_times)} samples · approx. {_format_wait(eta)} remaining")
    cap.release()
    progress.empty()

    if not motion_scores:
        raise ValueError("Video se visual frames analyse nahi ho paaye.")
    motion = normalize_signal(motion_scores)
    scene = normalize_signal(scene_scores)
    audio = audio_scores[1:1 + len(score_times)]
    if len(audio) < len(score_times):
        audio = np.pad(audio, (0, len(score_times) - len(audio)))
    audio = np.asarray(audio, dtype=np.float32)
    profiles = {
        "Auto (all categories)": (0.40, 0.30, 0.30),
        "Gaming": (0.55, 0.15, 0.30),
        "Sports": (0.45, 0.30, 0.25),
        "Vlog / Travel": (0.20, 0.45, 0.35),
        "Podcast / Talking": (0.05, 0.15, 0.80),
        "Education / Tutorial": (0.10, 0.20, 0.70),
        "Reaction / Comedy": (0.25, 0.25, 0.50),
        "Other": (0.34, 0.33, 0.33)
    }
    w_motion, w_scene, w_audio = profiles.get(category, profiles["Auto (all categories)"])
    combined = motion * w_motion + scene * w_scene + audio * w_audio

    # Smooth the signal and bridge short quiet gaps to keep a complete moment together.
    smoothed = np.convolve(combined, np.ones(7, dtype=np.float32) / 7, mode="same")
    threshold = max(float(np.percentile(smoothed, 72)),
                    float(np.mean(smoothed) + 0.20 * np.std(smoothed)))
    # A completely flat signal is not evidence of a highlight.
    active_indices = (np.flatnonzero(smoothed >= threshold)
                      if float(np.std(smoothed)) >= 1e-6 else np.array([], dtype=int))
    groups = []
    if active_indices.size:
        group_start = previous_idx = int(active_indices[0])
        for current_idx in active_indices[1:]:
            current_idx = int(current_idx)
            if current_idx - previous_idx > 12:
                groups.append((group_start, previous_idx))
                group_start = current_idx
            previous_idx = current_idx
        groups.append((group_start, previous_idx))

    times = np.asarray(score_times, dtype=np.float32)
    sample_step = float(np.median(np.diff(times))) if len(times) > 1 else 0.5
    candidates = []
    for first_idx, last_idx in groups:
        start_time = max(0.0, float(times[first_idx]) - 3.0)
        end_time = min(duration, float(times[last_idx]) + sample_step + 3.0)
        if end_time - start_time < 10.0:
            center = (start_time + end_time) / 2.0
            start_time = max(0.0, center - 5.0)
            end_time = min(duration, start_time + 10.0)
            start_time = max(0.0, end_time - 10.0)
        cursor = start_time
        while end_time - cursor > 180.0:
            piece_end = cursor + 180.0
            mask = (times >= cursor) & (times < piece_end)
            score = float(np.mean(combined[mask])) if np.any(mask) else 0.0
            candidates.append((score, cursor, piece_end))
            cursor = piece_end
        if end_time - cursor >= 10.0:
            mask = (times >= cursor) & (times <= end_time)
            score = float(np.mean(combined[mask])) if np.any(mask) else 0.0
            candidates.append((score, cursor, end_time))

    candidates.sort(key=lambda item: item[0], reverse=True)
    chosen = []
    for score, start_time, end_time in candidates:
        if all(end_time <= old_start or start_time >= old_end
               for _, old_start, old_end in chosen):
            chosen.append((score, start_time, end_time))
    try:
        os.remove(checkpoint_file)
    except FileNotFoundError:
        pass
    return duration, sorted(chosen, key=lambda item: item[1])


def _srt_timestamp(seconds):
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{millis:03}"


def _write_clip_srt(captions_path, clip_start, clip_end, srt_path):
    try:
        with open(captions_path, "r", encoding="utf-8") as file:
            captions = json.load(file)
    except (OSError, json.JSONDecodeError):
        return False
    entries = []
    for caption in captions if isinstance(captions, list) else []:
        try:
            start = max(float(caption["start_seconds"]), clip_start)
            end = min(float(caption["end_seconds"]), clip_end)
            caption_text = re.sub(r"[\r\n]+", " ", str(caption.get("text", ""))).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if caption_text and end > start:
            entries.append((start - clip_start, end - clip_start, caption_text))
    if not entries:
        return False
    with open(srt_path, "w", encoding="utf-8") as file:
        for index, (start, end, caption_text) in enumerate(entries, 1):
            file.write(f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{caption_text}\n\n")
    return True


def _create_whoosh(output_dir):
    """Create a quiet, original whoosh sound effect without external assets."""
    import wave
    sample_rate = 22050
    count = int(sample_rate * 0.42)
    noise = np.random.default_rng(42).normal(0, 1, count).astype(np.float32)
    smooth = np.convolve(noise, np.ones(18, dtype=np.float32) / 18.0, mode="same")
    envelope = np.sin(np.linspace(0, np.pi, count, dtype=np.float32)) ** 1.5
    audio = smooth * envelope
    peak = float(np.max(np.abs(audio))) if audio.size else 1.0
    samples = (np.clip(audio / max(peak, 1e-6) * 0.22, -1, 1) * 32767).astype(np.int16)
    path = Path(output_dir) / "subtle_whoosh.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    return path


def _create_like_subscribe_overlay(output_dir, width, height):
    """Create a transparent bottom CTA banner using OpenCV (no extra package)."""
    overlay = np.zeros((height, width, 4), dtype=np.uint8)
    scale = width / 1080.0
    font = cv2.FONT_HERSHEY_DUPLEX
    font_scale = 1.05 * scale
    thickness = max(2, int(2.5 * scale))
    like_text = "LIKE"
    sub_text = "SUBSCRIBE"
    (like_w, like_h), _ = cv2.getTextSize(like_text, font, font_scale, thickness)
    (sub_w, sub_h), _ = cv2.getTextSize(sub_text, font, font_scale, thickness)
    gap = int(52 * scale)
    pad_x = int(38 * scale)
    pad_y = int(25 * scale)
    total_w = like_w + sub_w + gap + pad_x * 2
    box_h = max(like_h, sub_h) + pad_y * 2
    x1 = max(int(24 * scale), (width - total_w) // 2)
    x2 = min(width - int(24 * scale), x1 + total_w)
    y2 = height - int(72 * scale)
    y1 = y2 - box_h
    # Dark translucent pill behind both calls to action.
    cv2.rectangle(overlay, (x1, y1), (x2, y2), (12, 16, 28, 218), -1, cv2.LINE_AA)
    like_x = x1 + pad_x
    text_y = y1 + pad_y + max(like_h, sub_h)
    cv2.putText(overlay, like_text, (like_x, text_y), font, font_scale,
                (255, 255, 255, 255), thickness, cv2.LINE_AA)
    sub_x = like_x + like_w + gap
    sub_rect_x1 = sub_x - int(18 * scale)
    sub_rect_x2 = min(x2 - int(12 * scale), sub_x + sub_w + int(18 * scale))
    cv2.rectangle(overlay, (sub_rect_x1, y1 + int(10 * scale)),
                  (sub_rect_x2, y2 - int(10 * scale)), (35, 45, 220, 245), -1, cv2.LINE_AA)
    cv2.putText(overlay, sub_text, (sub_x, text_y), font, font_scale,
                (255, 255, 255, 255), thickness, cv2.LINE_AA)
    path = str(Path(output_dir) / f"like_subscribe_{width}x{height}.png")
    if not cv2.imwrite(path, overlay):
        raise RuntimeError("Like/Subscribe overlay image create nahi ho paayi.")
    return path


def _has_audio_stream(ffmpeg, video_path):
    probe = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(video_path), "-map", "0:a:0",
         "-t", "0.05", "-f", "null", "-"],
        capture_output=True, text=True
    )
    return probe.returncode == 0


def export_clips(video_path, chosen, vertical, output_dir, captions_path=None):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(output_dir, exist_ok=True)
    outputs = []
    progress = st.progress(0, text="Viral edits apply ho rahe hain...")
    export_started = time.time()
    whoosh_path = _create_whoosh(output_dir)
    frame_width, frame_height = (1080, 1920) if vertical else (1920, 1080)
    cta_overlay_path = _create_like_subscribe_overlay(
        output_dir, frame_width, frame_height
    )
    has_audio = _has_audio_stream(ffmpeg, video_path)
    for index, (score, start_time, end_time) in enumerate(chosen, start=1):
        output_path = os.path.join(output_dir, f"Gaming_Short_{index}.mp4")
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            outputs.append(output_path)
            progress.progress(index / len(chosen),
                              text=f"Edited clip reuse ho rahi hai... {index}/{len(chosen)}")
            continue
        if os.path.exists(output_path):
            os.remove(output_path)
        clip_duration = end_time - start_time
        srt_path = str(Path(output_dir) / f"Gaming_Short_{index}.srt")
        has_captions = bool(captions_path and _write_clip_srt(
            captions_path, start_time, end_time, srt_path
        ))

        if vertical:
            video_filter = (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
                "fps=30,zoompan=z='min(zoom+0.00012,1.045)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                "s=1080x1920:fps=30,eq=contrast=1.06:saturation=1.12:brightness=0.01"
            )
        else:
            video_filter = (
                "fps=30,zoompan=z='min(zoom+0.00012,1.045)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                "s=1920x1080:fps=30,eq=contrast=1.06:saturation=1.12:brightness=0.01"
            )
        if has_captions:
            escaped_srt = srt_path.replace("\\", "/").replace(":", "\\:")
            video_filter += (
                f",subtitles='{escaped_srt}':force_style="
                "'FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
                "Alignment=2,MarginV=170'"
            )

        command = [
            ffmpeg, "-y", "-ss", f"{start_time:.3f}", "-i", str(video_path),
            "-loop", "1", "-i", cta_overlay_path
        ]
        if has_audio:
            command += ["-i", str(whoosh_path)]
        command += ["-t", f"{clip_duration:.3f}"]

        def build_filter_graph(active_video_filter):
            video_graph = (
                f"[0:v]{active_video_filter}[basev];"
                "[basev][1:v]overlay=0:0:shortest=1[vout]"
            )
            if has_audio:
                return (
                    video_graph + ";"
                    "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[basea];"
                    "[2:a]volume=0.18,adelay=120|120[fx];"
                    "[basea][fx]amix=inputs=2:duration=first:dropout_transition=0[aout]"
                )
            return video_graph

        command += ["-filter_complex", build_filter_graph(video_filter),
                    "-map", "[vout]"]
        if has_audio:
            command += ["-map", "[aout]"]
        else:
            command += ["-an"]

        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p"
        ]
        if has_audio:
            command += ["-c:a", "aac", "-b:a", "192k"]
        command += ["-movflags", "+faststart", output_path]

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 and has_captions:
            fallback_filter = video_filter.split(",subtitles=", 1)[0]
            command[command.index("-filter_complex") + 1] = build_filter_graph(fallback_filter)
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0:
                st.warning(f"Clip {index}: FFmpeg captions support unavailable; other edits applied.")
        if result.returncode != 0:
            details = result.stderr[-1800:] if result.stderr else "Unknown FFmpeg error"
            raise RuntimeError(f"Clip {index} export nahi hui:\\n{details}")
        outputs.append(output_path)
        fraction = index / len(chosen)
        elapsed = time.time() - export_started
        eta = (elapsed / max(index, 1)) * (len(chosen) - index)
        progress.progress(fraction, text=f"Viral edits: {fraction * 100:.0f}% · clip {index}/{len(chosen)} · approx. {_format_wait(eta)} remaining")
    progress.empty()
    return outputs

with st.sidebar:
    st.header("📚 Your clips")
    if st.button("🕒 Clip History (last 1 hour)", use_container_width=True):
        st.session_state.page = "history"
        st.rerun()
    st.divider()
    st.header("⚙️ Clip settings")
    st.info("Viral Auto Editing ON: smart 9:16 framing, gentle zoom, AI captions, audio leveling, color boost, subtle whoosh SFX aur bottom par LIKE + SUBSCRIBE banner. Clip duration 10 seconds se maximum 3 minutes.")
    vertical = st.checkbox("9:16 YouTube Shorts format", value=True)
    category = st.selectbox(
        "Video category",
        ["Auto (all categories)", "Gaming", "Sports", "Vlog / Travel",
         "Podcast / Talking", "Education / Tutorial", "Reaction / Comedy", "Other"]
    )
    st.caption("Gemini AI video ke visuals + audio dono se kills, clutch, funny moments, laughter, shouting, surprise, excitement, frustration aur doosre clear reactions detect karega. Key na ho ya AI fail ho toh visual/audio fallback chalega.")
    st.divider()
    st.header("✨ AI video metadata")
    st.text_input(
        "Gemini API Key",
        type="password",
        key="gemini_api_key_input",
        placeholder="Paste your Gemini API key",
        help="For permanent setup, add this once in Streamlit Cloud > Manage app > Settings > Secrets as [gemini] followed by api_key = \"YOUR_KEY\". A saved secret is loaded automatically in future sessions."
    )
    if get_gemini_api_key():
        st.success("Gemini API key available — saved Streamlit Secret or current session key detected")
        st.caption("Key ko chat, screenshot ya public GitHub file mein share mat karna.")
    else:
        st.info("Permanent setup ke liye Streamlit Cloud > Manage app > Settings > Secrets mein Gemini key ek baar save karo.")
        st.caption("Secrets mein save karne ke baad har session mein key dobara paste nahi karni padegi. Bina key ke visual/audio fallback clipping chalegi.")
    st.divider()
    st.header("▶️ YouTube channel")
    oauth_config = youtube_oauth_config()
    if not oauth_config:
        st.warning("YouTube connection setup pending hai.")
        st.caption("Google Cloud OAuth credentials banakar Streamlit Secrets mein [google_oauth] ke andar client_id aur client_secret add karne honge.")
    elif st.session_state.get("youtube_credentials"):
        st.success("YouTube account connected")
        if st.button("Disconnect YouTube", use_container_width=True):
            try:
                delete_persistent_youtube_credentials()
                st.session_state.pop("youtube_credentials", None)
                st.session_state.auto_upload_youtube = False
                st.session_state.youtube_persistence_error = None
            except Exception as disconnect_error:
                st.error("Saved login delete nahi hua: " + str(disconnect_error)[:250])
            st.rerun()
    else:
        try:
            flow = Flow.from_client_config(
                oauth_config, scopes=YOUTUBE_UPLOAD_SCOPE,
                autogenerate_code_verifier=False
            )
            flow.redirect_uri = YOUTUBE_REDIRECT_URI
            oauth_state = make_youtube_oauth_state(oauth_config["web"]["client_secret"])
            auth_url, _ = flow.authorization_url(
                state=oauth_state,
                access_type="offline", include_granted_scopes="true", prompt="consent"
            )
            st.link_button("Connect YouTube with Google", auth_url, use_container_width=True)
            st.caption("Google sign-in par permission approve karni hogi. Password app ko nahi diya jata.")
        except Exception as oauth_error:
            st.warning("YouTube sign-in setup mein issue hai. Baaki video clipping features use kar sakte ho.")
            st.caption(str(oauth_error)[:250])
    if st.session_state.get("youtube_oauth_error"):
        st.error(st.session_state.youtube_oauth_error)
    if st.session_state.get("youtube_persistence_error"):
        st.warning("YouTube login restore/save issue: " + st.session_state.youtube_persistence_error)
    youtube_connected = bool(st.session_state.get("youtube_credentials"))
    auto_upload_youtube = st.checkbox(
        "Automatically upload and schedule the next 1–2 clips",
        value=st.session_state.get("auto_upload_youtube_v2", True),
        disabled=not youtube_connected,
        key="auto_upload_youtube_v2",
        help="Har run mein maximum 2 clips upload hongi. Har scheduled publish time ke beech random 3.5–5 hours ka gap hoga. Baaki clips queue mein rahengi."
    )

if st.session_state.page == "history":
    st.subheader("🕒 Clip History & Upload Queue")
    st.caption("Jo clips abhi upload nahi hui hain, woh upload hone tak queue mein saved rahengi. Streamlit Cloud ka temporary storage restart par clear ho sakta hai.")
    history_items = list_clip_history()
    if history_items:
        for item in history_items:
            job_dir = item["job_dir"]
            clips = item["clips"]
            minutes_left = max(0, int((item["expires_at"] - time.time()) / 60))
            with st.expander(f"{job_dir.name} · {len(clips)} clips · approx. {minutes_left} min left", expanded=True):
                show_video_metadata(load_video_metadata(job_dir))
                upload_status_path = job_dir / "youtube_uploads.json"
                try:
                    with open(upload_status_path, "r", encoding="utf-8") as status_file:
                        history_uploads = json.load(status_file)
                    if not isinstance(history_uploads, dict):
                        history_uploads = {}
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    history_uploads = {}

                if st.session_state.get("youtube_credentials"):
                    if st.button(
                        "📅 Schedule next 1–2 queued clips",
                        key=f"history_schedule_batch_{job_dir.name}",
                        use_container_width=True
                    ):
                        try:
                            yt_service = get_youtube_service()
                            batch_results = upload_clips_to_youtube(
                                yt_service, clips, str(upload_status_path),
                                load_video_metadata(job_dir)
                            )
                            for clip_name, video_id, status in batch_results:
                                st.success(f"{clip_name}: https://www.youtube.com/watch?v={video_id} ({status})")
                            st.rerun()
                        except Exception as batch_error:
                            st.error(f"Queue upload nahi ho paaya: {str(batch_error)[:400]}")

                for clip_path in clips:
                    st.write(f"**{clip_path.name}**")
                    st.video(str(clip_path))
                    saved_upload = history_uploads.get(clip_path.name)
                    saved_video_id = saved_upload.get("video_id") if isinstance(saved_upload, dict) else saved_upload
                    if saved_video_id:
                        st.success("YouTube par upload ho chuki hai.")
                        st.markdown(f"[YouTube video kholo](https://www.youtube.com/watch?v={saved_video_id})")
                    elif st.session_state.get("youtube_credentials"):
                        if st.button(
                            f"⬆️ Upload Now (Private) — {clip_path.name}",
                            key=f"history_upload_now_{job_dir.name}_{clip_path.name}",
                            use_container_width=True
                        ):
                            try:
                                yt_service = get_youtube_service()
                                video_id, upload_state = upload_clip_now_to_youtube(
                                    yt_service, clip_path, str(upload_status_path),
                                    load_video_metadata(job_dir)
                                )
                                st.success(f"Upload complete: https://www.youtube.com/watch?v={video_id} ({upload_state})")
                                st.rerun()
                            except Exception as upload_error:
                                st.error(f"Direct YouTube upload nahi ho paaya: {str(upload_error)[:500]}")
                    else:
                        st.info("Direct upload ke liye sidebar se YouTube connect karo.")

                    with open(clip_path, "rb") as clip_file:
                        st.download_button(
                            f"⬇️ Download {clip_path.name}",
                            data=clip_file.read(),
                            file_name=clip_path.name,
                            mime="video/mp4",
                            key=f"history_download_{job_dir.name}_{clip_path.name}"
                        )
                if len(clips) > 1:
                    zip_path = job_dir / "Gaming_Shorts.zip"
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                        for clip_path in clips:
                            archive.write(clip_path, arcname=clip_path.name)
                    with open(zip_path, "rb") as zip_file:
                        st.download_button(
                            "⬇️ Download all clips (ZIP)",
                            data=zip_file.read(),
                            file_name="Gaming_Shorts.zip",
                            mime="application/zip",
                            type="primary",
                            key=f"history_zip_{job_dir.name}"
                        )
    else:
        st.info("Pichhle 1 ghante mein koi generated clips nahi mili.")
    if st.button("← Back to Upload"):
        st.session_state.page = "upload"
        st.rerun()

elif st.session_state.page == "upload":
    st.subheader("1. Upload your gameplay")
    st.write("Yahan video sirf save hogi. Processing tab tak start nahi hogi jab tak aap khud button na dabayein.")

    uploaded = st.file_uploader(
        "Gameplay video select karo",
        type=["mp4", "mov", "mkv", "avi"],
        help="Maximum upload size: 1 GB (1024 MB). Upload ke waqt original file ko compress ya resize nahi kiya jata.",
        max_upload_size=1024
    )

    if uploaded is not None:
        st.video(uploaded)
        st.caption(f"File: {uploaded.name} · Size: {uploaded.size / (1024 * 1024):.1f} MB")
        if st.button("💾 Save Video Only", type="primary", use_container_width=True):
            try:
                saved_path = save_uploaded_video(uploaded)
                st.session_state.selected_video = str(saved_path)
                st.success("Video save ho gayi. Abhi koi AI processing nahi hui.")
            except Exception as error:
                st.error(f"Video save nahi ho paayi: {error}")

    st.divider()
    st.subheader("Or paste a public video link")
    st.caption("Supported: YouTube, Vimeo, Dailymotion, Twitch and Streamable. Only public videos that you have permission to use.")
    video_url = st.text_input("Video URL", placeholder="https://...")
    if st.button("🌐 Fetch Video from Link", use_container_width=True):
        if not video_url.strip():
            st.warning("Pehle video link paste karo.")
        else:
            try:
                with st.spinner("Public video fetch ho rahi hai..."):
                    linked_path = download_video_from_url(video_url)
                st.session_state.selected_video = str(linked_path)
                st.success("Video fetch ho gayi. Ab Continue to Processing dabao.")
                st.video(str(linked_path))
            except Exception as error:
                st.error(str(error))

    saved_videos = list_saved_videos()
    if saved_videos:
        st.divider()
        st.subheader("Saved videos")
        names = [p.name for p in saved_videos]
        current_name = Path(st.session_state.selected_video).name if st.session_state.selected_video else ""
        default_index = names.index(current_name) if current_name in names else 0
        selected_name = st.selectbox("Baad mein process karne ke liye video choose karo", names,
                                     index=default_index)
        selected_path = UPLOAD_DIR / selected_name
        st.caption(f"Saved file size: {selected_path.stat().st_size / (1024 * 1024):.1f} MB")
        if st.button("Continue to Processing", use_container_width=True):
            st.session_state.selected_video = str(selected_path)
            st.session_state.page = "process"
            st.rerun()
    else:
        st.info("Abhi koi saved video nahi hai. Video select karke Save Video Only dabao.")

else:
    st.subheader("2. Create your Shorts")
    selected_path = Path(st.session_state.selected_video) if st.session_state.selected_video else None

    if selected_path and selected_path.exists():
        st.success(f"Selected video: {selected_path.name}")
        st.video(str(selected_path))
        job_dir = get_job_dir(selected_path, vertical, category)
        st.session_state.last_job_dir = str(job_dir)
        checkpoint_file = str(job_dir / "analysis_checkpoint.json")
        analysis_file = job_dir / "analysis.json"
        output_dir = str(job_dir / "clips")
        existing_clips = sorted(Path(output_dir).glob("Gaming_Short_*.mp4")) if Path(output_dir).exists() else []
        has_saved_work = os.path.exists(checkpoint_file) or os.path.exists(analysis_file) or bool(existing_clips)

        if has_saved_work:
            st.info(f"Saved progress found. {len(existing_clips)} completed clip(s) are already محفوظ.")

        st.divider()
        st.subheader("☁️ Background processing")
        st.caption("Is option se video private storage mein upload hogi. GitHub worker browser band hone ke baad bhi job complete karega; result dekhne ke liye isi app par wapas aakar status refresh karna.")
        if st.button("🚀 Start Background Processing", key="start_background_job", use_container_width=True):
            try:
                with st.spinner("Video private storage par upload ho rahi hai aur worker start ho raha hai..."):
                    bg_id, bg_size = submit_background_job(selected_path, category)
                st.session_state.background_job_id = bg_id
                st.query_params["bg_job"] = bg_id
                st.success(f"Background job start ho gayi. Job ID: {bg_id}")
            except Exception as bg_error:
                st.error(f"Background job start nahi hui: {str(bg_error)[:600]}")

        bg_job_id = st.session_state.get("background_job_id") or st.query_params.get("bg_job", "")
        if bg_job_id:
            st.caption(f"Current background job: {bg_job_id}")
            if st.button("🔄 Refresh Background Job Status", key="refresh_background_status", use_container_width=True):
                try:
                    bg_status = fetch_background_status(bg_job_id)
                    st.session_state.background_job_status = bg_status
                    if bg_status.get("state") == "completed":
                        paths = bg_status.get("clips", [])
                        st.session_state.background_clip_paths = [str(p) for p in paths]
                        local_clips = download_background_clips(bg_job_id, paths)
                        st.session_state.background_local_clips = [str(p) for p in local_clips]
                        thumbnail_paths = bg_status.get("thumbnail_paths", [])
                        local_thumbnails = download_background_thumbnails(bg_job_id, thumbnail_paths)
                        st.session_state.background_local_thumbnails = [str(p) for p in local_thumbnails]
                except Exception as status_error:
                    st.error(f"Status/result fetch nahi ho paaya: {str(status_error)[:500]}")
            bg_status = st.session_state.get("background_job_status", {})
            if bg_status:
                state = bg_status.get("state", "unknown")
                st.write(f"**Status:** {state.upper()}")
                st.info(bg_status.get("message", "Status available hai."))
                if state == "completed":
                    st.success(f"{bg_status.get('clip_count', len(st.session_state.get('background_local_clips', [])))} Shorts ready hain.")
                    bg_metadata = bg_status.get("metadata", {})
                    if bg_metadata:
                        st.markdown("### AI-generated YouTube metadata")
                        st.write("**Title:**", bg_metadata.get("title", ""))
                        st.write("**Description:**", bg_metadata.get("description", ""))
                        hashtags = bg_metadata.get("hashtags", [])
                        tags = bg_metadata.get("tags", [])
                        if hashtags:
                            st.write("**Hashtags:**", " ".join(str(tag) for tag in hashtags))
                        if tags:
                            st.write("**Tags:**", ", ".join(str(tag) for tag in tags))
                    features = bg_status.get("editing_features", [])
                    if features:
                        st.caption("Editing applied: " + " · ".join(features))
                    for thumbnail_string in st.session_state.get("background_local_thumbnails", []):
                        thumbnail_path = Path(thumbnail_string)
                        if thumbnail_path.exists():
                            st.image(str(thumbnail_path), caption=f"AI Thumbnail · {thumbnail_path.name}", use_container_width=True)
                            st.download_button(
                                f"⬇️ Download {thumbnail_path.name}",
                                data=thumbnail_path.read_bytes(),
                                file_name=thumbnail_path.name,
                                mime="image/jpeg",
                                key=f"bg_thumbnail_{bg_job_id}_{thumbnail_path.name}"
                            )
                    for clip_string in st.session_state.get("background_local_clips", []):
                        clip_path = Path(clip_string)
                        if clip_path.exists():
                            with st.expander(clip_path.name, expanded=False):
                                st.video(str(clip_path))
                                st.download_button(
                                    f"⬇️ Download {clip_path.name}",
                                    data=clip_path.read_bytes(),
                                    file_name=clip_path.name,
                                    mime="video/mp4",
                                    key=f"bg_download_{bg_job_id}_{clip_path.name}"
                                )
                elif state == "failed":
                    st.error("Worker fail hua. Status message mein error detail dekho.")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("← Back to Uploads", use_container_width=True):
                st.session_state.page = "upload"
                st.rerun()
        with col2:
            button_text = "▶️ Resume Processing" if has_saved_work else "🚀 Start Processing"
            if st.button(button_text, type="primary", use_container_width=True):
                try:
                    if os.path.exists(analysis_file):
                        with open(analysis_file, "r", encoding="utf-8") as file:
                            saved_analysis = json.load(file)
                        duration = float(saved_analysis["duration"])
                        st.session_state["last_analysis_mode"] = saved_analysis.get("mode", "Older saved analysis")
                        chosen = [(float(item[0]), float(item[1]), float(item[2]))
                                  for item in saved_analysis["chosen"] if len(item) >= 3]
                        if not chosen:
                            raise ValueError("Saved analysis purane format mein hai. Fresh processing start karo.")
                        st.success("Saved variable-duration analysis loaded. Analysis dobara nahi chalegi.")
                    else:
                        duration, chosen = analyse_video(
                            selected_path, checkpoint_file, category
                        )
                        if not chosen:
                            st.error("Is video mein enough clear highlight moments detect nahi hue. Different gameplay video try karo.")
                        else:
                            save_json_atomic(analysis_file, {
                                "duration": duration,
                                "category": category,
                                "mode": st.session_state.get("last_analysis_mode", "Visual + audio fallback"),
                                "chosen": [[score, start_time, end_time]
                                           for score, start_time, end_time in chosen]
                            })
                    if chosen:
                        st.subheader("⭐ Selected gameplay moments")
                        st.caption(f"Detection method: {st.session_state.get('last_analysis_mode', 'Automatic detection')}")
                        for i, (score, start_time, end_time) in enumerate(chosen, 1):
                            st.write(f"**Clip {i}:** {start_time:.1f}s – {end_time:.1f}s · Duration: {end_time - start_time:.1f}s · Highlight score: {score:.2f}")
                        outputs = export_clips(
                            selected_path, chosen, vertical, output_dir,
                            str(job_dir / "captions.json")
                        )
                        st.success(f"{len(outputs)} clips ready! Progress saved hai.")
                        try:
                            video_metadata = generate_video_metadata(selected_path, job_dir, category)
                            if video_metadata:
                                show_video_metadata(video_metadata)
                            else:
                                st.info("AI metadata ke liye Streamlit Secrets mein Gemini API key add karo. Clipping bina iske bhi chalegi.")
                        except Exception as metadata_error:
                            st.warning(f"AI metadata generate nahi ho paaya: {metadata_error}")
                        if auto_upload_youtube and st.session_state.get("youtube_credentials"):
                            try:
                                youtube_service = get_youtube_service()
                                upload_status = str(job_dir / "youtube_uploads.json")
                                upload_results = upload_clips_to_youtube(
                                    youtube_service, outputs, upload_status, load_video_metadata(job_dir)
                                )
                                for clip_name, video_id, status in upload_results:
                                    st.success(f"{clip_name}: https://www.youtube.com/watch?v={video_id} ({status})")
                            except Exception as upload_error:
                                st.error(f"YouTube upload nahi ho paaya: {upload_error}")
                except Exception as error:
                    st.error(str(error))

        # Show already completed clips after a rerun, Back navigation, or app revisit.
        completed = sorted(Path(output_dir).glob("Gaming_Short_*.mp4")) if Path(output_dir).exists() else []
        if completed:
            if st.session_state.get("youtube_credentials"):
                if st.button("📅 Upload next 1–2 queued clips", use_container_width=True):
                    try:
                        youtube_service = get_youtube_service()
                        upload_status = str(job_dir / "youtube_uploads.json")
                        upload_results = upload_clips_to_youtube(
                            youtube_service, completed, upload_status, load_video_metadata(job_dir)
                        )
                        for clip_name, video_id, status in upload_results:
                            st.success(f"{clip_name}: https://www.youtube.com/watch?v={video_id} ({status})")
                    except Exception as upload_error:
                        st.error(f"YouTube upload nahi ho paaya: {upload_error}")
            st.subheader("Your generated Shorts")
            show_video_metadata(load_video_metadata(job_dir))
            for clip_path in completed:
                with st.expander(clip_path.name, expanded=False):
                    st.video(str(clip_path))
                    with open(clip_path, "rb") as file:
                        st.download_button(
                            f"Download {clip_path.name}",
                            data=file.read(),
                            file_name=clip_path.name,
                            mime="video/mp4",
                            key=f"download_{job_dir.name}_{clip_path.name}",
                            on_click=delete_source_video_after_download,
                            args=(str(selected_path),)
                        )

            zip_path = job_dir / "Gaming_Shorts.zip"
            if len(completed) > 1:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for clip_path in completed:
                        archive.write(clip_path, arcname=clip_path.name)
                with open(zip_path, "rb") as file:
                    st.download_button(
                        "⬇️ Download all completed clips (ZIP)",
                        data=file.read(),
                        file_name="Gaming_Shorts.zip",
                        mime="application/zip",
                        type="primary",
                        use_container_width=True,
                        on_click=delete_source_video_after_download,
                        args=(str(selected_path),)
                    )
    else:
        # The original source may have been deleted after a clip/ZIP download.
        # Keep generated clips available from the saved job directory.
        last_job_dir = Path(st.session_state.get("last_job_dir", "")) if st.session_state.get("last_job_dir") else None
        saved_clips = sorted(last_job_dir.joinpath("clips").glob("Gaming_Short_*.mp4")) if last_job_dir and last_job_dir.exists() else []
        if saved_clips:
            st.success("Original uploaded video download ke baad delete ho gayi. Generated clips abhi available hain.")
            st.subheader("Your generated Shorts")
            for clip_path in saved_clips:
                with st.expander(clip_path.name, expanded=False):
                    st.video(str(clip_path))
                    with open(clip_path, "rb") as file:
                        st.download_button(
                            f"Download {clip_path.name}",
                            data=file.read(),
                            file_name=clip_path.name,
                            mime="video/mp4",
                            key=f"download_saved_{last_job_dir.name}_{clip_path.name}"
                        )
            zip_path = last_job_dir / "Gaming_Shorts.zip"
            if len(saved_clips) > 1:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for clip_path in saved_clips:
                        archive.write(clip_path, arcname=clip_path.name)
                with open(zip_path, "rb") as file:
                    st.download_button(
                        "⬇️ Download all completed clips (ZIP)",
                        data=file.read(),
                        file_name="Gaming_Shorts.zip",
                        mime="application/zip",
                        type="primary",
                        use_container_width=True
                    )
        else:
            st.warning("Selected video nahi mil rahi. Upload page par wapas jaakar video dobara save karo.")
        if st.button("← Back to Uploads"):
            st.session_state.page = "upload"
            st.session_state.selected_video = ""
            st.session_state.pop("last_job_dir", None)
            st.rerun()

st.divider()
st.caption(
    "Upload ke waqt original video bytes save hote hain; upload par compression nahi hoti. "
    "Generated clips re-encode hoti hain, isliye output bilkul bit-for-bit original nahi hota. "
    "Vertical Shorts ke liye full-frame fit with black background hota hai. Streamlit Cloud ka local storage temporary hai; "
    "original source video clip ya ZIP download karte hi delete ho jati hai; generated clips history mein 1 hour tak available rehti hain aur uske baad history check hone par delete ho jati hain; "
    "cleanup app activity par hota hai, exact minute par background timer nahi chalta. "
    "Large files upload hone mein internet speed aur hosting limits ka asar padega."
)
st.caption("Sirf apne ya permission wale gameplay videos upload karo. Free hosting par bade videos process hone mein waqt lag sakta hai.")

# Update history: keep this section at the bottom so users can see what changed.
if "show_update_history" not in st.session_state:
    st.session_state.show_update_history = False

st.divider()
if st.button("🆕 Updates & Commit History", use_container_width=True):
    st.session_state.show_update_history = not st.session_state.show_update_history

if st.session_state.show_update_history:
    st.markdown("### Website updates")
    st.markdown("""
    **Latest update — 21 Nov 2026**\n\n    - Added public video URL fetching for supported platforms (YouTube, Vimeo, Dailymotion, Twitch and Streamable); link fetching does not start clipping automatically.

    - Added Google OAuth connection for YouTube and private uploads of generated Shorts. Automatic upload can be controlled from the sidebar; manual upload is also available.

    - Fixed YouTube uploads to use resumable chunk transfers, show progress, retry temporary request failures, and save each successful video ID so retries do not re-upload completed clips.

    - Kept generated videos private on YouTube by default.

    **Git commit message:** `Add public video URL import`\n\n    **Previous feature commit:** `Add category-aware multi-signal highlight detection`

    **Previous feature commit:** `67f028b763fa1b415b1b9d74c49f86f9f56a9968`
    """)
