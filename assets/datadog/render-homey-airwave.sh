#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd)
frames=$(mktemp -d)
mkdir -p "$frames/png"

for frame in 0 1 2 3 4 5 6 7; do
  angle=$((frame * 45))
  offset=$((frame * 26))
  next=$((offset + 208))
  cat >"$frames/frame-$frame.svg" <<SVG
<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
  <defs>
    <linearGradient id="bg" x2="0" y2="1"><stop stop-color="#121820"/><stop offset="1" stop-color="#0b1016"/></linearGradient>
    <linearGradient id="duct" x2="1"><stop stop-color="#27343f"/><stop offset=".5" stop-color="#384956"/><stop offset="1" stop-color="#25323d"/></linearGradient>
    <filter id="glow"><feGaussianBlur stdDeviation="7" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  </defs>
  <rect width="960" height="540" rx="36" fill="url(#bg)"/>
  <path d="M55 76H905" stroke="#34414c" stroke-width="2"/>
  <text x="58" y="55" fill="#dce8ef" font-family="Arial,sans-serif" font-size="26" font-weight="700">HOMEY AIRWAVE</text>
  <text x="902" y="55" fill="#74d6ff" text-anchor="end" font-family="Arial,sans-serif" font-size="18">COMPRESSOR OFF · FAN MOVING</text>

  <rect x="55" y="145" width="235" height="265" rx="24" fill="#182129" stroke="#59656e" stroke-width="3"/>
  <circle cx="172" cy="250" r="71" fill="#10161c" stroke="#53616c" stroke-width="8"/>
  <path d="M172 191v118M113 250h118M130 208l84 84M214 208l-84 84" stroke="#64727d" stroke-width="8" stroke-linecap="round"/>
  <path d="M103 181l138 138" stroke="#ff6b62" stroke-width="13" stroke-linecap="round" filter="url(#glow)"/>
  <text x="172" y="368" fill="#ff8c83" text-anchor="middle" font-family="Arial,sans-serif" font-size="22" font-weight="700">COOLING STOPPED</text>

  <path d="M290 218H600V342H290" fill="url(#duct)" stroke="#60717d" stroke-width="4"/>
  <path d="M310 237H580M310 323H580" stroke="#1a252d" stroke-width="3" opacity=".7"/>
  <g fill="#67d7ff" filter="url(#glow)">
    <path d="M$((300 + offset % 208)) 262h34l22 18-22 18h-34l22-18z"/>
    <path d="M$((300 + next % 208)) 262h34l22 18-22 18h-34l22-18z"/>
  </g>

  <rect x="600" y="132" width="305" height="296" rx="28" fill="#17242d" stroke="#4f91a7" stroke-width="4"/>
  <circle cx="752" cy="272" r="96" fill="#0e171d" stroke="#31596a" stroke-width="8"/>
  <g transform="translate(752 272) rotate($angle)" fill="#7adfff" filter="url(#glow)">
    <path d="M0-15C24-80 72-80 67-35C63-4 30 11 0 15Z"/>
    <path d="M15 0C80 24 80 72 35 67C4 63-11 30-15 0Z"/>
    <path d="M0 15C-24 80-72 80-67 35C-63 4-30-11 0-15Z"/>
    <path d="M-15 0C-80-24-80-72-35-67C-4-63 11-30 15 0Z"/>
    <circle r="19" fill="#d8f6ff"/>
  </g>
  <text x="752" y="398" fill="#8ce6ff" text-anchor="middle" font-family="Arial,sans-serif" font-size="22" font-weight="700">BLOWER RUNNING</text>

  <circle cx="68" cy="478" r="7" fill="#63db91"/>
  <text x="88" y="486" fill="#aab8c2" font-family="Arial,sans-serif" font-size="19">Residual cool air is still moving through the home</text>
  <text x="905" y="486" fill="#667783" text-anchor="end" font-family="Arial,sans-serif" font-size="17">10 minute fan hold</text>
</svg>
SVG
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --headless --disable-gpu --hide-scrollbars \
    --window-size=960,540 \
    --screenshot="$frames/png/frame-$frame.png" \
    "file://$frames/frame-$frame.svg" >/dev/null 2>&1
done

ffmpeg -hide_banner -loglevel error -y -framerate 8 -i "$frames/png/frame-%d.png" \
  -vf "fps=8,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" \
  -loop 0 "$root/homey-airwave.gif"
