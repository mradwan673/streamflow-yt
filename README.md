# StreamFlow — local video downloader

A tiny self-hosted web UI on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Paste a video URL from any of yt-dlp's [1800+ supported sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) — YouTube, Vimeo, TikTok, X, SoundCloud, and so on — pick format/quality/folder, watch the progress bar.

## Features

- Single video or full playlist
- MP4 video (up to 4K) or MP3 audio with bitrate choice
- Folder browser with "create new folder"
- Live progress (percent, speed, ETA, completed-files list)
- Pulls cookies from your local browser to bypass YouTube's bot check
- Light + dark themes

## Requirements

- macOS or Linux
- Python 3.10+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and [`ffmpeg`](https://ffmpeg.org/) on your `PATH`

```bash
brew install yt-dlp ffmpeg
```

## Run

```bash
python3 server.py
```

Then open <http://127.0.0.1:8765> in your browser.

## Expose to your phone / other computers

Use a tunnel — your machine stays in control, the public URL is just a proxy:

```bash
# Cloudflare quick tunnel (no signup)
brew install cloudflared
cloudflared tunnel --url http://localhost:8765

# or ngrok (signup required)
brew install ngrok
ngrok config add-authtoken <your-token>
ngrok http 8765
```

## Run in Docker

```bash
docker build -t streamflow .
docker run --rm -p 8765:10000 -v $(pwd)/downloads:/data streamflow
# open http://localhost:8765
```

## Deploy to Render (free tier)

1. Push this repo to your own GitHub.
2. https://dashboard.render.com → **New → Web Service** → connect the repo.
3. Pick **Docker** as the runtime, **Free** plan, leave the rest default. Deploy.
4. Wait ~5 minutes; you get a public `https://<name>.onrender.com` URL.

## Configuration (env vars)

| Variable | Default | Notes |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Bind address. Set to `0.0.0.0` for cloud. |
| `PORT` | `8765` | Listen port. Render sets this to `10000`. |
| `STREAMFLOW_DIR` | `~/Downloads` | Folder where files are saved. Folder picker is restricted to inside this dir. |

## Notes

- Files always save inside `STREAMFLOW_DIR`.
- Only download content you have rights to. Each site's ToS still applies.
- Cloud IPs (Render, Vercel, Fly, etc.) get blocked by YouTube quickly. For non-YouTube sites this is fine; for YouTube specifically, run locally or upload a fresh `cookies.txt` periodically.
