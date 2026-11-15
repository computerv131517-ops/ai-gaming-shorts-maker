# AI Gaming Shorts Maker

A free-first Streamlit web app that analyses gameplay movement and exports selected short clips with audio.

## Features
- Upload MP4, MOV, MKV, or AVI gameplay videos
- Estimate high-movement moments using OpenCV frame differences
- Choose clip length and number of clips
- Export MP4 clips with original audio when the source contains audio
- Optional 9:16 vertical crop for Shorts
- Preview clips and download individually or as a ZIP

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy free
1. Open https://share.streamlit.io/
2. Sign in with GitHub and select `computerv131517-ops/ai-gaming-shorts-maker`.
3. Set the main file path to `app.py`.
4. Deploy.

## Notes
- Movement score is a heuristic, not semantic AI. Fast camera movement can rank highly.
- Free hosting has CPU, memory, runtime, and upload constraints. Large gameplay files may fail or take a long time.
- Upload only videos you own or have permission to edit.
