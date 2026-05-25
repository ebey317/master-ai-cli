---
name: project_iptv_espn_launch
description: IPTV via MPV — operator has XTREAM IPTV, always use MPV for any sports/TV stream, NEVER open a browser
metadata: 
  node_type: memory
  type: project
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

## ⚠️ DEFAULT RULE — always use MPV, never the browser

When operator asks to "open [any channel/sport/stream]" — query XTREAM API first, launch with MPV. Do NOT open a browser tab. The browser path was corrected 2026-05-25.

Stream URL format: `{server}/live/{user}/{pass}/{stream_id}.ts`

To find streams: `curl -s "{server}/player_api.php?username={user}&password={pass}&action=get_live_streams" | python3 -c "import json,sys; [print(c['stream_id'], c['name']) for c in json.load(sys.stdin) if 'keyword' in c['name'].lower()]"`

## NBA channels (trex provider)
- NBA TV HD: `749756`
- NBA TV HDTV RAW: `1920053`
- NBA RAW: `1920052`
- NBA PASS PPV 1 (live game slot): `1955375`
- NBA PASS PPV 2: `1955374`
- NBA FAST CHANNEL: `1924248`

## Opening ESPN

**Command:**
```bash
nohup mpv "http://line.plugtv.xyz/live/28fa070c23/d6e9d5fb7ee4/1921356.ts" > /tmp/espn_mpv.log 2>&1 &
```

- Stream ID: `1921356` = `US| ESPN ᴴᴰ ⁶⁰ᶠᵖˢ` from provider `trex`
- Hardware decoding: vaapi, 1280x720

**"Small window"** = user resizes MPV manually to their preferred small size. When user says "open ESPN in a small window" or "open X in a small window" — launch MPV and they'll size it. Do NOT try to force a window size via flags unless asked.

## Other ESPN channels available
- ESPN 2 HD: `45580`
- ESPN NEWS HD: `45578`  
- ESPN U: `90958`
- ESPN 60fps: `1921356` ← default

## XTREAM providers
- `trex`: `http://line.plugtv.xyz` / `28fa070c23` / `d6e9d5fb7ee4`
- `mega`: `http://rnnathyt.sqhsm.com` / `GarrySr` / `UDU8TAGE`

Stream URL format: `{server}/live/{user}/{pass}/{stream_id}.ts`

[[feedback_check_system_before_open]]
[[project_jellyfin_local_library_plan]]
