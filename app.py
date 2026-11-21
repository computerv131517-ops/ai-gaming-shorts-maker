import os
import re
import time
import shutil
import tempfile
import zipfile
import subprocess
import hashlib
import hmac
import secrets
import json
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
    with open(destination, "wb") as target:
        while True:
            chunk = uploaded_file.read(8 * 1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
            written += len(chunk)
            progress.progress(min(written / total, 1.0),
                              text="Saving original video without compression...")
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


def download_video_from_url(video_url):
    """Download a public video from supported sites; never use login/cookies or bypass access controls."""
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
        return downloaded
    except Exception as error:
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

YOUTUBE_UPLOAD_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]
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
        st.session_state.youtube_credentials = flow.credentials.to_json()
        st.session_state.youtube_oauth_error = None
    except Exception as error:
        st.session_state.youtube_oauth_error = "Google connection failed: " + str(error)[:300]
    st.query_params.clear()
    st.rerun()


def get_youtube_service():
    token_json = st.session_state.get("youtube_credentials")
    if not token_json:
        return None
    credentials = Credentials.from_authorized_user_info(
        json.loads(token_json), scopes=YOUTUBE_UPLOAD_SCOPE
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        st.session_state.youtube_credentials = credentials.to_json()
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
        uploaded_file = client.files.upload(file=str(video_path))
        started = time.time()
        while not uploaded_file.state or uploaded_file.state.name != 'ACTIVE':
            if uploaded_file.state and uploaded_file.state.name == 'FAILED':
                raise RuntimeError('Gemini video processing fail ho gayi.')
            if time.time() - started > 1800:
                raise TimeoutError('Video analysis 30 minutes se zyada le rahi hai. Dobara try karo.')
            time.sleep(5)
            uploaded_file = client.files.get(name=uploaded_file.name)
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

def upload_clips_to_youtube(youtube, clip_paths, status_path, metadata=None):
    """Upload clips in resumable chunks and keep visible per-clip status."""
    try:
        with open(status_path, "r", encoding="utf-8") as file:
            uploaded = json.load(file)
        if not isinstance(uploaded, dict):
            uploaded = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        uploaded = {}

    results = []
    total_clips = len(clip_paths)
    if total_clips == 0:
        st.info("Upload ke liye koi clip nahi mili.")
        return results

    status_box = st.status("⏳ YouTube par clips upload ho rahi hain...", expanded=True)
    progress = st.progress(0, text=f"Upload starting · 0/{total_clips} clips")
    try:
        for index, clip_path in enumerate(clip_paths, start=1):
            clip_path = Path(clip_path)
            if not clip_path.is_file() or clip_path.stat().st_size == 0:
                raise RuntimeError(f"{clip_path.name} missing hai ya empty hai.")

            if clip_path.name in uploaded:
                video_id = uploaded[clip_path.name]
                results.append((clip_path.name, video_id, "already uploaded"))
                status_box.write(f"✅ **{clip_path.name}** — Pehle se YouTube par upload ho chuki hai.")
                status_box.write(f"https://www.youtube.com/watch?v={video_id}")
                progress.progress(index / total_clips,
                                  text=f"Completed · {index}/{total_clips} clips")
                continue

            status_box.write(f"🔄 **{clip_path.name}** — Upload ho rahi hai...")
            body = {
                "snippet": {
                    "title": ((metadata or {}).get("title") or
                              (clip_path.stem.replace("_", " ") + " #Shorts"))[:100],
                    "description": (
                        ((metadata or {}).get("description") or
                         "Created with AI Gaming Shorts Maker.") + "\\n\\n" +
                        " ".join((metadata or {}).get("hashtags", []))
                    )[:5000],
                    "categoryId": "20",
                },
                "status": {
                    "privacyStatus": "private",
                    "selfDeclaredMadeForKids": False,
                },
            }

            media = MediaFileUpload(
                str(clip_path),
                mimetype="video/mp4",
                chunksize=8 * 1024 * 1024,
                resumable=True
            )
            request = youtube.videos().insert(
                part="snippet,status", body=body, media_body=media
            )

            response = None
            while response is None:
                chunk_status, response = request.next_chunk(num_retries=3)
                if chunk_status is not None:
                    clip_percent = int(chunk_status.progress() * 100)
                    overall_progress = (
                        (index - 1) + float(chunk_status.progress())
                    ) / total_clips
                    progress.progress(
                        min(overall_progress, 1.0),
                        text=f"Uploading {clip_path.name}: {clip_percent}% · clip {index}/{total_clips}"
                    )

            video_id = response.get("id") if response else None
            if not video_id:
                raise RuntimeError(f"{clip_path.name} upload ke baad YouTube video ID nahi mila.")

            uploaded[clip_path.name] = video_id
            save_json_atomic(status_path, uploaded)
            results.append((clip_path.name, video_id, "uploaded"))
            status_box.write(f"✅ **{clip_path.name}** — YouTube par upload complete!")
            status_box.write(f"https://www.youtube.com/watch?v={video_id}")
            progress.progress(
                index / total_clips,
                text=f"Uploaded · {index}/{total_clips} clips complete"
            )

        status_box.update(label=f"✅ YouTube upload complete — {len(results)}/{total_clips} clips", state="complete", expanded=True)
        progress.progress(1.0, text=f"✅ All {total_clips} clips uploaded or already uploaded.")
    except Exception as error:
        status_box.update(label="⚠️ YouTube upload ruk gaya — neeche status dekho.", state="error", expanded=True)
        error_text = str(error)
        if "quotaExceeded" in error_text:
            raise RuntimeError("YouTube API quota limit hit hui. Quota reset hone ke baad pending clips upload karo.") from error
        if "uploadLimitExceeded" in error_text:
            raise RuntimeError("YouTube channel ki upload limit hit hui. Baad mein dobara try karo.") from error
        if "invalid_grant" in error_text or "invalid credentials" in error_text.lower():
            raise RuntimeError("Google session expire ho gayi. YouTube ko disconnect karke dobara connect karo.") from error
        raise RuntimeError(f"YouTube upload ruk gaya: {error_text[:500]}") from error

    return results


def save_json_atomic(path, data):
    temp_path = str(path) + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file)
    os.replace(temp_path, path)


# Handle the Google redirect only after all helper functions are defined.
handle_youtube_oauth_callback()

def get_job_dir(video_path, vertical, category):
    digest = hashlib.sha256()
    with open(video_path, "rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    settings = f"auto-variable-duration-v1|{int(vertical)}|{category}"
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
            progress.progress(min((idx + 1) / len(sample_times), 1.0),
                              text=f"Video analyse ho rahi hai... {idx + 1}/{len(sample_times)}")
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
    active_indices = np.flatnonzero(smoothed >= threshold)
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


def export_clips(video_path, chosen, vertical, output_dir):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(output_dir, exist_ok=True)
    outputs = []
    progress = st.progress(0, text="Clips export ho rahi hain...")
    for index, (score, start_time, end_time) in enumerate(chosen, start=1):
        output_path = os.path.join(output_dir, f"Gaming_Short_{index}.mp4")
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            outputs.append(output_path)
            progress.progress(index / len(chosen),
                              text=f"Saved clip reuse ho rahi hai... {index}/{len(chosen)}")
            continue
        if os.path.exists(output_path):
            os.remove(output_path)
        clip_duration = end_time - start_time
        command = [
            ffmpeg, "-y", "-ss", f"{start_time:.3f}", "-i", str(video_path),
            "-t", f"{clip_duration:.3f}", "-map", "0:v:0", "-map", "0:a?"
        ]
        if vertical:
            command += ["-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"]
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", output_path
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            details = result.stderr[-1800:] if result.stderr else "Unknown FFmpeg error"
            raise RuntimeError(f"Clip {index} export nahi hui:\\n{details}")
        outputs.append(output_path)
        progress.progress(index / len(chosen), text=f"Clips export ho rahi hain... {index}/{len(chosen)}")
    progress.empty()
    return outputs

with st.sidebar:
    st.header("📚 Your clips")
    if st.button("🕒 Clip History (last 1 hour)", use_container_width=True):
        st.session_state.page = "history"
        st.rerun()
    st.divider()
    st.header("⚙️ Clip settings")
    st.info("Smart Auto Clipping: har moment ki duration automatically choose hogi (10 seconds se maximum 3 minutes). Koi fixed clip-count limit nahi hai.")
    vertical = st.checkbox("9:16 YouTube Shorts format", value=True)
    category = st.selectbox(
        "Video category",
        ["Auto (all categories)", "Gaming", "Sports", "Vlog / Travel",
         "Podcast / Talking", "Education / Tutorial", "Reaction / Comedy", "Other"]
    )
    st.caption("Moment detection visual movement, scene changes aur audio activity par based hai; abhi kills/clutches ko semantically samajhne wala trained model nahi hai.")
    st.divider()
    st.header("✨ AI video metadata")
    st.text_input(
        "Gemini API Key",
        type="password",
        key="gemini_api_key_input",
        placeholder="Paste your Gemini API key",
        help="Key is used only in this active app session and is not written to the GitHub code or job files."
    )
    if get_gemini_api_key():
        st.success("Gemini API key available for this session")
        st.caption("Key ko chat, screenshot ya public GitHub file mein share mat karna.")
    else:
        st.info("Gemini API key paste karo. Ya phir Streamlit Secrets ka existing setup use kar sakte ho.")
        st.caption("Bina key ke clipping chalegi, lekin AI title/description/hashtags generate nahi honge.")
    st.divider()
    st.header("▶️ YouTube channel")
    oauth_config = youtube_oauth_config()
    if not oauth_config:
        st.warning("YouTube connection setup pending hai.")
        st.caption("Google Cloud OAuth credentials banakar Streamlit Secrets mein [google_oauth] ke andar client_id aur client_secret add karne honge.")
    elif st.session_state.get("youtube_credentials"):
        st.success("YouTube account connected")
        if st.button("Disconnect YouTube", use_container_width=True):
            st.session_state.pop("youtube_credentials", None)
            st.session_state.auto_upload_youtube = False
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
    youtube_connected = bool(st.session_state.get("youtube_credentials"))
    auto_upload_youtube = st.checkbox(
        "Automatically upload all generated clips as Private (no review required)",
        value=st.session_state.get("auto_upload_youtube_v2", True),
        disabled=not youtube_connected,
        key="auto_upload_youtube_v2",
        help="After processing finishes, every generated clip uploads automatically as Private. You can turn this off before processing."
    )

if st.session_state.page == "history":
    st.subheader("🕒 Clip History — last 1 hour")
    st.caption("Generated clips yahan 1 ghante tak milengi. Iske baad app unhe automatically clean up karegi jab history check hogi.")
    history_items = list_clip_history()
    if history_items:
        for item in history_items:
            job_dir = item["job_dir"]
            clips = item["clips"]
            minutes_left = max(0, int((item["expires_at"] - time.time()) / 60))
            with st.expander(f"{job_dir.name} · {len(clips)} clips · approx. {minutes_left} min left", expanded=True):
                show_video_metadata(load_video_metadata(job_dir))
                for clip_path in clips:
                    st.write(f"**{clip_path.name}**")
                    st.video(str(clip_path))
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
                                "mode": "variable_duration",
                                "chosen": [[score, start_time, end_time]
                                           for score, start_time, end_time in chosen]
                            })
                    if chosen:
                        st.subheader("⭐ Selected gameplay moments")
                        for i, (score, start_time, end_time) in enumerate(chosen, 1):
                            st.write(f"**Clip {i}:** {start_time:.1f}s – {end_time:.1f}s · Duration: {end_time - start_time:.1f}s · Highlight score: {score:.2f}")
                        outputs = export_clips(
                            selected_path, chosen, vertical, output_dir
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
                if st.button("⬆️ Upload generated clips to YouTube (Private)", use_container_width=True):
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
    **Latest update — 21 Nov 2026**\\n\\n    - Added public video URL fetching for supported platforms (YouTube, Vimeo, Dailymotion, Twitch and Streamable); link fetching does not start clipping automatically.

    - Added Google OAuth connection for YouTube and private uploads of generated Shorts. Automatic upload can be controlled from the sidebar; manual upload is also available.

    - Fixed YouTube uploads to use resumable chunk transfers, show progress, retry temporary request failures, and save each successful video ID so retries do not re-upload completed clips.

    - Kept generated videos private on YouTube by default.

    **Git commit message:** `Add public video URL import`\n\n    **Previous feature commit:** `Add category-aware multi-signal highlight detection`

    **Previous feature commit:** `67f028b763fa1b415b1b9d74c49f86f9f56a9968`
    """)
