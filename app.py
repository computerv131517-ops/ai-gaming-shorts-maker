import os
import re
import time
import shutil
import tempfile
import zipfile
import subprocess
import hashlib
import json
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

def save_json_atomic(path, data):
    temp_path = str(path) + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file)
    os.replace(temp_path, path)

def get_job_dir(video_path, clip_seconds, number_of_clips, vertical):
    digest = hashlib.sha256()
    with open(video_path, "rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    settings = f"{clip_seconds}|{number_of_clips}|{int(vertical)}"
    job_id = hashlib.sha256((digest.hexdigest() + settings).encode()).hexdigest()[:20]
    folder = UPLOAD_DIR / "processing_jobs" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder

def analyse_video(video_path, clip_seconds, number_of_clips, checkpoint_file):
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
    next_idx = 0

    if os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file, "r", encoding="utf-8") as file:
                saved = json.load(file)
            next_idx = int(saved.get("next_idx", 0))
            scores = [float(x) for x in saved.get("scores", [])]
            score_times = [float(x) for x in saved.get("score_times", [])]
            previous_data = saved.get("previous")
            previous = np.array(previous_data, dtype=np.uint8) if previous_data else None
            next_idx = min(next_idx, len(sample_times))
            st.info(f"Analysis checkpoint se resume ho raha hai: {next_idx}/{len(sample_times)} samples.")
        except Exception:
            next_idx, scores, score_times, previous = 0, [], [], None

    progress = st.progress(min(next_idx / max(len(sample_times), 1), 1.0),
                           text="Gameplay analysis resume ho raha hai...")
    for idx in range(next_idx, len(sample_times)):
        timestamp = sample_times[idx]
        cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp * 1000))
        ok, frame = cap.read()
        if ok:
            small = cv2.resize(frame, (320, 180))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if previous is not None:
                scores.append(float(cv2.absdiff(previous, gray).mean()))
                score_times.append(float(timestamp))
            previous = gray

        # Save a checkpoint every 10 samples (about 5 seconds of source video).
        if (idx + 1) % 10 == 0 or idx == len(sample_times) - 1:
            save_json_atomic(checkpoint_file, {
                "next_idx": idx + 1,
                "scores": scores,
                "score_times": score_times,
                "previous": previous.tolist() if previous is not None else None
            })
        if idx % 5 == 0 or idx == len(sample_times) - 1:
            progress.progress(min((idx + 1) / len(sample_times), 1.0),
                              text=f"Gameplay analyse ho raha hai... {idx + 1}/{len(sample_times)}")

    cap.release()
    progress.empty()

    if not scores:
        raise ValueError("Video se visual frames analyse nahi ho paaye.")

    step = 0.5
    starts = np.arange(0, max(0, duration - clip_seconds) + 0.001, step)
    candidates = []
    score_array = np.asarray(scores)
    time_array = np.asarray(score_times)

    for start_time in starts:
        mask = (time_array >= start_time) & (time_array < start_time + clip_seconds)
        if np.any(mask):
            candidates.append((float(score_array[mask].mean()), float(start_time)))

    candidates.sort(reverse=True)
    chosen = []
    for score, start_time in candidates:
        end_time = start_time + clip_seconds
        if all(end_time <= old_start or start_time >= old_start + clip_seconds
               for _, old_start in chosen):
            chosen.append((score, start_time))
        if len(chosen) >= number_of_clips:
            break

    try:
        os.remove(checkpoint_file)
    except FileNotFoundError:
        pass
    return duration, sorted(chosen, key=lambda item: item[1])

def export_clips(video_path, chosen, clip_seconds, vertical, output_dir):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(output_dir, exist_ok=True)
    outputs = []
    progress = st.progress(0, text="Clips export ho rahi hain...")
    for index, (score, start_time) in enumerate(chosen, start=1):
        output_path = os.path.join(output_dir, f"Gaming_Short_{index}.mp4")
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            outputs.append(output_path)
            progress.progress(index / len(chosen),
                              text=f"Saved clip reuse ho rahi hai... {index}/{len(chosen)}")
            continue

        # Remove an incomplete file left by an interrupted export.
        if os.path.exists(output_path):
            os.remove(output_path)

        command = [
            ffmpeg, "-y", "-ss", f"{start_time:.3f}", "-i", str(video_path),
            "-t", str(clip_seconds), "-map", "0:v:0", "-map", "0:a?"
        ]
        if vertical:
            # Fit the complete frame inside a 9:16 canvas; never crop.
            # Scale up/down as needed and fill unused space with black bars.
            command += ["-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"]
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
        progress.progress(index / len(chosen), text=f"Clips export ho rahi hain... {index}/{len(chosen)}")
    progress.empty()
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
        job_dir = get_job_dir(selected_path, clip_seconds, number_of_clips, vertical)
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
                        chosen = [(float(item[0]), float(item[1])) for item in saved_analysis["chosen"]]
                        st.success("Saved analysis loaded. Analysis dobara nahi chalegi.")
                    else:
                        duration, chosen = analyse_video(
                            selected_path, clip_seconds, number_of_clips, checkpoint_file
                        )
                        if not chosen:
                            st.error("Is video mein selected length ke clips nahi mil paaye.")
                        else:
                            save_json_atomic(analysis_file, {
                                "duration": duration,
                                "chosen": [[score, start_time] for score, start_time in chosen]
                            })
                    if chosen:
                        st.subheader("⭐ Selected gameplay moments")
                        for i, (score, start_time) in enumerate(chosen, 1):
                            st.write(f"**Clip {i}:** {start_time:.1f}s – {start_time + clip_seconds:.1f}s · Movement score: {score:.2f}")
                        outputs = export_clips(
                            selected_path, chosen, clip_seconds, vertical, output_dir
                        )
                        st.success(f"{len(outputs)} clips ready! Progress saved hai.")
                except Exception as error:
                    st.error(str(error))

        # Show already completed clips after a rerun, Back navigation, or app revisit.
        completed = sorted(Path(output_dir).glob("Gaming_Short_*.mp4")) if Path(output_dir).exists() else []
        if completed:
            st.subheader("Your generated Shorts")
            for clip_path in completed:
                with st.expander(clip_path.name, expanded=False):
                    st.video(str(clip_path))
                    with open(clip_path, "rb") as file:
                        st.download_button(
                            f"Download {clip_path.name}",
                            data=file.read(),
                            file_name=clip_path.name,
                            mime="video/mp4",
                            key=f"download_{job_dir.name}_{clip_path.name}"
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
                        use_container_width=True
                    )
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

# Update history: keep this section at the bottom so users can see what changed.
if "show_update_history" not in st.session_state:
    st.session_state.show_update_history = False

st.divider()
if st.button("🆕 Updates & Commit History", use_container_width=True):
    st.session_state.show_update_history = not st.session_state.show_update_history

if st.session_state.show_update_history:
    st.markdown("### Website updates")
    st.markdown("""
    **Latest update — 15 Nov 2026**

    - Changed 9:16 output to **Fit / No Crop**.
    - Entire original frame stays visible; extra canvas space is filled with black.
    - Added **Resume Processing** with saved analysis checkpoints.
    - Completed clips are saved individually; returning to the page reuses finished clips.
    - Original uploaded file remains unchanged.

    **Git commit message:** `Use fit-to-frame 9:16 output with black background`

    **Previous feature commit:** `0817307c4383ba59f84c62e8fbb53891e74c474a`
    """)
