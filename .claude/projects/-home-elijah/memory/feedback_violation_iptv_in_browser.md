---
name: feedback_violation_iptv_in_browser
description: ⚠️ VIOLATION CAPTURED 2026-05-28: IPTV stream opened in browser instead of MPV
metadata:
  type: feedback
---

Stream/IPTV URLs must ALWAYS launch via MPV — never in a browser tab. Any .ts/.m3u8/.m3u URL = MPV command.

**Why:** Violation caught on 2026-05-28. Rule: 'open [channel/stream]' = query XTREAM API → nohup mpv <url> &.

**How to apply:** When operator says 'open [any channel/sport/stream]': query XTREAM API first, build MPV command, launch via Bash. Never open a sensei tab for a stream URL.

Last captured: 2026-05-28 19:18:25
