import os
import re
import time
import shutil
import tempfile
import zipfile
import subprocess
import hashlib
from pathlib import Path

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

def list_saved_videos():
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

def analyse_video(video_path, clip_seconds, number_of_clips):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError("Video open nahi ho paayi. MP4 file try karo.")

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not fps or not frame_count:
        cap.release()
        raise ValueError("Video ka FPS ya duration read nahi ho paaya.")

    duration = frame_count / fps
    if duration < clip_seconds:
        cap.release()
        raise ValueError(f"Video sirf {duration:.1f} seconds ki hai. Clip length kam select karo.")

    sample_times = np.arange(0, duration, 0.5)
    previous = None
    scores = []
    score_times = []
    progress = st.progress(0, text="Gameplay analyse ho raha hai...")

    for idx, timestamp in enumerate(sample_times):
        cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp * 1000))
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if previous is not None:
            scores.append(float(cv2.absdiff(previous, gray).mean()))
            score_times.append(float(timestamp))
        previous = gray
        if idx % 10 == 0:
            progress.progress(min((idx + 1) / len(sample_times), 1.0),
                              text="Gameplay analyse ho raha hai...")

    cap.release()
    progress.empty()

    if not scores:
        raise ValueError("Video se visual frames analyse nahi ho paaye.")

    step = 0.5
    starts = np.arange(0, max(0, duration - clip_seconds) + 0.001, step)
    candidates = []
    score_array = np.asarray(scores)
    time_array = np.asarray(score_times)

    for start in starts:
        mask = (time_array >= start) & (time_array < start + clip_seconds)
        if np.any(mask):
            candidates.append((float(score_array[mask].mean()), float(start)))

    candidates.sort(reverse=True)
    chosen = []
    for score, start in candidates:
        end = start + clip_seconds
        if all(end <= old_start or start >= old_start + clip_seconds
               for _, old_start in chosen):
            chosen.append((score, start))
        if len(chosen) >= number_of_clips:
            break

    return duration, sorted(chosen, key=lambda item: item[1])

def export_clips(video_path, chosen, clip_seconds, vertical, output_dir):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    outputs = []
    for index, (score, start) in enumerate(chosen, start=1):
        output_path = os.path.join(output_dir, f"Gaming_Short_{index}.mp4")
        command = [
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(video_path),
            "-t", str(clip_seconds), "-map", "0:v:0", "-map", "0:a?"
        ]
        if vertical:
            # Center-crop to portrait without upscaling the original video.
            command += ["-vf", "crop=w='trunc(min(iw,ih*9/16)/2)*2':h='trunc(ih/2)*2':x=(iw-ow)/2:y=0"]
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", output_path
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            details = result.stderr[-1800:] if result.stderr else "Unknown FFmpeg error"
            raise RuntimeError(f"Clip {index} export nahi hui:\n{details}")
        outputs.append(output_path)
    return outputs

with st.sidebar:
    st.header("⚙️ Clip settings")
    clip_seconds = st.slider("Har clip ki length (seconds)", 10, 60, 20, 5)
    number_of_clips = st.slider("Kitni clips banani hain?", 1, 10, 5)
    vertical = st.checkbox("9:16 YouTube Shorts format", value=True)
    st.caption("Movement score ek estimate hai; camera movement bhi score badha sakta hai.")

if st.session_state.page == "upload":
    st.subheader("1. Upload your gameplay")
    st.write("Yahan video sirf save hogi. Processing tab tak start nahi hogi jab tak aap khud button na dabayein.")

    uploaded = st.file_uploader(
        "Gameplay video select karo",
        type=["mp4", "mov", "mkv", "avi"],
        help="Upload ke waqt original file ko compress ya resize nahi kiya jata."
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
        col1, col2 = st.columns(2)
        with col1:
            if st.button("← Back to Uploads", use_container_width=True):
                st.session_state.page = "upload"
                st.rerun()
        with col2:
            if st.button("🚀 Start Processing", type="primary", use_container_width=True):
                with tempfile.TemporaryDirectory() as work_dir:
                    try:
                        duration, chosen = analyse_video(selected_path, clip_seconds, number_of_clips)
                        if not chosen:
                            st.error("Is video mein selected length ke clips nahi mil paaye.")
                        else:
                            st.subheader("⭐ Selected gameplay moments")
                            for i, (score, start) in enumerate(chosen, 1):
                                st.write(f"**Clip {i}:** {start:.1f}s – {start + clip_seconds:.1f}s · Movement score: {score:.2f}")

                            with st.spinner("Clips export ho rahi hain. Audio include hoga..."):
                                output_dir = os.path.join(work_dir, "clips")
                                os.makedirs(output_dir, exist_ok=True)
                                outputs = export_clips(selected_path, chosen, clip_seconds, vertical, output_dir)

                            st.success(f"{len(outputs)} clips ready! Original uploaded file unchanged hai.")
                            for path in outputs:
                                with st.expander(Path(path).name):
                                    st.video(path)
                                    with open(path, "rb") as file:
                                        st.download_button(
                                            f"Download {Path(path).name}",
                                            data=file.read(),
                                            file_name=Path(path).name,
                                            mime="video/mp4",
                                            key=Path(path).name
                                        )

                            zip_path = os.path.join(work_dir, "Gaming_Shorts.zip")
                            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                                for path in outputs:
                                    archive.write(path, arcname=Path(path).name)
                            with open(zip_path, "rb") as file:
                                st.download_button(
                                    "⬇️ Download all clips (ZIP)",
                                    data=file.read(),
                                    file_name="Gaming_Shorts.zip",
                                    mime="application/zip",
                                    type="primary",
                                    use_container_width=True
                                )
                    except Exception as error:
                        st.error(str(error))
    else:
        st.warning("Selected video nahi mil rahi. Upload page par wapas jaakar video dobara save karo.")
        if st.button("← Back to Uploads"):
            st.session_state.page = "upload"
            st.rerun()

st.divider()
st.caption(
    "Upload ke waqt original video bytes save hote hain; upload par compression nahi hoti. "
    "Generated clips re-encode hoti hain, isliye output bilkul bit-for-bit original nahi hota. "
    "Vertical Shorts ke liye center crop hota hai. Streamlit Cloud ka local storage temporary hai; "
    "app restart/sleep ke baad saved uploads rehne ki guarantee nahi hai. "
    "Large files upload hone mein internet speed aur hosting limits ka asar padega."
)
st.caption("Sirf apne ya permission wale gameplay videos upload karo. Free hosting par bade videos process hone mein waqt lag sakta hai.")
