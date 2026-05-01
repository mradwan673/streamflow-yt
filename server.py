#!/usr/bin/env python3
"""Tiny local video downloader. Single video or playlist, with progress."""
import json
import os
import pty
import re
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

PORT = 8765
HOME = Path.home()
DEFAULT_DIR = HOME / "Downloads"
DEFAULT_DIR.mkdir(exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

DOWNLOAD_PROG_RE = re.compile(
    r"^\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*\S+\s+at\s+(\S+)\s+ETA\s+(\S+)"
)
DESTINATION_RE = re.compile(r"^\[download\]\s+Destination:\s+(.+)$")
MERGER_RE = re.compile(r'^\[Merger\]\s+Merging formats into\s+"(.+)"$')
PLAYLIST_ITEM_RE = re.compile(r"^\[download\]\s+Downloading item (\d+) of (\d+)")
PLAYLIST_VIDEO_RE = re.compile(r"^\[download\]\s+Downloading video (\d+) of (\d+)")


def title_from_filename(path_str: str) -> str:
    name = Path(path_str).stem
    name = re.sub(r"\.f\d+$", "", name)
    name = re.sub(r"\s*\[[A-Za-z0-9_-]{6,}\]\s*$", "", name)
    return name.strip()


def safe_path(raw: str) -> Path:
    p = Path(raw).expanduser().resolve()
    home = HOME.resolve()
    if p != home and home not in p.parents:
        raise ValueError("path must be inside your home folder")
    return p


def new_job() -> dict:
    return {
        "status": "running",
        "log": "",
        "current": {"title": "", "percent": 0.0, "speed": "", "eta": ""},
        "playlist": {"index": 0, "count": 0},
        "files": [],
    }


VIDEO_QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360"}
AUDIO_QUALITIES = {"320", "256", "192", "128"}
BROWSERS = {"none", "chrome", "safari", "firefox", "brave", "edge", "chromium", "opera", "vivaldi"}


def video_format(quality: str) -> str:
    if quality == "best" or quality not in VIDEO_QUALITIES:
        return "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
    h = quality
    return (
        f"bv*[ext=mp4][vcodec^=avc1][height<={h}]+ba[ext=m4a]/"
        f"b[ext=mp4][height<={h}]/"
        f"bv*[height<={h}]+ba/b[height<={h}]"
    )


def run_download(job_id: str, url: str, mode: str, kind: str, dest: Path, quality: str, browser: str) -> None:
    if mode == "audio":
        bitrate = quality if quality in AUDIO_QUALITIES else "192"
        fmt_args = ["-x", "--audio-format", "mp3", "--audio-quality", f"{bitrate}K"]
    else:
        fmt_args = [
            "-f", video_format(quality),
            "--merge-output-format", "mp4",
        ]

    if kind == "playlist":
        out_tmpl = "%(playlist_title|Playlist)s/%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"
        list_args = ["--yes-playlist"]
    else:
        out_tmpl = "%(title)s [%(id)s].%(ext)s"
        list_args = ["--no-playlist"]

    cookie_args = []
    if browser in BROWSERS and browser != "none":
        cookie_args = ["--cookies-from-browser", browser]

    cmd = [
        "yt-dlp",
        "--proxy", "",
        "--newline",
        "--no-color",
        "-o", str(dest / out_tmpl),
        "--print", "after_move:[FILE]%(filepath)s",
        *cookie_args,
        *list_args,
        *fmt_args,
        url,
    ]

    def handle_line(line: str) -> None:
        if not line:
            return

        if line.startswith("[FILE]"):
            fp = line[len("[FILE]"):]
            with JOBS_LOCK:
                JOBS[job_id]["files"].append(fp)
            return

        m = DOWNLOAD_PROG_RE.match(line)
        if m:
            pct_s, speed_s, eta_s = m.groups()
            try:
                pct = float(pct_s)
            except ValueError:
                pct = 0.0
            with JOBS_LOCK:
                cur = JOBS[job_id]["current"]
                cur["percent"] = pct
                cur["speed"] = speed_s
                cur["eta"] = eta_s
            return

        m = DESTINATION_RE.match(line) or MERGER_RE.match(line)
        if m:
            with JOBS_LOCK:
                JOBS[job_id]["current"]["title"] = title_from_filename(m.group(1))
                JOBS[job_id]["current"]["percent"] = 0.0
            return

        m = PLAYLIST_ITEM_RE.match(line) or PLAYLIST_VIDEO_RE.match(line)
        if m:
            with JOBS_LOCK:
                JOBS[job_id]["playlist"]["index"] = int(m.group(1))
                JOBS[job_id]["playlist"]["count"] = int(m.group(2))
            return

        with JOBS_LOCK:
            JOBS[job_id]["log"] = (JOBS[job_id]["log"] + line + "\n")[-8000:]

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=slave_fd,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env={**os.environ, "HTTP_PROXY": "", "HTTPS_PROXY": "", "http_proxy": "", "https_proxy": ""},
        )
        os.close(slave_fd)

        buf = b""
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                idx_n = buf.find(b"\n")
                idx_r = buf.find(b"\r")
                idxs = [i for i in (idx_n, idx_r) if i != -1]
                if not idxs:
                    break
                idx = min(idxs)
                line_bytes = buf[:idx]
                buf = buf[idx + 1:]
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                handle_line(line)

        if buf:
            handle_line(buf.decode("utf-8", errors="replace").rstrip())

        proc.wait()
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["log"] += f"\n[exception] {e}"
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass


INDEX_HTML = r"""<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>StreamFlow — Video Downloader</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0..1,0&display=swap" rel="stylesheet">
<script>
  tailwind.config = { darkMode: 'class' };
</script>
<style>
  :root { --primary: #3b82f6; --primary-dark: #2563eb; }
  html.dark { --primary: #528dff; --primary-dark: #afc6ff; }
  body { font-family: 'Inter', system-ui, -apple-system, sans-serif; }
  .material-symbols-outlined { font-variation-settings: 'FILL' 0; }
  .filled { font-variation-settings: 'FILL' 1; }

  /* glass cards */
  .card {
    background: rgba(255,255,255,0.92);
    backdrop-filter: blur(16px);
    border: 1px solid rgba(226,232,240,0.8);
    box-shadow: 0 10px 30px -10px rgba(15,23,42,0.06);
  }
  .dark .card {
    background: rgba(29,31,39,0.6);
    border: 1px solid rgba(255,255,255,0.06);
    box-shadow: 0 10px 30px -10px rgba(0,0,0,0.4);
  }

  /* input glow */
  .input-glow:focus-within { box-shadow: 0 0 0 3px rgba(59,130,246,0.2); border-color: #3b82f6 !important; }
  .dark .input-glow:focus-within { box-shadow: 0 0 0 2px #528dff, 0 0 15px rgba(82,141,255,0.2); border-color: #528dff !important; }

  /* segmented control */
  .seg {
    padding: .5rem 1.5rem;
    border-radius: .375rem;
    font-size: .875rem;
    font-weight: 500;
    color: #64748b;
    transition: all .15s;
    border: 1px solid transparent;
  }
  .dark .seg { color: #94a3b8; }
  .seg.active { background: white; color: #0f172a; box-shadow: 0 1px 2px rgba(0,0,0,0.06); border-color: rgba(226,232,240,0.6); }
  .dark .seg.active { background: #272a32; color: #fff; border-color: rgba(255,255,255,0.08); }

  /* progress bar pulse */
  @keyframes pulse-bar { 0%,100% { opacity: 1 } 50% { opacity: .8 } }
  .bar-fill { animation: pulse-bar 1.6s ease-in-out infinite; }

  /* select chevron */
  .select-wrap { position: relative; }
  .select-wrap select { appearance: none; -webkit-appearance: none; padding-right: 2.5rem; }
  .select-wrap .chev { position: absolute; right: .75rem; top: 50%; transform: translateY(-50%); pointer-events: none; color: #94a3b8; }
</style>
</head>
<body class="bg-slate-50 dark:bg-[#10131b] text-slate-900 dark:text-[#e1e2ed] min-h-screen">

<!-- Header -->
<header class="sticky top-0 z-30 border-b border-slate-200 dark:border-white/10 bg-white/70 dark:bg-zinc-950/70 backdrop-blur">
  <div class="max-w-3xl mx-auto px-4 h-14 flex items-center justify-between">
    <div class="flex items-center gap-2 font-bold tracking-tight">
      <span class="material-symbols-outlined filled text-blue-500">cloud_download</span>
      <span>StreamFlow</span>
    </div>
    <button id="themeToggle" aria-label="Toggle theme"
      class="p-2 rounded-lg hover:bg-slate-100 dark:hover:bg-white/5 active:scale-95 transition">
      <span id="themeIcon" class="material-symbols-outlined">dark_mode</span>
    </button>
  </div>
</header>

<main class="max-w-3xl mx-auto px-4 py-8">
  <!-- Hero -->
  <div class="text-center mb-8">
    <div class="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400 mb-4 border border-blue-100 dark:border-blue-500/20">
      <span class="material-symbols-outlined filled text-3xl">play_circle</span>
    </div>
    <h1 class="text-[32px] leading-tight font-bold mb-1">Video Downloader</h1>
    <p class="text-slate-500 dark:text-slate-400">Works with YouTube, Vimeo, TikTok, X, SoundCloud and 1800+ more.</p>
  </div>

  <!-- Card: type + URL -->
  <div class="card rounded-xl p-6 mb-4">
    <div class="flex justify-center mb-6">
      <div class="inline-flex p-1 bg-slate-100 dark:bg-zinc-800/60 rounded-lg border border-slate-200 dark:border-white/5">
        <button id="tab-single" class="seg active">🎬 Single Video</button>
        <button id="tab-playlist" class="seg">📑 Playlist</button>
      </div>
    </div>

    <label class="block text-sm font-semibold mb-2 text-slate-700 dark:text-slate-300">Video URL</label>
    <div class="input-glow rounded-lg flex items-center gap-2 px-3 h-14 bg-white dark:bg-[#1d1f27] border border-slate-300 dark:border-[#424754] transition">
      <span class="material-symbols-outlined text-slate-400">link</span>
      <input id="url" type="url" placeholder="Paste video link here..."
        class="flex-1 bg-transparent outline-none text-slate-800 dark:text-slate-100 placeholder:text-slate-400 text-base">
    </div>
  </div>

  <!-- Card: settings -->
  <div class="card rounded-xl p-6 mb-4">
    <h3 class="text-sm font-semibold mb-4 flex items-center gap-2 pb-2 border-b border-slate-200 dark:border-white/5">
      <span class="material-symbols-outlined text-blue-500 text-base">tune</span>
      Download Settings
    </h3>
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <div>
        <label class="block text-sm mb-2 text-slate-600 dark:text-slate-400">Format</label>
        <div class="select-wrap">
          <select id="mode" class="w-full bg-white dark:bg-[#1d1f27] border border-slate-300 dark:border-[#424754] rounded-lg h-12 px-4 outline-none focus:border-blue-500 transition cursor-pointer">
            <option value="video">MP4 Video</option>
            <option value="audio">MP3 Audio</option>
          </select>
          <span class="chev material-symbols-outlined">expand_more</span>
        </div>
      </div>
      <div>
        <label class="block text-sm mb-2 text-slate-600 dark:text-slate-400">Quality</label>
        <div class="select-wrap">
          <select id="qVideo" class="w-full bg-white dark:bg-[#1d1f27] border border-slate-300 dark:border-[#424754] rounded-lg h-12 px-4 outline-none focus:border-blue-500 transition cursor-pointer">
            <option value="best">Best quality</option>
            <option value="2160">2160p (4K)</option>
            <option value="1440">1440p (2K)</option>
            <option value="1080" selected>1080p (FHD)</option>
            <option value="720">720p (HD)</option>
            <option value="480">480p</option>
            <option value="360">360p</option>
          </select>
          <select id="qAudio" class="hidden w-full bg-white dark:bg-[#1d1f27] border border-slate-300 dark:border-[#424754] rounded-lg h-12 px-4 outline-none focus:border-blue-500 transition cursor-pointer">
            <option value="320">320 kbps</option>
            <option value="256">256 kbps</option>
            <option value="192" selected>192 kbps</option>
            <option value="128">128 kbps</option>
          </select>
          <span class="chev material-symbols-outlined">expand_more</span>
        </div>
      </div>
      <div class="sm:col-span-2">
        <label class="block text-sm mb-2 text-slate-600 dark:text-slate-400 flex items-center gap-1">
          Sign-in cookies
          <span class="text-xs text-slate-400" title="Reads cookies from your browser so the site recognises you as signed in">(use when the site asks you to sign in)</span>
        </label>
        <div class="select-wrap">
          <select id="browser" class="w-full bg-white dark:bg-[#1d1f27] border border-slate-300 dark:border-[#424754] rounded-lg h-12 px-4 outline-none focus:border-blue-500 transition cursor-pointer">
            <option value="chrome" selected>Chrome</option>
            <option value="safari">Safari</option>
            <option value="firefox">Firefox</option>
            <option value="brave">Brave</option>
            <option value="edge">Edge</option>
            <option value="chromium">Chromium</option>
            <option value="opera">Opera</option>
            <option value="vivaldi">Vivaldi</option>
            <option value="none">None (no cookies)</option>
          </select>
          <span class="chev material-symbols-outlined">expand_more</span>
        </div>
      </div>
    </div>
  </div>

  <!-- Card: save path -->
  <div class="card rounded-xl p-4 mb-4 flex items-center justify-between gap-3">
    <div class="flex items-center gap-3 min-w-0 flex-1">
      <span class="material-symbols-outlined text-slate-400 flex-shrink-0">folder_open</span>
      <div class="min-w-0">
        <div class="text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400 font-semibold mb-0.5">Save to</div>
        <div id="dest" class="text-sm font-mono truncate text-slate-700 dark:text-slate-200"></div>
      </div>
    </div>
    <button id="pick" class="px-4 py-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-white/5 rounded-md border border-slate-200 dark:border-[#424754] flex-shrink-0 transition">Change</button>
  </div>

  <!-- Action -->
  <button id="go"
    class="w-full h-14 bg-blue-600 hover:bg-blue-700 active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed disabled:active:scale-100 text-white rounded-xl font-medium shadow-lg shadow-blue-600/20 hover:shadow-blue-600/30 flex items-center justify-center gap-2 transition">
    <span class="material-symbols-outlined filled">download</span>
    <span>Download Now</span>
  </button>

  <div id="out"></div>
</main>

<!-- Folder picker modal -->
<div id="modal" class="fixed inset-0 bg-black/50 z-40 hidden items-center justify-center p-4">
  <div class="bg-white dark:bg-[#1d1f27] text-slate-900 dark:text-slate-100 rounded-2xl w-full max-w-lg max-h-[80vh] flex flex-col p-4 gap-3 border border-slate-200 dark:border-white/10 shadow-2xl">
    <div class="flex items-center">
      <strong>Pick a folder</strong>
      <button id="upBtn" class="ml-auto px-3 py-1.5 text-sm rounded-md border border-slate-200 dark:border-[#424754] hover:bg-slate-50 dark:hover:bg-white/5 flex items-center gap-1">
        <span class="material-symbols-outlined text-base">arrow_upward</span> Up
      </button>
    </div>
    <div id="crumbs" class="font-mono text-xs px-3 py-2 bg-slate-100 dark:bg-zinc-900/60 rounded-md break-all"></div>
    <div id="folders" class="flex-1 overflow-auto border border-slate-200 dark:border-[#424754] rounded-lg min-h-[200px]"></div>
    <div class="flex gap-2">
      <input id="newName" type="text" placeholder="New folder name…"
        class="flex-1 px-3 py-2 rounded-md border border-slate-200 dark:border-[#424754] bg-white dark:bg-[#10131b] outline-none focus:border-blue-500 text-sm">
      <button id="mkBtn" class="px-3 py-2 rounded-md border border-slate-200 dark:border-[#424754] hover:bg-slate-50 dark:hover:bg-white/5 text-sm">Create folder</button>
    </div>
    <div class="flex justify-between gap-2">
      <button id="cancelBtn" class="px-4 py-2 rounded-md border border-slate-200 dark:border-[#424754] hover:bg-slate-50 dark:hover:bg-white/5 text-sm">Cancel</button>
      <button id="selectBtn" class="px-4 py-2 rounded-md bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium">Select this folder</button>
    </div>
  </div>
</div>

<script>
// ----- theme -----
const root = document.documentElement;
const tIcon = document.getElementById('themeIcon');
function applyTheme(t) {
  root.classList.toggle('dark', t === 'dark');
  tIcon.textContent = t === 'dark' ? 'light_mode' : 'dark_mode';
  localStorage.setItem('theme', t);
}
applyTheme(localStorage.getItem('theme') ||
  (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
document.getElementById('themeToggle').onclick = () =>
  applyTheme(root.classList.contains('dark') ? 'light' : 'dark');

// ----- state -----
const DEFAULT_DIR = __DEFAULT_DIR__;
let dest = DEFAULT_DIR;
let browsing = DEFAULT_DIR;
let kind = 'single';

const destEl = document.getElementById('dest');
const modal = document.getElementById('modal');
const crumbs = document.getElementById('crumbs');
const folders = document.getElementById('folders');
const newName = document.getElementById('newName');
const tabSingle = document.getElementById('tab-single');
const tabPlaylist = document.getElementById('tab-playlist');

destEl.textContent = dest;

function setKind(k) {
  kind = k;
  tabSingle.classList.toggle('active', k === 'single');
  tabPlaylist.classList.toggle('active', k === 'playlist');
}
tabSingle.onclick = () => setKind('single');
tabPlaylist.onclick = () => setKind('playlist');

const modeEl = document.getElementById('mode');
const qVideo = document.getElementById('qVideo');
const qAudio = document.getElementById('qAudio');
function syncQuality() {
  const isAudio = modeEl.value === 'audio';
  qVideo.classList.toggle('hidden', isAudio);
  qAudio.classList.toggle('hidden', !isAudio);
}
modeEl.onchange = syncQuality; syncQuality();

// ----- folder picker -----
function openModal() { modal.classList.remove('hidden'); modal.classList.add('flex'); listDir(); }
function closeModal() { modal.classList.add('hidden'); modal.classList.remove('flex'); }
document.getElementById('pick').onclick = () => { browsing = dest; openModal(); };
document.getElementById('cancelBtn').onclick = closeModal;
document.getElementById('selectBtn').onclick = () => { dest = browsing; destEl.textContent = dest; closeModal(); };
document.getElementById('upBtn').onclick = async () => {
  const parts = browsing.split('/').filter(Boolean);
  if (parts.length <= 1) return;
  parts.pop();
  browsing = '/' + parts.join('/');
  await listDir();
};
document.getElementById('mkBtn').onclick = async () => {
  const name = newName.value.trim();
  if (!name) return;
  const r = await fetch('/mkdir', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({path: browsing, name}),
  });
  const j = await r.json();
  if (j.error) { alert(j.error); return; }
  newName.value = '';
  browsing = j.path;
  await listDir();
};
async function listDir() {
  crumbs.textContent = browsing;
  folders.innerHTML = '<div class="p-3 text-slate-400 italic text-sm">loading…</div>';
  const r = await fetch('/list?path=' + encodeURIComponent(browsing));
  const j = await r.json();
  if (j.error) { folders.innerHTML = `<div class="p-3 text-red-500 text-sm">${j.error}</div>`; return; }
  folders.innerHTML = '';
  if (j.dirs.length === 0) { folders.innerHTML = '<div class="p-3 text-slate-400 italic text-sm">(no subfolders)</div>'; return; }
  for (const name of j.dirs) {
    const div = document.createElement('div');
    div.className = 'px-3 py-2.5 cursor-pointer border-b border-slate-100 dark:border-white/5 last:border-b-0 hover:bg-slate-50 dark:hover:bg-white/5 text-sm flex items-center gap-2';
    div.innerHTML = '<span class="material-symbols-outlined text-blue-500 text-base">folder</span><span class="truncate"></span>';
    div.querySelector('span.truncate').textContent = name;
    div.onclick = async () => {
      browsing = (browsing.endsWith('/') ? browsing : browsing + '/') + name;
      await listDir();
    };
    folders.appendChild(div);
  }
}

// ----- download -----
document.getElementById('go').onclick = async () => {
  const url = document.getElementById('url').value.trim();
  const mode = modeEl.value;
  const quality = (mode === 'audio') ? qAudio.value : qVideo.value;
  const browser = document.getElementById('browser').value;
  if (!url) { alert('Paste a URL first.'); return; }
  const out = document.getElementById('out');
  const go = document.getElementById('go');
  go.disabled = true;

  out.innerHTML = `
    <div class="card mt-6 rounded-xl p-6">
      <h3 class="font-semibold mb-3 flex items-center gap-2">
        <span class="material-symbols-outlined text-blue-500">downloading</span>Downloading…
      </h3>
      <div id="pl" class="text-sm text-blue-600 dark:text-blue-400 font-semibold mb-1"></div>
      <div id="ptitle" class="text-sm break-words mb-3 min-h-[1.2em]">starting…</div>
      <div class="h-3 bg-slate-200 dark:bg-white/10 rounded-full overflow-hidden">
        <div id="bar" class="h-full bg-gradient-to-r from-blue-500 to-blue-600 bar-fill transition-all" style="width:0%"></div>
      </div>
      <div class="flex justify-between text-xs text-slate-500 dark:text-slate-400 mt-2">
        <span id="pct">0%</span>
        <span><span id="speed"></span> · ETA <span id="eta">--</span></span>
      </div>
      <div id="files" class="mt-4 max-h-48 overflow-auto text-xs font-mono space-y-1"></div>
    </div>
    <pre id="log" class="mt-3 p-3 rounded-lg bg-slate-100 dark:bg-zinc-900/60 text-xs max-h-48 overflow-auto whitespace-pre-wrap break-words text-slate-600 dark:text-slate-400"></pre>`;

  const bar = document.getElementById('bar');
  const pct = document.getElementById('pct');
  const speed = document.getElementById('speed');
  const eta = document.getElementById('eta');
  const ptitle = document.getElementById('ptitle');
  const pl = document.getElementById('pl');
  const filesEl = document.getElementById('files');
  const log = document.getElementById('log');

  const r = await fetch('/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({url, mode, dest, kind, quality, browser}),
  });
  const j = await r.json();
  if (j.error) { log.textContent = j.error; go.disabled = false; return; }
  const job_id = j.job_id;
  let lastFiles = 0;

  const poll = setInterval(async () => {
    const s = await fetch('/status?id=' + job_id).then(x => x.json());
    log.textContent = s.log || '';
    log.scrollTop = log.scrollHeight;
    const c = s.current || {};
    bar.style.width = (c.percent || 0) + '%';
    pct.textContent = (c.percent || 0).toFixed(1) + '%';
    speed.textContent = c.speed || '';
    eta.textContent = c.eta || '--';
    if (c.title) ptitle.textContent = c.title;
    if (s.playlist && s.playlist.count > 0) {
      pl.textContent = `Item ${s.playlist.index} of ${s.playlist.count}`;
    }
    if (s.files && s.files.length > lastFiles) {
      for (let i = lastFiles; i < s.files.length; i++) {
        const d = document.createElement('div');
        d.className = 'truncate text-emerald-600 dark:text-emerald-400';
        d.textContent = '✓ ' + s.files[i];
        filesEl.appendChild(d);
      }
      lastFiles = s.files.length;
      filesEl.scrollTop = filesEl.scrollHeight;
    }
    if (s.status === 'done') {
      clearInterval(poll);
      go.disabled = false;
      bar.style.width = '100%';
      bar.classList.remove('bar-fill');
      pct.textContent = '100%';
      const div = document.createElement('div');
      div.className = 'mt-3 p-3 rounded-lg bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/30 text-emerald-700 dark:text-emerald-300 text-sm flex items-center gap-2';
      div.innerHTML = `<span class="material-symbols-outlined">check_circle</span>Done — ${s.files.length} file${s.files.length === 1 ? '' : 's'} saved.`;
      out.appendChild(div);
    } else if (s.status === 'error') {
      clearInterval(poll);
      go.disabled = false;
      bar.classList.remove('bar-fill');
      const div = document.createElement('div');
      div.className = 'mt-3 p-3 rounded-lg bg-red-50 dark:bg-red-500/10 border border-red-200 dark:border-red-500/30 text-red-700 dark:text-red-300 text-sm flex items-center gap-2';
      div.innerHTML = '<span class="material-symbols-outlined">error</span>Failed — see log above.';
      out.appendChild(div);
    }
  }, 700);
};
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            html = INDEX_HTML.replace("__DEFAULT_DIR__", json.dumps(str(DEFAULT_DIR)))
            self._send(200, html.encode())
        elif self.path.startswith("/status"):
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            jid = (qs.get("id") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                snapshot = json.loads(json.dumps(job)) if job else {"status": "missing"}
            self._json(200, snapshot)
        elif self.path.startswith("/list"):
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            raw = (qs.get("path") or [str(DEFAULT_DIR)])[0]
            try:
                p = safe_path(raw)
                if not p.is_dir():
                    raise ValueError("not a folder")
                dirs = sorted(
                    [e.name for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")],
                    key=str.casefold,
                )
                self._json(200, {"path": str(p), "dirs": dirs})
            except Exception as e:
                self._json(200, {"error": str(e), "dirs": []})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        params = parse_qs(body)

        if self.path == "/start":
            url = (params.get("url") or [""])[0].strip()
            mode = (params.get("mode") or ["video"])[0]
            kind = (params.get("kind") or ["single"])[0]
            quality = (params.get("quality") or ["best"])[0]
            browser = (params.get("browser") or ["chrome"])[0]
            if browser not in BROWSERS:
                browser = "none"
            dest_raw = (params.get("dest") or [str(DEFAULT_DIR)])[0]
            if kind not in ("single", "playlist"):
                kind = "single"
            if not url:
                self._json(400, {"error": "missing url"}); return
            try:
                dest = safe_path(dest_raw)
                if not dest.is_dir():
                    raise ValueError("destination is not a folder")
            except Exception as e:
                self._json(400, {"error": f"bad destination: {e}"}); return
            job_id = uuid.uuid4().hex[:12]
            with JOBS_LOCK:
                JOBS[job_id] = new_job()
            threading.Thread(target=run_download, args=(job_id, url, mode, kind, dest, quality, browser), daemon=True).start()
            self._json(200, {"job_id": job_id})

        elif self.path == "/mkdir":
            parent_raw = (params.get("path") or [""])[0]
            name = (params.get("name") or [""])[0].strip()
            if not name or "/" in name or name in (".", ".."):
                self._json(400, {"error": "invalid folder name"}); return
            try:
                parent = safe_path(parent_raw)
                new_dir = (parent / name).resolve()
                safe_path(str(new_dir))
                new_dir.mkdir(parents=False, exist_ok=False)
                self._json(200, {"path": str(new_dir)})
            except FileExistsError:
                self._json(400, {"error": "folder already exists"})
            except Exception as e:
                self._json(400, {"error": str(e)})
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Video downloader running at http://127.0.0.1:{PORT}")
    print(f"Default save folder: {DEFAULT_DIR}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
