---
name: project_command_center
description: Command Center — AI-powered universal voice remote. Physical + phone app. Voice commands all streaming services and IPTV from one place.
metadata: 
  node_type: memory
  type: project
  originSessionId: 9c4347ef-805a-4fd8-8434-f3ecfb6d8174
---

# Command Center

**Status:** Concept locked 2026-05-28. In portfolio. Not building yet.

**The product:** Universal voice remote — physical device + phone app. User programs 4-6 clear buttons (their subscriptions). Three fixed categories: Live TV, Movies, Series. Directional pad for manual browse. Voice always live.

**What it is NOT:** A player. A replacement app. It's a commander — sits on top of everything the user already has.

**Why:** Every household has 4-6 subscriptions buried in separate apps. Nobody unified them with voice from a physical remote. Roku/Amazon/Apple tried as platforms. Nobody made it a standalone branded remote product.

**Target market:** Anyone with IPTV + streaming subscriptions. Physical remote sold at retail long-term. Phone app ships first.

**Revenue:** Physical remote hardware margin + app (free or one-time purchase) + programmable tile packs.

**Franchise vision:** Remote on shelf at Walmart. Brand that sits on top of all streaming.

---

## Backend Architecture (the circus underneath)

**The full signal chain:**

```
Voice input
    ↓
Speech-to-text (Whisper local)
    ↓
Intent parser (Claude/Qwen LLM)
    → {service: "iptv", query: "ESPN", action: "play"}
    ↓
Router
    ↓
Executor (per service)
    ↓
Output (MPV / Roku ECP / deep link)
```

**Layer 1 — Voice:** Microphone on remote → Bluetooth → phone hub → Whisper transcribes

**Layer 2 — Intent parser:** LLM takes raw text, returns structured JSON: service + query + action. Claude handles this already.

**Layer 3 — Router:** Looks at `service` field, routes to correct executor

**Layer 4 — Executors:**
- IPTV: search ~/.iptv/channels.m3u → find stream URL → send to MPV or Roku
- Roku ECP: POST to http://ROKU_IP:8060/launch/[channel_id] → send keypresses
- Netflix: launch channel ID 12 on Roku via ECP
- Hulu: launch channel ID on Roku via ECP
- MPV: subprocess call, plays stream URL directly

**Layer 5 — Players:** MPV (desktop, already working), Roku ECP (TV)

---

## What Elijah Already Has (zero build needed)

- ~/.iptv/channels.m3u — M3U playlist ✅
- XTREAM credentials ✅
- MPV working, Claude can already open ESPN ✅
- Roku TV ✅
- Claude as intent parser ✅

## What Needs Building

1. Whisper voice pipeline (local speech-to-text)
2. M3U channel search function (grep/fuzzy match on playlist)
3. Roku ECP client (~50 lines Python)
4. Deep link generator for Netflix/Hulu/Prime on Roku
5. Router connecting all executors
6. Phone app with mic input + voice UI

## Technical Gaps

- Roku IP discovery on local network (mDNS scan)
- Netflix content ID lookup for specific titles (their API or TMDB)
- Packaging as standalone phone app (Flutter)

**Why:** Operator has MPV + IPTV + Claude already wired. This is connecting existing pieces, not rebuilding from scratch.

---

## The TV Extension — Architecture Completed 2026-05-28

The missing piece that completes the product. Three parts:

**1. THE REMOTE** — physical or phone. Voice input. Programmable buttons. Sends commands.

**2. THE TV EXTENSION** — installed on the TV itself.
- Reads what apps/services are available on that TV
- Receives commands from the remote over WiFi
- Executes — launches apps, plays content, navigates
- Platform targets: Fire Stick (APK sideload), Android TV (Play Store), Roku (Roku channel SDK), Samsung (Tizen), LG (webOS), Apple TV (tvOS)
- One codebase, six platform builds

**3. THE CLOUD BRAIN** — content resolution engine
- Customer searches any title → queries all connected services simultaneously
- Priority stack: subscription beats rental, highest quality wins, TV capability checked first
- Quality scoring: 4K HDR > 4K > 1080p > 720p > SD > unknown
- IPTV/local wins only when no legitimate source has the title
- Result: best version plays automatically. Customer never chooses.

**Signal chain:** Remote → Cloud Brain → TV Extension → Screen plays

---

## Market Context

**Logitech Harmony** was the last successful universal remote. Shut down 2021. Left the market empty. Command Center is Harmony rebuilt for the streaming era — voice-first, AI-powered, content-aware.

**The moat:** The TV Extension installed on millions of TVs talking to Command Center. Once that network exists, nobody replicates it without starting over. The extension IS the defensible asset.

---

## Card UI + Two-Tap Flow — Added 2026-05-28

### Browse Layer (card grid)
Content displayed as cards — image, title, year. Same pattern as Game Pass, Netflix home, Apple TV. IPTV live channels mixed into same grid as subscription content. No separation. One unified browsable surface.

### Detail Layer (card opens on click)
User clicks a card → full detail screen:
- Title, year, rating, runtime, genre
- Description (readable, not buried)
- WHERE TO WATCH section with checkmarks:
  - ✅ = user has it, available now
  - ❌ = not in user's plan or not available on that service

### Two Actions — never three taps
- [▶ PLAY] → auto-picks best ✅ source by priority, plays immediately
- [CHOOSE SOURCE] → user highlights a specific ✅ and confirms

### Why the ❌ rows matter
Customer sees Hulu doesn't have it without going to Hulu. Sees Netflix requires upgrade without going to Netflix. Frustration intercepted before it happens. Trust built before anything plays.

### Market comparison
JustWatch does exactly this — shows where to watch anything across all services. JustWatch does NOT play anything. Command Center plays it. That's the gap JustWatch left open. Command Center = JustWatch + playback + IPTV + voice + the remote.

---

## Universal Guide Model — Added 2026-05-28

### The Main UI IS the Product
The 6 programmable buttons are shortcuts. The main UI already has everything. M3U is the backbone — enriched with subscription metadata so every title knows what it costs and where it lives.

### M3U Enrichment Layer
```
Raw M3U entry → Command Center enriches it:

"ESPN HD"     → ESPN HD | LIVE  | IPTV ✅ free
"Avatar"      → Avatar  | MOVIE | IPTV SD ✅ free
                                | Netflix 4K 💳 paid
                                | Prime HD 💳 paid
"Game Tonight"→ Game 7  | LIVE  | ESPN/IPTV ✅ free
                                | Hulu Live 💳 paid
```

### The Paid/Free Flag = API Key Router
- ✅ free → Command Center's own player (IPTV/M3U stream)
- 💳 paid → service native player via that service's API key
- ❓ unknown → flagged, never guessed, never played blind

One flag. One decision. No cross-contamination. No infringement.

### Legal Defense — Clean
Command Center is a GUIDE and LAUNCHER. Never touches premium stream data. Same model as Google TV, Apple TV, Fire TV home screen. They show everything, launch the licensed player. Nobody sues Google TV for showing what's on Netflix. Command Center is that — plus voice, plus IPTV, plus the ring.

When user clicks Netflix title → Netflix API key fires → Netflix native player opens → Netflix plays it. Command Center never touched the stream. Zero infringement.

### 6 Buttons = Guide Shortcuts
Buttons jump to sections of the guide faster:
- Button 1 → LIVE section
- Button 2 → MOVIES section  
- Button 3 → SERIES section
- Button 4-6 → user's most-used services
Guide already has everything. Buttons are fast lanes, not requirements.

---

## TV Extension UI Layer — Added 2026-05-28

### The Ring — always visible, never leaves
Persistent animated border around the entire screen. Present regardless of what service is running.
- Slow white pulse = active, nothing locked
- Solid colored = locked into a service (Hulu green, Netflix red, etc.)
- Fast pulse = mic open, listening

TV turns on → ring appears immediately. Signals: Command Center is alive. This remote is in charge.

### Three Screen States

**STATE 1 — MAIN SURFACE (nothing locked)**
Command Center's OWN UI. Full content grid — Live TV, Movies, Series. All subscriptions in one place. Browse with arrows or hold to talk. Ring: slow white pulse.

**STATE 2 — LOCKED INTO SERVICE (pressed once)**
Service's native UI runs full screen. Ring stays, changes to service color. Arrows browse that service natively. Hold = voice scoped to that service. Command Center steps back. Ring never leaves.

**STATE 3 — LISTENING (press and hold)**
Ring fast-pulses. Mic open. 5-second window. Say the content. Done.

### The Player — two tracks
- IPTV / M3U streams → Command Center's OWN player. Fully branded. CC owns that experience completely.
- Netflix / Hulu / Prime → their native player. DRM requirement — legally cannot bypass. CC launches it, steps back, ring stays. Compliance not weakness.

### Subscription Expiry — CC catches it
Service error intercepted by extension before hitting screen. Customer never sees raw service error. They see a clean Command Center card:
"⚠ Hulu — Subscription inactive — [Renew] [Remove]"
Unified error handling. One look. Command Center branded.

### Center Button — nothing selected
Loads Command Center HOME SCREEN. Full content grid, everything subscribed to, all in one place. Browse with arrows OR hold to talk. "Find something good" → AI picks based on history. Center button = front door. Never need to know which service has what.

**One line:** CC has its own UI, its own player for IPTV, its own ring that never leaves — subscription services run under the ring in their native UI. You never lose the remote. You never lose the brand.

---

## Session Locking Model — Added 2026-05-28

**Core principle:** One active session at a time. One API key open at a time. No cross-contamination.

```
LOCKED STATE (press service button once)
─────────────────────────────────────────
Service owns the remote surface
Arrow keys browse that service
Hold to talk = scoped voice, that service only
All other API keys dormant
Command Center steps back — service runs itself

UNLOCKED STATE (press twice = close, or never opened one)
─────────────────────────────────────────
Remote surface belongs to Command Center
Hold to talk = full orchestration mode
"Find Avatar" = searches ALL sources simultaneously
Content resolution engine picks best version
Local M3U active as one of many sources
Command Center owns the remote, services are tools
```

**Why this solves the 4K/quality problem:**
- Locked into Hulu → Hulu manages resolution, Command Center doesn't interfere
- On main surface → Command Center checks TV capability, scores all versions, picks winner

**API key protection:**
- One session open = one key active = no bleed between services
- No Hulu billing Netflix activity. No IPTV stream conflicting with Prime.
- Clean, isolated, auditable per session.

**One line:** `LOCKED = service owns remote. UNLOCKED = Command Center owns remote.`

---

## Button Gesture Layer — Added 2026-05-28

Every programmable button has three states. No menu needed. Muscle memory.

```
PRESS ONCE       → ON  (open / launch / select)
PRESS TWICE      → OFF (close / exit / back to home)
PRESS AND HOLD   → RECORD VOICE (5-second mic window, scoped to that button's service)
```

Universal across every button. Customer learns it once, applies everywhere.
Press and hold Netflix button → mic opens scoped to Netflix → say "Avatar" not "open Netflix Avatar."
Press and hold Live TV button → mic opens scoped to IPTV → say "ESPN" not "open live TV ESPN."
Button sets the context. Voice carries only the content request.

**Why this matters for the sale:**
- Customer never has to say the service name again after setup
- Button IS the context. Triple tap = you're already inside Netflix asking for content.
- Faster than any voice assistant on the market today — Alexa still makes you say the full command every time.

**How auto-programming works:**
- Customer signs into Netflix in the app → app configures the Netflix button automatically
- Button knows: single = launch, hold = exit, triple = Netflix voice scope
- No manual programming. No blinking lights. No TV pointing.
- Physical remote buttons = mirror of app buttons. Same gestures both places.

---

## Onboarding Flow (customer experience)

1. Buy remote, download app on phone
2. App pairs to remote (QR or 4-digit code)
3. App asks which streaming services they have
4. OAuth login per service (Command Center never sees passwords)
5. For IPTV: paste M3U link or XTREAM code (provider emails this at signup)
6. Install TV Extension on their TV from app store
7. Done. Remote knows everything.

**M3U auto-discovery:** Not automatic — customer pastes it once. 30-second task.
