"""Portable SKILL.md tutor runtime for AI Engineering from Scratch.

This module lets Sensei run the curriculum's prose-based skills
(start-learning, learn, course-guide, check-understanding) without
hand-adapting each one into skill_runtime.py's typed STEPS state machine.
It treats the SKILL.md as the spec, reads lesson files from the local clone,
and updates LEARNING.md.

2026-09-28 — built for Elijah's voice+IDE learning workflow.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

# Try to import skill_marketplace for source discovery.
sys.path.insert(0, str(Path(__file__).parent))
try:
    import skill_marketplace as sm
except Exception:
    sm = None  # type: ignore

# ─── Paths ──────────────────────────────────────────────────────────

AIES_REPO = Path("/home/elijah/projects/ai-engineering-from-scratch")
OPENCODE_SKILLS = Path.home() / ".config" / "opencode" / "skills"
DEFAULT_LEARNING_FILE = Path("LEARNING.md")
ROADMAP_URL = "https://raw.githubusercontent.com/rohitg00/ai-engineering-from-scratch/main/ROADMAP.md"
README_URL = "https://raw.githubusercontent.com/rohitg00/ai-engineering-from-scratch/main/README.md"
RAW_BASE = "https://raw.githubusercontent.com/rohitg00/ai-engineering-from-scratch/main"

# ─── Helpers ────────────────────────────────────────────────────────


def _find_skill_dir(name: str) -> Path | None:
    """Locate a portable SKILL.md by name under the OpenCode skills tree."""
    if not OPENCODE_SKILLS.is_dir():
        return None
    for skill_md in OPENCODE_SKILLS.rglob("SKILL.md"):
        text = skill_md.read_text(errors="replace")
        fm = sm._parse_frontmatter(text) if sm else _parse_frontmatter(text)
        if fm.get("name") == name:
            return skill_md.parent
    return None


def _parse_frontmatter(text: str) -> dict:
    """Minimal frontmatter parser fallback."""
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _repo_exists() -> bool:
    return (AIES_REPO / "phases").is_dir()


def _read_local(path: str) -> str:
    full = AIES_REPO / path
    if full.exists():
        return full.read_text(errors="replace")
    return ""


def _read_url(url: str) -> str:
    import urllib.parse
    import urllib.request

    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _read(path: str, fallback_url: str | None = None) -> str:
    if _repo_exists():
        text = _read_local(path)
        if text:
            return text
    if fallback_url:
        return _read_url(fallback_url)
    return ""


def _phase_dirs() -> list[tuple[int, str, str]]:
    """Return sorted list of (phase_num, phase_dir_name, title_guess).

    phase_dir_name is the full directory name like '01-math-foundations'.
    """
    if not _repo_exists():
        return []
    out = []
    for d in sorted((AIES_REPO / "phases").iterdir()):
        if not d.is_dir():
            continue
        m = re.match(r"^(\d+)-(.+)$", d.name)
        if not m:
            continue
        num = int(m.group(1))
        title = m.group(2).replace("-", " ").title()
        out.append((num, d.name, title))
    return sorted(out)


def _lessons_for_phase(phase_dir_name: str) -> list[tuple[int, str, str, str]]:
    """Return sorted (lesson_num, lesson_dir, lesson_slug, title) for a phase.

    phase_dir_name is the full directory name like '01-math-foundations'.
    lesson_dir is the full directory name like '01-dev-environment'.
    lesson_slug is the suffix like 'dev-environment'.
    """
    phase_dir = AIES_REPO / "phases" / phase_dir_name
    if not phase_dir.is_dir():
        return []
    out = []
    for d in sorted(phase_dir.iterdir()):
        if not d.is_dir():
            continue
        m = re.match(r"^(\d+)-(.+)$", d.name)
        if not m:
            continue
        num = int(m.group(1))
        lesson_slug = m.group(2)
        title = lesson_slug.replace("-", " ").title()
        # Prefer README title if present.
        readme = d / "README.md"
        if readme.exists():
            first = readme.read_text(errors="replace").splitlines()[0]
            if first.startswith("#"):
                title = first.lstrip("#").strip()
        out.append((num, d.name, lesson_slug, title))
    return sorted(out)


def _read_lesson_doc(phase_dir_name: str, lesson_dir: str) -> str:
    path = f"phases/{phase_dir_name}/{lesson_dir}/docs/en.md"
    url = f"{RAW_BASE}/{path}"
    return _read(path, url)


def _read_quiz(phase_dir_name: str, lesson_dir: str) -> dict:
    path = f"phases/{phase_dir_name}/{lesson_dir}/quiz.json"
    url = f"{RAW_BASE}/{path}"
    text = _read(path, url)
    try:
        return json.loads(text) if text else {}
    except Exception:
        return {}


# ─── LEARNING.md operations ─────────────────────────────────────────


def _learning_exists(path: Path | None = None) -> bool:
    p = path or DEFAULT_LEARNING_FILE
    return p.exists()


def _parse_learning(path: Path | None = None) -> dict:
    """Best-effort parse of LEARNING.md into a dict."""
    p = path or DEFAULT_LEARNING_FILE
    if not p.exists():
        return {}
    text = p.read_text(errors="replace")
    return {
        "exists": True,
        "text": text,
        "entry_phase": _extract_entry_phase(text),
        "pace": _extract_pace(text),
    }


def _extract_entry_phase(text: str) -> int | None:
    m = re.search(r"Entry point:\s*Phase\s*(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _extract_pace(text: str) -> str:
    m = re.search(r"Pace:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _find_next_lesson(path: Path | None = None) -> tuple[str, str, str] | None:
    """Find the next unlogged lesson from LEARNING.md. Returns (phase, lesson_dir, title)."""
    data = _parse_learning(path)
    if not data.get("exists"):
        # Default to first lesson.
        lessons = _lessons_for_phase("00-setup-and-tooling")
        if lessons:
            return ("00-setup-and-tooling", lessons[0][1], lessons[0][3])
        return None

    entry_phase = data.get("entry_phase")
    text = data["text"]

    # Parse progress log rows: Date | Lesson | Quiz | Note
    done = set()
    for line in text.splitlines():
        m = re.match(r"^\|\s*[^|]+\|\s*([^/]+)/([^\s|]+)\s*\|", line)
        if m:
            done.add((m.group(1).strip(), m.group(2).strip()))

    phases = _phase_dirs()
    for num, phase_slug, _ in phases:
        if entry_phase is not None and num < entry_phase:
            continue
        # Skip if phase status is "Skip" in the Path table.
        if f"| {num:02d} " in text or f"| {num} " in text:
            if re.search(rf"\|\s*{num:02d}\s*\|[^|]+\|\s*Skip\s*\|", text):
                continue
        lessons = _lessons_for_phase(phase_slug)
        for _, lesson_dir, lesson_slug, title in lessons:
            if (phase_slug, lesson_slug) not in done and (
                phase_slug,
                lesson_dir,
            ) not in done:
                return (phase_slug, lesson_dir, title)
    return None


def _append_progress(
    phase: str,
    lesson_dir: str,
    score: str,
    note: str = "",
    path: Path | None = None,
) -> None:
    p = path or DEFAULT_LEARNING_FILE
    text = p.read_text(errors="replace") if p.exists() else ""
    row = f"| {time.strftime('%Y-%m-%d')} | {phase}/{lesson_dir} | {score} | {note} |"
    if "## Progress log" in text:
        # Insert after the header row.
        lines = text.splitlines()
        idx = next((i for i, l in enumerate(lines) if "## Progress log" in l), -1)
        if idx >= 0 and idx + 2 < len(lines) and "|" in lines[idx + 2]:
            lines.insert(idx + 3, row)
            text = "\n".join(lines)
        else:
            text += f"\n{row}\n"
    else:
        text += f"\n\n## Progress log\n\n| Date | Lesson | Quiz | Note |\n|------|--------|------|------|\n{row}\n"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(p)


# ─── TTS integration ────────────────────────────────────────────────


def speak(text: str) -> None:
    """Send text to the existing Sensei / AI Controller TTS stack."""
    # Prefer the local voice_bridge /speak endpoint if available.
    bridge = os.environ.get("VOICE_BRIDGE_URL", "http://127.0.0.1:8002")
    try:
        import urllib.request

        data = urllib.parse.urlencode({"text": text[:500]}).encode()
        req = urllib.request.Request(f"{bridge}/speak", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            _ = r.read()
        return
    except Exception:
        pass
    # Fallback: call master-ai TTS server if present.
    tts_server = os.environ.get("TTS_SERVER_URL", "http://127.0.0.1:8792")
    try:
        import urllib.request

        data = json.dumps({"text": text[:500]}).encode()
        req = urllib.request.Request(
            f"{tts_server}/speak",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            _ = r.read()
        return
    except Exception:
        pass


def split_for_tts(text: str, max_chars: int = 400) -> list[str]:
    """Break markdown into sentence-ish chunks for voice reading."""
    # Strip markdown links, code fences, heavy formatting.
    cleaned = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", text)
    cleaned = re.sub(r"```[\s\S]*?```", " (code block) ", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"#+\s*", "", cleaned)
    cleaned = re.sub(r"\*\*|__|\*|_|\|", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    chunks = []
    while len(cleaned) > max_chars:
        cut = cleaned.rfind(". ", 0, max_chars)
        if cut < max_chars // 2:
            cut = cleaned.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(cleaned[:cut].strip())
        cleaned = cleaned[cut:].strip()
    if cleaned:
        chunks.append(cleaned)
    return chunks


# ─── Tutor actions ────────────────────────────────────────────────────


def start_learning(
    mission: str = "",
    hours_per_week: str = "",
    build_goal: str = "",
    skip_interview: bool = False,
) -> dict[str, Any]:
    """Run the start-learning skill: create LEARNING.md."""
    if DEFAULT_LEARNING_FILE.exists() and not skip_interview:
        return {
            "status": "exists",
            "message": f"{DEFAULT_LEARNING_FILE} already exists. Options: resume (tutor next), rerun placement, or start-over.",
        }

    phases = _phase_dirs()
    path_table = [
        "| Phase | Name | Status | Est. hours |",
        "|-------|------|--------|------------|",
    ]
    # Hours are unknown without ROADMAP; leave blank or default to phase count * 5.
    for num, slug, title in phases:
        status = "Do" if num >= 1 else "Skip"  # default: skip phase 0 setup
        hours = "~17"
        path_table.append(f"| {num:02d} | {title} | {status} | {hours} |")

    entry_phase = 1
    pace = hours_per_week or "~5 h"
    mission_line = (
        mission or "Learn AI engineering from first principles and build real systems."
    )
    if build_goal:
        mission_line += f" Goal: {build_goal}."

    text = f"""# My AI Engineering Path
<!-- Managed by the ai-engineering-from-scratch learning skills.
     Repo: https://github.com/rohitg00/ai-engineering-from-scratch -->

## Mission
{mission_line}

## Placement
- Date: {time.strftime("%Y-%m-%d")}
- Score: setup (no quiz)
- Entry point: Phase {entry_phase}: Math Foundations
- Pace: {pace}/week

## Path
{"\n".join(path_table)}

## Progress log
| Date | Lesson | Quiz | Note |
|------|--------|------|------|

## Review queue

"""
    tmp = DEFAULT_LEARNING_FILE.with_suffix(".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(DEFAULT_LEARNING_FILE)
    return {
        "status": "created",
        "message": f"Created {DEFAULT_LEARNING_FILE}. Entry: Phase {entry_phase}. Next: tutor next",
    }


def next_lesson() -> dict[str, Any]:
    """Run the learn skill: teach the next lesson."""
    loc = _find_next_lesson()
    if not loc:
        return {
            "status": "complete",
            "message": "All planned lessons are logged. Use course-guide to explore topics or check-understanding for quizzes.",
        }
    phase, lesson_dir, title = loc
    doc = _read_lesson_doc(phase, lesson_dir)
    quiz = _read_quiz(phase, lesson_dir)
    return {
        "status": "lesson",
        "phase": phase,
        "lesson": lesson_dir,
        "title": title,
        "doc": doc,
        "quiz": quiz,
        "tts_chunks": split_for_tts(doc),
    }


def record_lesson(
    phase: str, lesson: str, score: str, note: str = ""
) -> dict[str, Any]:
    _append_progress(phase, lesson_dir, score, note)
    return {"status": "recorded", "phase": phase, "lesson": lesson_dir, "score": score}


def current_lesson() -> dict[str, Any] | None:
    """Return the next uncompleted lesson without advancing progress."""
    loc = _find_next_lesson()
    if not loc:
        return None
    phase, lesson_dir, title = loc
    doc = _read_lesson_doc(phase, lesson_dir)
    quiz = _read_quiz(phase, lesson_dir)
    return {
        "status": "lesson",
        "phase": phase,
        "lesson": lesson_dir,
        "title": title,
        "doc": doc,
        "quiz": quiz,
        "tts_chunks": split_for_tts(doc),
    }


def read_aloud(
    lesson_result: dict[str, Any] | None = None, chunk_index: int = 0
) -> dict[str, Any]:
    """Speak a lesson's TTS chunks starting from chunk_index.

    If no lesson_result is provided, speaks the current next lesson.
    Returns a status dict the CLI/TUI can act on.
    """
    if lesson_result is None:
        lesson_result = current_lesson()
    if not lesson_result or lesson_result.get("status") != "lesson":
        return {
            "status": "no_lesson",
            "message": "No next lesson found. Create LEARNING.md or run tutor start.",
        }

    chunks = lesson_result.get("tts_chunks", [])
    if not chunks:
        return {"status": "no_chunks", "message": "Lesson has no readable content."}

    if chunk_index < 0 or chunk_index >= len(chunks):
        return {
            "status": "bad_index",
            "message": f"Chunk {chunk_index} out of range (0-{len(chunks) - 1}).",
        }

    chunk_text = chunks[chunk_index]
    speak(chunk_text)
    return {
        "status": "spoken",
        "phase": lesson_result["phase"],
        "lesson": lesson_result["lesson"],
        "title": lesson_result["title"],
        "chunk_index": chunk_index,
        "total_chunks": len(chunks),
        "chunk_text": chunk_text,
    }


def course_guide(topic: str) -> dict[str, Any]:
    """Find the lesson whose title/slug best matches the topic."""
    topic_low = topic.lower().replace(" ", "-")
    best = None
    best_score = 0
    for num, phase_slug, _ in _phase_dirs():
        for lnum, lesson_dir, lesson_slug, title in _lessons_for_phase(phase_slug):
            candidates = [lesson_slug, title.lower().replace(" ", "-")]
            score = max(
                sum(1 for w in topic_low.split("-") if w in c) for c in candidates
            )
            if score > best_score:
                best_score = score
                best = (phase_slug, lesson_dir, title)
    if best:
        phase, lesson, title = best
        return {
            "status": "match",
            "phase": phase,
            "lesson": lesson_dir,
            "title": title,
            "doc": _read_lesson_doc(phase, lesson),
            "quiz": _read_quiz(phase, lesson),
            "tts_chunks": split_for_tts(_read_lesson_doc(phase, lesson)),
        }
    return {"status": "no_match", "topic": topic}


def check_understanding(phase_arg: str | int) -> dict[str, Any]:
    """Collect post-stage quiz questions from all lessons in a phase."""
    phase_slug = None
    if isinstance(phase_arg, int) or phase_arg.isdigit():
        num = int(phase_arg)
        for n, slug, _ in _phase_dirs():
            if n == num:
                phase_slug = slug
                break
    else:
        phase_slug = phase_arg
    if not phase_slug:
        return {"status": "error", "message": f"Could not resolve phase: {phase_arg!r}"}

    questions = []
    for _, lesson_dir, _, _ in _lessons_for_phase(phase_slug):
        quiz = _read_quiz(phase_slug, lesson_dir)
        for q in quiz.get("questions", []):
            if q.get("stage") == "post":
                questions.append({"lesson": lesson_dir, **q})
    random.shuffle(questions)
    return {
        "status": "quiz",
        "phase": phase_slug,
        "count": len(questions),
        "questions": questions[:20],
    }


# ─── CLI ────────────────────────────────────────────────────────────


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="AI Engineering from Scratch tutor helper"
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("start", help="Create LEARNING.md")
    sub.add_parser("next", help="Get the next lesson")
    p_guide = sub.add_parser("guide", help="Find a lesson by topic")
    p_guide.add_argument("topic", nargs="+")
    p_quiz = sub.add_parser("quiz", help="Build a phase quiz")
    p_quiz.add_argument("phase")
    p_speak = sub.add_parser("speak", help="Speak a text string via TTS")
    p_speak.add_argument("text")
    p_read = sub.add_parser("read", help="Read the current/next lesson aloud")
    p_read.add_argument(
        "--chunk", type=int, default=0, help="Chunk index to start from"
    )
    p_read.add_argument(
        "--all", action="store_true", help="Read all chunks sequentially"
    )

    args = parser.parse_args()
    if args.cmd == "start":
        print(json.dumps(start_learning(), indent=2))
    elif args.cmd == "next":
        print(json.dumps(next_lesson(), indent=2))
    elif args.cmd == "guide":
        print(json.dumps(course_guide(" ".join(args.topic)), indent=2))
    elif args.cmd == "quiz":
        print(json.dumps(check_understanding(args.phase), indent=2))
    elif args.cmd == "speak":
        speak(args.text)
        print(json.dumps({"spoken": True}))
    elif args.cmd == "read":
        if args.all:
            res = current_lesson()
            chunks = res.get("tts_chunks", []) if res else []
            for i, _ in enumerate(chunks):
                r = read_aloud(res, chunk_index=i)
                print(json.dumps(r, indent=2))
        else:
            print(json.dumps(read_aloud(chunk_index=args.chunk), indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
