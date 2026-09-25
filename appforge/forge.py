#!/usr/bin/env python3
"""AppForge — terminal wizard that turns answers into a shippable app.

Flow:
  1. Ask a sequence of scoping questions (what / who / features / AI? / look)
  2. Cache answers to ~/.appforge_sessions/<slug>_<ts>.json so you can
     start on your phone (SSH) and resume on your desktop later
  3. Render the Flask starter template, filling placeholders from answers
  4. Emit ~/Desktop/<slug>.zip + ~/Desktop/<slug>_NEXT_STEPS.md
     (Gumroad + App Store / Play Store submission checklist)

v0.1: templates-only. AI-generated code comes in v0.2.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE / "templates" / "flask_starter"
SESSION_DIR = Path.home() / ".appforge_sessions"
OUT_DIR = Path.home() / "Desktop"

# ── colors (work on light + dark terminals) ──────────────────────
C = "\033[36m"
G = "\033[32m"
Y = "\033[33m"
R = "\033[31m"
BC = "\033[1;34m"
X = "\033[0m"
D = "\033[2m"


# ── question catalog ─────────────────────────────────────────────
# Each question: (key, prompt, default, help_line).
# Keep the list human-readable; each answer lands in the session JSON.
QUESTIONS = [
    (
        "name",
        "What's your app called?",
        "MyApp",
        "Short name — becomes the zip filename and the title bar.",
    ),
    (
        "tagline",
        "One-sentence tagline — what does it do?",
        "Does a thing that helps a person",
        "Shown on Gumroad listing + splash page.",
    ),
    (
        "audience",
        "Who is this for? (kid / non-technical adult / developer / business)",
        "non-technical adult",
        "Shapes tone and the defaults we pick.",
    ),
    (
        "platform",
        "Where does it run? (web / desktop / mobile / cli)",
        "web",
        "v0.1 only outputs a Flask web app; other values recorded for v0.2.",
    ),
    (
        "features",
        "Top 3 features, comma-separated.",
        "login, list view, form",
        "Each feature becomes a stub route + placeholder UI card.",
    ),
    (
        "needs_ai",
        "Does this app need AI? (y/n)",
        "n",
        "If y, we wire in a /ask endpoint that calls your local Ollama.",
    ),
    (
        "data_store",
        "Where does data live? (sqlite / json-file / none)",
        "sqlite",
        "sqlite gets you a DB file next to app.py; json-file is simpler.",
    ),
    (
        "color",
        "Primary brand color (hex or name).",
        "#2266cc",
        "Used for buttons + header on the splash page.",
    ),
    (
        "price",
        "Gumroad price in USD (0 for free).",
        "0",
        "Dropped into the NEXT_STEPS file for listing.",
    ),
    (
        "creator",
        "Your name / creator handle.",
        "Anonymous",
        "Goes in the README + footer.",
    ),
]


# ── session helpers ──────────────────────────────────────────────
def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s or "myapp"


def list_sessions():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SESSION_DIR.glob("*.json"), reverse=True)
    return files


def save_session(answers: dict) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(answers.get("name", "myapp"))
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = SESSION_DIR / f"{slug}_{ts}.json"
    path.write_text(json.dumps(answers, indent=2))
    return path


def load_session(path: Path) -> dict:
    return json.loads(path.read_text())


# ── wizard I/O ───────────────────────────────────────────────────
def say(msg: str) -> None:
    print(msg)


def ask(q, default: str) -> str:
    try:
        raw = input(f"  {C}{q}{X}\n  {D}[{default}]{X} > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return raw or default


def confirm(msg: str, default: bool = True) -> bool:
    dflt = "Y/n" if default else "y/N"
    while True:
        r = input(f"  {Y}{msg}{X} [{dflt}] > ").strip().lower()
        if not r:
            return default
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False


# ── template render ──────────────────────────────────────────────
def render_placeholder(text: str, answers: dict) -> str:
    """Replace {{KEY}} placeholders with the answer value (str)."""

    def sub(m):
        key = m.group(1).strip()
        val = answers.get(key, "")
        return str(val)

    return re.sub(r"\{\{\s*([A-Z_][A-Z0-9_]*)\s*\}\}", sub, text)


def build_app(answers: dict) -> Path:
    """Copy the Flask starter template into a build dir, fill placeholders,
    return the path to the populated build dir."""
    slug = slugify(answers.get("name", "myapp"))
    build = Path("/tmp") / f"appforge_{slug}_{datetime.now():%H%M%S}"
    if build.exists():
        shutil.rmtree(build)
    shutil.copytree(TEMPLATE_DIR, build)
    # Map {{KEY}} placeholders → UPPER(key) for readability
    ph = {k.upper(): v for k, v in answers.items()}
    ph["SLUG"] = slug
    ph["GENERATED_AT"] = datetime.now().isoformat(timespec="seconds")
    # Feature list expanded for the UI cards
    feat_list = [f.strip() for f in answers.get("features", "").split(",") if f.strip()]
    ph["FEATURES_JSON"] = json.dumps(feat_list)
    ph["FEATURES_HTML"] = "".join(
        f'<div class="card"><h3>{f}</h3><p>Stub — wire me up.</p></div>'
        for f in feat_list
    )
    ph["AI_SNIPPET"] = (
        "\n@app.post('/ask')\ndef ask():\n    "
        "import urllib.request, json\n    "
        "q = (request.get_json() or {}).get('q', '')\n    "
        "r = urllib.request.urlopen('http://localhost:11434/api/generate',\n"
        "        data=json.dumps({'model': 'master-ai:latest', 'prompt': q,"
        " 'stream': False}).encode(), timeout=60)\n    "
        "return {'answer': json.loads(r.read()).get('response','')}\n"
        if answers.get("needs_ai", "n").lower().startswith("y")
        else ""
    )
    for p in build.rglob("*"):
        if p.is_file():
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            new = render_placeholder(text, ph)
            if new != text:
                p.write_text(new)
    return build


def zip_build(build: Path, slug: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{slug}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in build.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(build.parent))
    return out


def write_next_steps(answers: dict, zip_path: Path) -> Path:
    slug = slugify(answers.get("name", "myapp"))
    out = OUT_DIR / f"{slug}_NEXT_STEPS.md"
    tmpl = f"""# {answers.get("name")} — Next Steps

**Zip:** `{zip_path}`
**Tagline:** {answers.get("tagline")}
**Audience:** {answers.get("audience")}
**Platform:** {answers.get("platform")}
**Generated:** {datetime.now().isoformat(timespec="seconds")}

---

## 1. Run it locally

```bash
unzip {zip_path.name}
cd {slug}
pip install -r requirements.txt
python app.py
# → open http://localhost:5000
```

## 2. Sell it on Gumroad

1. Create a Gumroad account: https://gumroad.com/signup
2. New Product → **Digital Product**
3. Upload the zip (`{zip_path.name}`)
4. Price: **${answers.get("price")}**  ·  Title: **{answers.get("name")}**
5. Description: use the tagline + "what it does" + setup steps above
6. Publish → share the product URL

## 3. App Store / Play Store (if you later wrap it)

- **Android** — Capacitor wrap → `.aab` → Google Play Console ($25 one-time)
- **iOS** — Capacitor wrap → Xcode → App Store Connect ($99/yr)
- Capacitor guide: https://capacitorjs.com/docs

## 4. Sole-proprietor quick checklist

- EIN (free, IRS.gov) if selling with a business name
- Business bank account (Novo / Relay / Mercury — free)
- Invoice + receipts folder (Google Drive is fine at this scale)
- 1099-K threshold: Gumroad reports if you cross it

---

Creator: {answers.get("creator")}
"""
    out.write_text(tmpl)
    return out


# ── main wizard ──────────────────────────────────────────────────
def banner():
    print(f"\n{BC}  ╔══════════════════════════════════════════╗{X}")
    print(f"{BC}  ║  🛠   APP  FORGE  —  answer → shippable  ║{X}")
    print(f"{BC}  ╚══════════════════════════════════════════╝{X}\n")


def resume_prompt():
    sessions = list_sessions()
    if not sessions:
        return None
    say(f"  {D}Recent sessions you can resume:{X}")
    for i, s in enumerate(sessions[:5], 1):
        say(f"    {Y}{i}{X}) {s.name}")
    r = input(
        f"\n  Resume one? (1-{min(5, len(sessions))}, or Enter to start fresh) > "
    ).strip()
    if r.isdigit() and 1 <= int(r) <= min(5, len(sessions)):
        return load_session(sessions[int(r) - 1])
    return None


def run():
    banner()
    answers = resume_prompt() or {}
    for key, prompt, dflt, help_line in QUESTIONS:
        current_default = answers.get(key, dflt)
        say(f"\n  {D}{help_line}{X}")
        answers[key] = ask(prompt, current_default)
        save_session(answers)  # save after each answer so phone→desktop is safe

    say(f"\n{G}  ✅ Answers captured. Generating app...{X}\n")
    build = build_app(answers)
    slug = slugify(answers.get("name", "myapp"))
    zip_path = zip_build(build, slug)
    steps = write_next_steps(answers, zip_path)

    say(f"  {G}📦 App zip:        {X}{zip_path}")
    say(f"  {G}📋 Next steps:     {X}{steps}")
    say(f"  {G}💾 Session saved:  {X}{SESSION_DIR}/{slug}_*.json")
    say(f"\n{BC}  Forge complete. Happy shipping.{X}\n")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print(f"\n{Y}  Aborted. Partial answers saved in {SESSION_DIR}/{X}")
        sys.exit(130)
