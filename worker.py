import argparse, json, os, re, subprocess, tempfile, time
from pathlib import Path
import cv2, requests, imageio_ffmpeg
from google import genai

def api(method, path, **kwargs):
    key = os.environ['SUPABASE_SECRET_KEY']
    headers = kwargs.pop('headers', {})
    headers.update({'apikey': key, 'Authorization': 'Bearer ' + key})
    response = requests.request(method, os.environ['SUPABASE_URL'].rstrip('/') + '/storage/v1/' + path, headers=headers, timeout=300, **kwargs)
    response.raise_for_status()
    return response

def remote(job, name):
    return 'object/gaming-videos/jobs/' + job + '/' + name

def set_status(job, state, message, **extra):
    data = {'job_id': job, 'state': state, 'message': message, 'updated_at': time.time(), **extra}
    api('POST', remote(job, 'status.json'), headers={'Content-Type':'application/json','x-upsert':'true'}, data=json.dumps(data).encode())

def run(job):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', job): raise ValueError('Invalid job ID')
    with tempfile.TemporaryDirectory() as tmp:
        source = str(Path(tmp) / 'input.mp4')
        outdir = Path(tmp) / 'clips'; outdir.mkdir()
        set_status(job, 'running', 'Private source video download ho rahi hai.')
        response = api('GET', 'object/authenticated/gaming-videos/jobs/' + job + '/input.mp4', stream=True)
        with open(source, 'wb') as target:
            for chunk in response.iter_content(8 * 1024 * 1024):
                if chunk: target.write(chunk)
        cap = cv2.VideoCapture(source); fps = cap.get(cv2.CAP_PROP_FPS); frames = cap.get(cv2.CAP_PROP_FRAME_COUNT); cap.release()
        if not fps or frames / fps < 10: raise RuntimeError('Video kam se kam 10 seconds ki honi chahiye.')
        duration = frames / fps
        set_status(job, 'running', 'Gemini AI visuals aur audio analyse kar raha hai.')
        client = genai.Client(api_key=os.environ['GEMINI_API_KEY']); uploaded = None
        try:
            uploaded = client.files.upload(file=source); started = time.time()
            while not uploaded.state or uploaded.state.name != 'ACTIVE':
                if uploaded.state and uploaded.state.name == 'FAILED': raise RuntimeError('Gemini processing fail hui.')
                if time.time() - started > 1800: raise TimeoutError('Gemini processing timeout.')
                time.sleep(5); uploaded = client.files.get(name=uploaded.name)
            prompt = ('Analyze the entire video, visuals and audio. Find distinct strong gaming highlights: kills, clutches, escapes, fights, funny moments, wins, laughter, shouting, surprise, frustration and excitement. Only report events actually present; include context and payoff; avoid overlap and filler. Each clip 10-180 seconds. Return ONLY JSON with highlights array of objects containing start_seconds, end_seconds and confidence. Video duration: ' + str(duration))
            result = client.models.generate_content(model='gemini-3.8-flash', contents=[uploaded, prompt])
            raw = (result.text or '').strip()
            if raw.startswith('```'): raw = raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
            data = json.loads(raw); candidates = []
            for item in data.get('highlights', []):
                try:
                    a = max(0.0, float(item['start_seconds'])); b = min(duration, float(item['end_seconds']))
                    score = float(item.get('confidence', 0.75))
                    if 10 <= b-a <= 180 and a < b: candidates.append((score,a,b))
                except (KeyError, TypeError, ValueError): pass
            candidates.sort(reverse=True); chosen = []
            for item in candidates:
                _, a, b = item
                if all(b <= old_a or a >= old_b for _, old_a, old_b in chosen): chosen.append(item)
            chosen.sort(key=lambda x:x[1])
            if not chosen: raise RuntimeError('Gemini ko clear highlights nahi mile.')
        finally:
            if uploaded:
                try: client.files.delete(name=uploaded.name)
                except Exception: pass
        set_status(job, 'running', 'Highlights mil gaye; clips render ho rahi hain.', clip_count=len(chosen))
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe(); names = []
        for index, (_, start, end) in enumerate(chosen, 1):
            output = outdir / ('Gaming_Short_' + str(index) + '.mp4')
            vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,eq=contrast=1.06:saturation=1.12:brightness=0.01,drawbox=x=(iw-600)/2:y=ih-155:w=600:h=72:color=0x0c101c@0.82:t=fill,drawtext=text='LIKE':fontcolor=white:fontsize=34:x=(w-text_w)/2-115:y=h-130,drawtext=text='SUBSCRIBE':fontcolor=white:fontsize=34:x=(w-text_w)/2+35:y=h-130"
            cmd = [ffmpeg,'-y','-ss',str(start),'-i',source,'-t',str(end-start),'-vf',vf,'-af','loudnorm=I=-16:TP=-1.5:LRA=11','-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-movflags','+faststart',str(output)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode: raise RuntimeError('FFmpeg clip export failed: ' + (proc.stderr or '')[-1000:])
            set_status(job, 'uploading', output.name + ' private storage mein save ho rahi hai.')
            with open(output, 'rb') as video:
                api('POST', remote(job, 'clips/' + output.name), headers={'Content-Type':'video/mp4','x-upsert':'true'}, data=video)
            names.append('clips/' + output.name)
        set_status(job, 'completed', 'Processing complete.', duration=duration, clip_count=len(names), clips=names)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--job-id', required=True); args = parser.parse_args()
    try: run(args.job_id)
    except Exception as error:
        try: set_status(args.job_id, 'failed', str(error)[:1000])
        except Exception: pass
        raise
