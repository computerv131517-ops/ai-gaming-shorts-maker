import argparse
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import cv2
import numpy as np
import requests
from google import genai


MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def api(method, path, **kwargs):
    key = os.environ["SUPABASE_SECRET_KEY"]
    headers = kwargs.pop("headers", {})
    headers.update({"apikey": key, "Authorization": "Bearer " + key})
    response = requests.request(
        method, os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1/" + path,
        headers=headers, timeout=300, **kwargs
    )
    response.raise_for_status()
    return response


def remote(job, name):
    return "object/gaming-videos/jobs/" + job + "/" + name


def set_status(job, state, message, **extra):
    data = {
        "job_id": job, "state": state, "message": message,
        "updated_at": time.time(), **extra
    }
    api(
        "POST", remote(job, "status.json"),
        headers={"Content-Type": "application/json", "x-upsert": "true"},
        data=json.dumps(data, ensure_ascii=False).encode("utf-8")
    )


def parse_json(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("Gemini ne valid JSON response nahi diya.")
    return json.loads(raw[start:end + 1])


def analyze_video(client, source, duration):
    uploaded = client.files.upload(file=source)
    started = time.time()
    try:
        while not uploaded.state or uploaded.state.name != "ACTIVE":
            if uploaded.state and uploaded.state.name == "FAILED":
                raise RuntimeError("Gemini video processing fail hui.")
            if time.time() - started > 1800:
                raise TimeoutError("Gemini video analysis timeout.")
            time.sleep(5)
            uploaded = client.files.get(name=uploaded.name)

        prompt = f"""
Analyze the complete gaming video, including visuals and audible speech.
Return ONLY valid JSON with this exact structure:
{{
 "highlights":[{{"start_seconds":0,"end_seconds":30,"confidence":0.9,"reason":"short reason","event":"clutch"}}],
 "captions":[{{"start_seconds":1.2,"end_seconds":3.1,"text":"exact words actually spoken"}}],
 "metadata":{{"title":"short engaging YouTube Shorts title","description":"2-3 sentence description","hashtags":["#gaming","#shorts"],"tags":["gaming","gameplay"]}}
}}
Rules:
- Find distinct strong moments: kills, multi-kills, clutch, escape, fight, funny moment, win, surprise, laughter, shouting, excitement or emotional reactions.
- Only report events actually present. Include setup and payoff. Avoid filler and overlapping clips.
- Each highlight must be 10 to 180 seconds and stay within the {duration:.2f}-second video.
- Captions must be a faithful transcript of clearly audible speech, with accurate absolute video timestamps. Do not invent words. If speech is unclear, omit it. Keep each caption short (ideally one line).
- Metadata must be relevant to the actual gameplay, not generic clickbait. Use 3-5 relevant hashtags and 5-12 tags.
- If no speech is audible, return an empty captions array.
"""
        result = client.models.generate_content(model=MODEL, contents=[uploaded, prompt])
        return parse_json(result.text)
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass


def make_srt(captions, clip_start, clip_end, path):
    def stamp(seconds):
        ms = max(0, int(round(seconds * 1000)))
        hours, ms = divmod(ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        seconds, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"

    lines = []
    selected = []
    for item in captions:
        try:
            start = max(float(item["start_seconds"]), clip_start)
            end = min(float(item["end_seconds"]), clip_end)
            text = re.sub(r"\s+", " ", str(item["text"])).strip()
            if end > start and text:
                selected.append((start - clip_start, end - clip_start, text[:180]))
        except (KeyError, TypeError, ValueError):
            continue
    for number, (start, end, text) in enumerate(selected, 1):
        lines.extend([str(number), f"{stamp(start)} --> {stamp(end)}", text, ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return len(selected)


def make_sound_effects(path, duration, sample_rate=48000):
    """Create original, royalty-free synthetic whoosh and impact accents."""
    total = max(1, int(duration * sample_rate))
    audio = np.zeros(total, dtype=np.float32)
    rng = np.random.default_rng(2026)

    def add_whoosh(at, length=0.65, amplitude=0.13):
        start = int(at * sample_rate)
        count = min(int(length * sample_rate), total - start)
        if count <= 0:
            return
        noise = rng.normal(0, 1, count).astype(np.float32)
        # Simple smoothing makes broadband noise feel like a soft sweep.
        kernel = np.ones(24, dtype=np.float32) / 24
        noise = np.convolve(noise, kernel, mode="same")
        envelope = np.sin(np.linspace(0, math.pi, count, dtype=np.float32)) ** 1.5
        audio[start:start + count] += noise * envelope * amplitude

    def add_impact(at, length=0.22, amplitude=0.22):
        start = int(at * sample_rate)
        count = min(int(length * sample_rate), total - start)
        if count <= 0:
            return
        t = np.arange(count, dtype=np.float32) / sample_rate
        envelope = np.exp(-t * 18)
        tone = np.sin(2 * math.pi * (95 - 35 * t / max(length, 0.01)) * t)
        click = rng.normal(0, 0.15, count).astype(np.float32)
        audio[start:start + count] += (tone + click) * envelope * amplitude

    add_whoosh(0.12)
    add_impact(min(0.95, max(0.15, duration * 0.12)))
    if duration > 18:
        add_whoosh(duration * 0.52, length=0.5, amplitude=0.08)
        add_impact(duration * 0.55, amplitude=0.14)

    peak = float(np.max(np.abs(audio))) if audio.size else 0
    if peak > 0.95:
        audio *= 0.95 / peak
    pcm = (audio * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def ffmpeg_escape(path):
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def generate_ai_thumbnail(client, clip_path, output_path, title_hint):
    """Generate a truthful 16:9 thumbnail from a real frame in this clip."""
    capture = cv2.VideoCapture(str(clip_path))
    if not capture.isOpened():
        return False
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_count * 0.45)))
    ok, frame = capture.read()
    capture.release()
    if not ok:
        return False
    frame = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        return False
    from google.genai import types
    prompt = (
        "Create a polished 16:9 YouTube gaming thumbnail using this real gameplay frame "
        "as the visual reference. Make the actual action clear, cinematic, vibrant and crisp. "
        "Do not add text, logos, watermarks, fake characters, fake UI or events not shown. "
        "Context: " + str(title_hint)[:180]
    )
    response = client.models.generate_content(
        model="gemini-3.1-flash-image",
        contents=[prompt, types.Part.from_bytes(data=encoded.tobytes(), mime_type="image/jpeg")],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="16:9", image_size="1K"),
        ),
    )
    for part in response.parts:
        if getattr(part, "inline_data", None):
            part.as_image().save(str(output_path))
            return output_path.is_file() and output_path.stat().st_size > 2048
    return False


def render_clip(ffmpeg, source, output, start, end, captions, index, metadata, tmpdir):
    length = end - start
    srt = tmpdir / f"captions_{index}.srt"
    caption_count = make_srt(captions, start, end, srt)
    sfx = tmpdir / f"sfx_{index}.wav"
    make_sound_effects(sfx, length)

    # Vertical crop, subtle periodic smart zoom, cinematic colour tuning,
    # readable burned-in captions, and a persistent Like/Subscribe footer.
    zoom = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "zoompan=z='1+0.045*(0.5-0.5*cos(2*PI*on/240))':"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30,"
        "eq=contrast=1.08:saturation=1.18:brightness=0.008,"
        f"subtitles='{ffmpeg_escape(srt)}':force_style='FontName=DejaVu Sans,FontSize=18,"
        "Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,BorderStyle=1,"
        "Outline=2,Shadow=1,Alignment=2,MarginV=190',"
        "drawbox=x=0:y=ih-132:w=iw:h=132:color=0x080D18@0.82:t=fill,"
        f"drawtext=fontfile='{FONT}':text='LIKE  •  SUBSCRIBE':"
        "fontcolor=white:fontsize=42:x=(w-text_w)/2:y=h-94:"
        "shadowcolor=black:shadowx=2:shadowy=2"
    )
    cmd = [
        ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(source), "-i", str(sfx),
        "-t", f"{length:.3f}", "-filter_complex",
        "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[base];"
        "[1:a]volume=0.32[sfx];[base][sfx]amix=inputs=2:duration=first:dropout_transition=0[a]",
        "-vf", zoom, "-map", "0:v:0", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(output)
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        # Some source videos have no audio stream; retry with generated SFX only.
        fallback = [
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(source), "-i", str(sfx),
            "-t", f"{length:.3f}", "-vf", zoom, "-map", "0:v:0", "-map", "1:a:0",
            "-af", "volume=0.32", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output)
        ]
        retry = subprocess.run(fallback, capture_output=True, text=True)
        if retry.returncode:
            raise RuntimeError("FFmpeg advanced edit failed: " + (retry.stderr or proc.stderr or "")[-1400:])
    return caption_count


def run(job):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", job):
        raise ValueError("Invalid job ID")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg install nahi mila.")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        source = tmpdir / "input.mp4"
        outdir = tmpdir / "clips"
        outdir.mkdir()
        set_status(job, "running", "Private source video download ho rahi hai.")
        response = api(
            "GET", "object/authenticated/gaming-videos/jobs/" + job + "/input.mp4",
            stream=True
        )
        with source.open("wb") as target:
            for chunk in response.iter_content(8 * 1024 * 1024):
                if chunk:
                    target.write(chunk)

        cap = cv2.VideoCapture(str(source))
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        if not fps or frames / fps < 10:
            raise RuntimeError("Video kam se kam 10 seconds ki honi chahiye.")
        duration = frames / fps

        set_status(job, "running", "Gemini highlights, speech captions aur metadata analyse kar raha hai.")
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        analysis = analyze_video(client, str(source), duration)

        candidates = []
        for item in analysis.get("highlights", []):
            try:
                a = max(0.0, float(item["start_seconds"]))
                b = min(duration, float(item["end_seconds"]))
                score = float(item.get("confidence", 0.75))
                if 10 <= b - a <= 180 and a < b:
                    candidates.append((score, a, b, str(item.get("event", "highlight"))))
            except (KeyError, TypeError, ValueError):
                continue
        candidates.sort(reverse=True)
        chosen = []
        for item in candidates:
            _, a, b, _ = item
            if all(b <= old[1] or a >= old[2] for old in chosen):
                chosen.append(item)
        chosen.sort(key=lambda item: item[1])
        if not chosen:
            raise RuntimeError("Gemini ko 10-180 second ke clear highlights nahi mile.")

        metadata = analysis.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.setdefault("title", "Epic Gaming Moment 🔥 #shorts")
        metadata.setdefault("description", "A highlight from this gameplay session.")
        metadata.setdefault("hashtags", ["#gaming", "#shorts"])
        metadata.setdefault("tags", ["gaming", "gameplay", "shorts"])

        captions = analysis.get("captions", [])
        if not isinstance(captions, list):
            captions = []

        set_status(
            job, "running",
            "Advanced editing chal rahi hai: vertical crop, smart zoom, captions, colour, audio aur SFX.",
            clip_count=len(chosen), metadata=metadata
        )
        names = []
        total_caption_count = 0
        for index, (_, start, end, event) in enumerate(chosen, 1):
            output = outdir / f"Gaming_Short_{index}.mp4"
            caption_count = render_clip(
                ffmpeg, source, output, start, end, captions, index, metadata, tmpdir
            )
            total_caption_count += caption_count
            set_status(job, "running", f"{output.name} ka AI thumbnail generate ho raha hai.")
            thumbnail = tmpdir / f"Gaming_Short_{index}_AI_Thumbnail.jpg"
            try:
                made_thumbnail = generate_ai_thumbnail(
                    client, output, thumbnail, metadata.get("title", event)
                )
            except Exception:
                made_thumbnail = False
            set_status(job, "uploading", f"{output.name} aur uska thumbnail private storage mein save ho rahe hain.")
            with output.open("rb") as video:
                api(
                    "POST", remote(job, "clips/" + output.name),
                    headers={"Content-Type": "video/mp4", "x-upsert": "true"},
                    data=video
                )
            names.append("clips/" + output.name)
            if made_thumbnail:
                with thumbnail.open("rb") as image_file:
                    api(
                        "POST", remote(job, "thumbnails/" + thumbnail.name),
                        headers={"Content-Type": "image/jpeg", "x-upsert": "true"},
                        data=image_file
                    )
                thumbnail_names.append("thumbnails/" + thumbnail.name)

        # Keep metadata and a text transcript alongside the private rendered clips.
        api(
            "POST", remote(job, "metadata.json"),
            headers={"Content-Type": "application/json", "x-upsert": "true"},
            data=json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        )
        set_status(
            job, "completed", "Advanced editing complete. Shorts ready hain.",
            duration=duration, clip_count=len(names), clips=names,
            caption_count=total_caption_count, metadata=metadata,
            editing_features=[
                "9:16 vertical framing", "AI speech captions burned into video",
                "Smart zoom", "Colour enhancement", "Audio loudness enhancement",
                "Original synthetic whoosh and impact sound effects",
                "Like and Subscribe footer", "AI title, description and hashtags", "AI-generated 16:9 thumbnails"
            ]
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    try:
        run(args.job_id)
    except Exception as error:
        try:
            set_status(args.job_id, "failed", str(error)[:1200])
        except Exception:
            pass
        raise
