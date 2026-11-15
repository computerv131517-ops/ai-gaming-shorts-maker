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

## YouTube private uploads (optional)

To enable channel connection and private uploads:

1. In Google Cloud Console, create/select a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen. While the app is in Testing, add the Google account that owns the YouTube channel as a test user.
3. Create an OAuth client ID of type **Web application**.
4. Add this exact Authorized redirect URI:
   `https://ai-gaming-shorts-maker.streamlit.app`
5. In Streamlit Community Cloud, open this app's **Settings → Secrets** and add:

```toml
[google_oauth]
client_id = "YOUR_GOOGLE_OAUTH_CLIENT_ID"
client_secret = "YOUR_GOOGLE_OAUTH_CLIENT_SECRET"
```

6. Save secrets and wait for the app to restart. In the sidebar, choose **Connect YouTube with Google** and approve the YouTube upload permission.
7. Uploads are **Private** by default. The automatic-upload checkbox is opt-in; otherwise use the manual private-upload button after clips are generated.

The OAuth token is kept in the active Streamlit session. Reconnecting may be required after the session expires or the app restarts. Never paste OAuth secrets into chat, GitHub source files, or public messages. Upload only videos you own or have permission to publish.
