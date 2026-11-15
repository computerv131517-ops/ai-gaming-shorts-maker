import os
import tempfile
import zipfile
import subprocess
from pathlib import Path

import cv2
import numpy as np
import streamlit as st
import imageio_ffmpeg

st.set_page_config(page_title="AI Gaming Shorts Maker", page_icon="🎮", layout="wide")

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

st.markdown('<div class="hero"><h1>🎮 AI Gaming Shorts Maker</h1><p>Upload gameplay → find high-action moments → export clips with audio.</p></div>', unsafe_allow_html=True)
st.write("")

def analyse_video(video_path, clip_seconds, number_of_clips):
    cap = cv2.VideoCapture(video_path)
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

    # Sample approximately 2 frames per second to keep free hosting usage manageable.
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
            progress.progress(min((idx + 1) / len(sample_times), 1.0), text="Gameplay analyse ho raha hai...")

    cap.release()
    progress.empty()

    if not scores:
        raise ValueError("Video se visual frames analyse nahi ho paaye.")

    # Average movement score for every possible clip window.
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
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", video_path,
            "-t", str(clip_seconds), "-map", "0:v:0", "-map", "0:a?",
        ]
        if vertical:
            # Fill a 9:16 canvas by scaling and center-cropping. Audio is copied through AAC encoding.
            command += ["-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"]
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            output_path
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

uploaded = st.file_uploader("Apna gameplay video upload karo", type=["mp4", "mov", "mkv", "avi"])

if uploaded:
    st.success(f"Uploaded: {uploaded.name}")
    st.video(uploaded)

    if st.button("🚀 Find Moments & Create Clips", type="primary", use_container_width=True):
        with tempfile.TemporaryDirectory() as work_dir:
            input_path = os.path.join(work_dir, Path(uploaded.name).name)
            with open(input_path, "wb") as file:
                file.write(uploaded.getbuffer())

            try:
                duration, chosen = analyse_video(input_path, clip_seconds, number_of_clips)
                if not chosen:
                    st.error("Is video mein selected length ke clips nahi mil paaye.")
                else:
                    st.subheader("⭐ Selected gameplay moments")
                    for i, (score, start) in enumerate(chosen, 1):
                        st.write(f"**Clip {i}:** {start:.1f}s – {start + clip_seconds:.1f}s  ·  Movement score: {score:.2f}")

                    with st.spinner("Clips export ho rahi hain. Audio bhi include hoga..."):
                        output_dir = os.path.join(work_dir, "clips")
                        os.makedirs(output_dir, exist_ok=True)
                        outputs = export_clips(input_path, chosen, clip_seconds, vertical, output_dir)

                    st.success(f"{len(outputs)} clips ready! Original audio included where available.")
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
    st.info("Shuru karne ke liye upar apni gameplay video upload karo.")

st.caption("Tip: Sirf apne ya permission wale gameplay videos upload karo. Free hosting par bade videos process hone mein waqt lag sakta hai.")
