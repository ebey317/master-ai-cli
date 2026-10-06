#!/usr/bin/env python3
"""job_application_driver — apply to Indeed jobs end-to-end with subagents.

This script is built so a small model only has to run one Bash command.
It handles: search → qualify → apply → report. Email verification and logging
are left to the small model because they use the assistant's email MCP tools.

Usage:
    # dry-run: find and qualify postings, do not apply
    python3 ~/scripts/job_application_driver.py --query "HVAC Installer" --count 5

    # actually apply
    python3 ~/scripts/job_application_driver.py --query "HVAC Installer" --count 3 --apply

    # apply and automatically log each verified confirmation (email check still
    # uses assistant tools, so --auto-log only works when run inside an assistant
    # session that exposes mcp__email-bridge tools; otherwise it skips logging)
    python3 ~/scripts/job_application_driver.py --query "HVAC Installer" --count 3 --apply --auto-log

Output: JSON summary printed to stdout. Example:
    {
      "query": "HVAC Installer",
      "location": "Indianapolis, IN",
      "requested": 3,
      "processed": 5,
      "applied": 2,
      "jobs": [
        {"company": "...", "title": "...", "qualified": true, "applied": true,
         "apply_url": "...", "expected_subject": "Indeed Application: ...",
         "status": "APPLIED_AWAITING_EMAIL"}
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.request
from datetime import date
from typing import Any

HOME = os.path.expanduser("~")

# IMPORTANT: load projects/master-ai sensei_mcp_server BEFORE adding ~/scripts,
# because an older shadow copy lives at ~/scripts/sensei_mcp_server.py.
sys.path.insert(0, os.path.join(HOME, "projects", "claf", "tools"))
sys.path.insert(0, os.path.join(HOME, "projects", "master-ai"))
import sensei_mcp_server as sensei  # noqa: E402

sys.path.insert(0, os.path.join(HOME, "projects", "master-ai"))
import subagent_registry as reg  # noqa: E402

# Email server is optional; if not importable, the driver just skips verification.
try:
    sys.path.insert(0, os.path.join(HOME, "scripts", "email_mcp"))
    import server as email_server  # noqa: E402
except Exception:
    email_server = None


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------


def _tool_text(resp: dict) -> str:
    try:
        return resp["content"][0]["text"]
    except Exception:
        return str(resp)


def _extract_json(text: str) -> Any:
    """Grab the first JSON object/array from a string."""
    text = text or ""
    for start_char in ("{", "["):
        idx = text.find(start_char)
        if idx != -1:
            try:
                return json.loads(text[idx:])
            except Exception:
                continue
    return None


def _unwrap_result(text: str) -> dict:
    """Return the inner 'result' dict from a Sensei tool response text."""
    data = _extract_json(text)
    if isinstance(data, dict):
        if "result" in data:
            return data["result"]
        return data
    return {"ok": False, "raw": text}


def _call(name: str, **kwargs) -> dict:
    fn = getattr(sensei, f"tool_{name}", None)
    if not fn:
        return {"ok": False, "error": f"no tool {name}"}
    return _unwrap_result(_tool_text(fn(kwargs)))


def _activate_sensei_session() -> str:
    """Route actions to the active Sensei side-panel session.

    The bridge may have several sessions; only the active side-panel session
    is polled by the extension. The MCP default is "mcp-default", which can
    silently time out if the panel is using a different session id.
    """
    active = "mcp-default"
    try:
        res = sensei._http("GET", "/extension/sessions", timeout=3.0)
        active = res.get("json", {}).get("active_side_panel_session") or active
    except Exception:
        pass

    if not getattr(sensei, "_DRIVER_SESSION_PATCHED", False):
        _orig_dispatch = sensei._dispatch

        def _dispatch_dynamic(kind, payload, session=None, wait=30):
            if session is None:
                session = getattr(sensei, "DEFAULT_SESSION", active)
            return _orig_dispatch(kind, payload, session=session, wait=wait)

        sensei._dispatch = _dispatch_dynamic
        sensei._DRIVER_SESSION_PATCHED = True

    sensei.DEFAULT_SESSION = active

    # Clear any stale actions left over from a previous crashed run.
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:8080/extension/queue?session_id={active}",
            timeout=3.0,
        ).read()
    except Exception:
        pass

    return active


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


def _tab_list() -> list[dict]:
    """Return session_tabs from the bridge (falls back to all_tabs).

    Uses the raw bridge dispatch so we don't get truncated by the MCP tool's
    3000-char text formatter.
    """
    res = sensei._dispatch("BROWSER_TAB_LIST", {})
    result = res.get("result", {})
    tabs = result.get("session_tabs") or result.get("all_tabs", [])
    return tabs if isinstance(tabs, list) else []


def _active_tab_id() -> str | None:
    for tab in _tab_list():
        if tab.get("active"):
            return str(tab["id"])
    return None


def _active_tab_url() -> str:
    for tab in _tab_list():
        if tab.get("active"):
            return tab.get("url", "")
    return ""


def _find_tab_by_url(url_substr: str) -> str | None:
    for tab in _tab_list():
        if url_substr in tab.get("url", ""):
            return str(tab["id"])
    return None


def _newest_tab_id() -> str | None:
    """Return the highest numeric tab id — useful after tab_create."""
    best = None
    for tab in _tab_list():
        try:
            tid = int(tab["id"])
        except Exception:
            continue
        if best is None or tid > best:
            best = tid
    return str(best) if best is not None else None


def _switch(tab_id: str) -> dict:
    return _call("tab_switch", tab_id=tab_id)


def _close(tab_id: str) -> dict:
    return _call("tab_close", tab_id=tab_id)


def _create_tab(url: str) -> str:
    # Use raw dispatch so we get the full tab_created id before truncation.
    res = sensei._dispatch("BROWSER_TAB_CREATE", {"target": url})
    result = res.get("result", {})
    tc = result.get("tab_created", {})
    if tc and tc.get("id"):
        tid = str(tc["id"])
        _switch(tid)
        return tid
    # Fallback: find by URL or newest id.
    for _ in range(5):
        tid = _find_tab_by_url(url)
        if tid:
            _switch(tid)
            return tid
        tid = _newest_tab_id()
        if tid:
            _switch(tid)
            return tid
        _wait(500)
    return ""


def _trim_tabs(max_tabs: int = 3, keep: set | None = None):
    """Close oldest tabs until we are under the hard 4-tab maximum.

    If `keep` is provided, those tab ids are always preserved. Otherwise the
    active tab and the newest tabs are kept.
    """
    res = sensei._dispatch("BROWSER_TAB_LIST", {})
    tabs = res.get("result", {}).get("all_tabs", [])
    if len(tabs) <= max_tabs:
        return
    sorted_tabs = sorted(tabs, key=lambda x: x["id"])
    preserved: set = set(keep) if keep else set()
    if not preserved:
        active_id = None
        for t in tabs:
            if t.get("active"):
                active_id = t["id"]
                break
        if active_id is not None:
            preserved.add(active_id)
        for t in reversed(sorted_tabs):
            if len(preserved) >= max_tabs:
                break
            preserved.add(t["id"])
    for t in sorted_tabs:
        if t["id"] not in preserved:
            try:
                sensei._dispatch("BROWSER_TAB_CLOSE", {"target": str(t["id"])})
            except Exception:
                pass


def _wait(ms: int = 2000) -> dict:
    return _call("wait", ms=ms)


def _browse(url: str) -> dict:
    return _call("browse", url=url)


def _js_query(selector: str, attribute: str = "textContent", limit: int = 30) -> list:
    res = _call(
        "execute_js",
        command="query",
        selector=selector,
        attribute=attribute,
        limit=limit,
        all_frames=True,
    )
    # results is a list per frame; take the first frame with results.
    frames = res.get("results", []) if isinstance(res, dict) else []
    for frame in frames:
        if frame.get("count", 0) > 0:
            return frame.get("results", [])
    return []


def _page_text(selector: str = "body") -> str:
    items = _js_query(selector, "textContent", 1)
    return items[0].get("text", "") if items else ""


def _query_buttons(limit: int = 50) -> list[dict]:
    """Return visible action buttons on the active page, ignoring global nav."""
    # Smartapply puts most action text in <button>; some flows use links styled as buttons.
    ignore = {
        "skip to main content",
        "find jobs",
        "company reviews",
        "find salaries",
        "my jobs",
        "messages",
        "notifications",
        "account",
        "employers / post job",
        "profile",
        "my reviews",
        "home",
        "exit",
        "report an issue",
        "report job",
        "save-icon",
        "preview previous page",
        "preview next page",
        "show morechevron down",
        "1 new update",
        "unread count",
    }
    collected: list[dict] = []
    seen: set[str] = set()
    for selector in ("button", "[data-testid*='submit']", "[data-testid*='continue']"):
        # Smartapply uses iframes for recaptcha/analytics; only the main frame holds the form buttons.
        res = _call(
            "execute_js",
            command="query",
            selector=selector,
            attribute="textContent",
            limit=limit,
            all_frames=False,
        )
        frames = res.get("results", []) if isinstance(res, dict) else []
        for frame in frames:
            for item in frame.get("results", []):
                text = (item.get("text") or "").strip()
                if not text:
                    continue
                norm = text.lower()
                # Skip empty, single chars, and nav items.
                if len(norm) <= 1 or norm in ignore or norm in seen:
                    continue
                seen.add(norm)
                collected.append(
                    {"text": text, "selector": selector, "tag": item.get("tag", "")}
                )
    return collected


def _find_button_text(buttons: list[dict], *keywords: str) -> str | None:
    """Return the highest-priority button text containing any of the keywords (case-insensitive).

    Keywords are checked in order, and within each keyword the shortest matching
    button is preferred (avoids long aria labels). Submit/Schedule are explicitly
    prioritized over Continue/Next.
    """
    for kw in keywords:
        kw_lower = kw.lower()
        matches = [btn for btn in buttons if kw_lower in btn["text"].lower()]
        if matches:
            return min(matches, key=lambda b: len(b["text"]))["text"]
    return None


def _fill_profile_fields(profile: dict) -> None:
    """Fill common Smart Apply fields directly, avoiding the slow fill_form read_full path."""
    name_parts = (profile.get("full_name") or "").split(None, 1)
    first = name_parts[0] if name_parts else ""
    last = name_parts[1] if len(name_parts) > 1 else ""

    mapping: list[tuple[str, str]] = [
        ("input[name='firstName']", first),
        ("input[name='lastName']", last),
        ("input[name='email']", profile.get("email", "")),
        ("input[type='email']", profile.get("email", "")),
        ("input[name='phone']", profile.get("phone", "")),
        ("input[type='tel']", profile.get("phone", "")),
        ("input[name='city']", profile.get("city", "")),
        ("input[name='location']", profile.get("city", "")),
    ]
    for selector, value in mapping:
        if not value:
            continue
        # Only fill if the element exists.
        check = _call(
            "execute_js",
            command="query",
            selector=selector,
            attribute="textContent",
            limit=1,
            all_frames=False,
        )
        frames = check.get("results", []) if isinstance(check, dict) else []
        if any(frame.get("count", 0) > 0 for frame in frames):
            _call("execute_js", command="fill_selector", selector=selector, value=value)


def _description_html() -> str:
    # get_dom returns up to 50000 chars, enough for most descriptions.
    res = _call("get_dom", selector="#jobDescriptionText")
    return res.get("html", "")


# ---------------------------------------------------------------------------
# Indeed extraction
# ---------------------------------------------------------------------------


def _build_search_url(query: str, location: str) -> str:
    from urllib.parse import quote

    return f"https://www.indeed.com/jobs?q={quote(query)}&l={quote(location)}"


def _discover_jobs(search_tab_id: str, limit: int = 20) -> list[dict]:
    """Return job cards from the active Indeed search tab."""
    _switch(search_tab_id)
    _wait(4000)

    # Retry once if the page hasn't hydrated yet.
    for attempt in range(2):
        jks = _js_query("a[data-jk]", "data-jk", limit)
        titles = _js_query("a[data-jk]", "textContent", limit)
        if jks and titles:
            break
        _wait(2000)

    companies = _js_query('span[data-testid="company-name"]', "textContent", limit)
    locations = _js_query('div[data-testid="text-location"]', "textContent", limit)
    pays = _js_query(".salary-snippet-container", "textContent", limit)

    seen_jks = set()
    jobs = []
    n = min(len(jks), len(titles))
    for i in range(n):
        jk = (jks[i] or {}).get("data-jk", "")
        if not jk or jk in seen_jks:
            continue
        seen_jks.add(jk)
        loc = (locations[i] or {}).get("text", "") if i < len(locations) else ""
        # strip the "25 min·" prefix if present
        loc = re.sub(r"^\d+\s*min\s*[·•]\s*", "", loc)
        jobs.append(
            {
                "jk": jk,
                "title": (titles[i] or {}).get("text", "").strip(),
                "company": (companies[i] or {}).get("text", "").strip()
                if i < len(companies)
                else "",
                "location": loc.strip(),
                "pay": (pays[i] or {}).get("text", "").strip() if i < len(pays) else "",
                "apply_url": f"https://www.indeed.com/viewjob?jk={jk}",
            }
        )
    return jobs


# ---------------------------------------------------------------------------
# Subagent wrappers
# ---------------------------------------------------------------------------


def _load_profile() -> dict:
    res = reg.run("profile_fetcher")
    if not res.get("ok"):
        raise RuntimeError(f"profile_fetcher failed: {res}")
    return res["profile"]


def _make_form_profile(profile: dict) -> dict:
    """Convert profile dict into the flat key/value set smart_form_fill expects."""
    addr = profile.get("address", "")
    parts = [p.strip() for p in addr.split(",")]
    city, state, zipc = "Indianapolis", "IN", "46218"
    if len(parts) >= 2:
        city = parts[-2]
        m = re.search(r"([A-Za-z]{2})\s+(\d{5}(-\d{4})?)", parts[-1])
        if m:
            state, zipc = m.group(1), m.group(2)
    name = profile.get("legal_name", "")
    name_parts = name.split()
    return {
        "full_name": name,
        "first_name": name_parts[0] if name_parts else "",
        "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
        "email": profile.get("primary_email", ""),
        "phone": profile.get("phone", ""),
        "address": addr,
        "city": city,
        "state": state,
        "zip": zipc,
        "country": "United States",
        "authorized_us": True,
        "requires_sponsorship": False,
        "has_cdl": False,
        "willing_drug_test": True,
        "willing_background_check": True,
        "drivers_license_valid": profile.get("drivers_license_valid", True),
        "felony": False,
        "desired_salary": profile.get("pay_rules", {}).get("preferred_yr", 50000),
        "min_hourly": profile.get("pay_rules", {}).get("floor_hr_top", 21.63),
        "resume_path": profile.get("files", {}).get("resume", ""),
    }


def _qualify(job: dict, description_html: str, profile: dict) -> dict:
    task = json.dumps(
        {
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "pay": job["pay"],
            "description_html": description_html,
            "apply_url": job["apply_url"],
        }
    )
    return reg.run("posting_inspector", task=task, context={"profile": profile})


# ---------------------------------------------------------------------------
# Apply loop
# ---------------------------------------------------------------------------


def _open_apply_page(job: dict) -> dict:
    """Click the apply button on the detail page and return the Smart Apply URL.

    This is the stop-at-apply helper: it opens the application form but does not
    fill or submit it, giving the user a clean hand-off.
    """
    apply_selectors = [
        "#indeedApplyButton",
        "[data-testid='indeed-apply-button']",
        ".indeed-apply-button",
    ]
    for sel in apply_selectors:
        r = _call("execute_js", command="click_selector", selector=sel, all_frames=True)
        if r.get("ok"):
            break
    else:
        return {"opened": False, "reason": "apply button click failed on all selectors"}

    _wait(3000)
    tabs = _tab_list()
    # Prefer the active tab if it is a smartapply URL, otherwise the newest tab.
    active = next((t for t in tabs if t.get("active")), None)
    if active and "smartapply" in active.get("url", ""):
        return {
            "opened": True,
            "reason": "smart apply page opened",
            "final_url": active["url"],
        }
    newest = max(tabs, key=lambda t: int(t["id"]))
    return {
        "opened": True,
        "reason": "smart apply page opened",
        "final_url": newest.get("url", ""),
    }


def _apply_to_job(job: dict, form_profile: dict, max_steps: int = 10) -> dict:
    # Try several possible apply-button selectors on the detail page.
    apply_selectors = [
        "#indeedApplyButton",
        "[data-testid='indeed-apply-button']",
        ".indeed-apply-button",
    ]
    clicked = False
    for sel in apply_selectors:
        r = _call("execute_js", command="click_selector", selector=sel, all_frames=True)
        if r.get("ok"):
            clicked = True
            break
    if not clicked:
        return {
            "applied": False,
            "reason": "apply button click failed on all selectors",
        }

    # Wait for navigation to smartapply.
    _wait(3000)

    # Make sure we are on the application tab.
    app_tab = _active_tab_id()
    if not app_tab:
        return {"applied": False, "reason": "lost active tab after apply click"}

    # Resume upload if the page asks for it.
    file_inputs = _js_query('input[type="file"]', "outerHTML", 5)
    if file_inputs and form_profile.get("resume_path"):
        # Use the first file input; Indeed usually has a hidden one.
        _call(
            "upload_file",
            selector='input[type="file"]',
            path=form_profile["resume_path"],
        )
        _wait(1500)

    def _success_url() -> tuple[bool, str]:
        """Check all tabs for an Indeed application-success URL."""
        for t in _tab_list():
            url = (t.get("url") or "").lower()
            if any(
                x in url
                for x in [
                    "post-apply",
                    "/applied",
                    "application-submitted",
                    "apply/success",
                    "applied=1",
                    "applied=true",
                ]
            ):
                return True, t.get("url", "")
        return False, ""

    def _input_snapshot() -> dict[str, int]:
        """Return counts of text vs. choice inputs on the active page."""
        snapshot: dict[str, int] = {}
        groups = {
            "text": "input[type='text'], input[type='email'], input[type='tel'], input:not([type]), textarea",
            "choice": "select, input[type='radio'], input[type='checkbox']",
        }
        for kind, sel in groups.items():
            res = _call(
                "execute_js",
                command="query",
                selector=sel,
                attribute="textContent",
                limit=20,
                all_frames=False,
            )
            frames = res.get("results", []) if isinstance(res, dict) else []
            snapshot[kind] = sum(frame.get("count", 0) for frame in frames)
        return snapshot

    # Step through the application.
    for step in range(max_steps):
        _wait(1500)

        # Detect completion from any tab URL first.
        ok, success_url = _success_url()
        if ok:
            return {
                "applied": True,
                "reason": "success page reached",
                "final_url": success_url,
            }

        # Fill common text fields directly. Choice/screening questions are skipped;
        # fill_form is too slow (2×20s read_full per page) and currently fails with 403.
        inputs = _input_snapshot()
        if inputs.get("text", 0) > 0:
            _fill_profile_fields(form_profile)
        _wait(800)

        # Re-check success after fill (some pages auto-advance).
        ok, success_url = _success_url()
        if ok:
            return {
                "applied": True,
                "reason": "success page reached",
                "final_url": success_url,
            }

        # Determine the next button to click from actual button text.
        buttons = _query_buttons(limit=30)
        btn_text = _find_button_text(
            buttons,
            "submit your application",
            "submit application",
            "schedule an interview",
            "continue",
            "next",
            "save",
            "review",
        )

        # If no action button is found, check whether we landed on a confirmation/success page.
        if not btn_text:
            page_text = _page_text("body").lower()
            if any(
                x in page_text
                for x in [
                    "submitted",
                    "application submitted",
                    "you applied",
                    "success",
                    "your application has been submitted",
                ]
            ):
                return {
                    "applied": True,
                    "reason": "submitted detected in page text",
                    "final_url": _active_tab_url(),
                }
            ok, success_url = _success_url()
            if ok:
                return {
                    "applied": True,
                    "reason": "success url detected",
                    "final_url": success_url,
                }
            # Capture current active tab URL for the failure reason.
            tabs = _tab_list()
            active_id = _active_tab_id()
            final_url = ""
            for t in tabs:
                if str(t.get("id")) == active_id:
                    final_url = t.get("url", "")
                    break
            return {
                "applied": False,
                "reason": "no continue/submit button found",
                "final_url": final_url,
            }

        # Click the matched button by visible text.
        cr = _call("execute_js", command="click_text", text=btn_text, tag="button")
        if not cr.get("ok"):
            return {
                "applied": False,
                "reason": f"click '{btn_text}' failed: {cr}",
                "final_url": _active_tab_url(),
            }
        _wait(2000)

    return {"applied": False, "reason": f"exceeded {max_steps} apply steps"}


# ---------------------------------------------------------------------------
# Optional email verification / logging
# ---------------------------------------------------------------------------


def _find_confirmation_email(title: str, max_wait: int = 60) -> dict | None:
    """Poll AOL inbox for an Indeed confirmation matching the job title."""
    if not email_server:
        return None
    deadline = time.time() + max_wait
    expected = f"Indeed Application: {title}"
    while time.time() < deadline:
        try:
            res = email_server.search_inbox(
                "aol", f'FROM indeedapply@indeed.com SUBJECT "{title}"', limit=5
            )
            # search_inbox returns a formatted string; look for a UID.
            for line in res.splitlines()[2:]:
                parts = line.split(None, 3)
                if len(parts) >= 4 and parts[0].isdigit():
                    return {
                        "account": "aol",
                        "sender": "indeedapply@indeed.com",
                        "subject": parts[3].strip(),
                        "uid": parts[0],
                    }
        except Exception:
            pass
        time.sleep(5)
    return None


def _log_application(job: dict, confirmation: dict, profile: dict) -> dict:
    task = json.dumps(
        {
            "date": date.today().isoformat(),
            "company": job["company"],
            "title": job["title"],
            "location": job["location"],
            "pay": job["pay"],
            "source": "Indeed",
            "description_summary": job.get("description_summary", ""),
            "contact": "Contact not provided in posting; follow via Indeed Apply.",
            "confirmation": confirmation,
            "apply_url": job["apply_url"],
            "status": "VERIFIED",
        }
    )
    return reg.run("application_logger", task=task, context={"profile": profile})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Indeed job-application driver")
    parser.add_argument("--query", required=True, help="Indeed search query")
    parser.add_argument("--location", default="Indianapolis, IN", help="Location")
    parser.add_argument("--count", type=int, default=3, help="Max jobs to process")
    parser.add_argument(
        "--apply", action="store_true", help="Actually apply (default is dry-run)"
    )
    parser.add_argument(
        "--stop-at-apply",
        action="store_true",
        help="Open the Smart Apply page for each qualified job, then stop for manual submit",
    )
    parser.add_argument(
        "--auto-log",
        action="store_true",
        help="Verify AOL email and log automatically (requires email_server import)",
    )
    parser.add_argument("--max-apply-steps", type=int, default=8)
    args = parser.parse_args()

    # Make sure actions go to the live Sensei side-panel session.
    active_session = _activate_sensei_session()
    print(f"[driver] active Sensei session: {active_session}", file=sys.stderr)

    profile = _load_profile()
    form_profile = _make_form_profile(profile)

    search_url = _build_search_url(args.query, args.location)
    print(f"[driver] opening search: {search_url}", file=sys.stderr)

    # Enforce the hard 4-tab maximum before opening anything new.
    _trim_tabs(max_tabs=3)

    # Open search results in a fresh tab so we can always return to it.
    search_tab_id = _create_tab(search_url)
    if not search_tab_id:
        print(json.dumps({"ok": False, "error": "could not create search tab"}))
        return

    jobs = _discover_jobs(search_tab_id, limit=args.count + 5)
    print(f"[driver] found {len(jobs)} job cards", file=sys.stderr)
    print(f"[driver] jks: {[j['jk'] for j in jobs]}", file=sys.stderr)

    results: list[dict] = []
    seen_jks: set[str] = set()
    processed = 0
    applied_count = 0

    for job in jobs:
        if processed >= args.count:
            break

        if job["jk"] in seen_jks:
            continue
        seen_jks.add(job["jk"])

        entry: dict = {
            "company": job["company"],
            "title": job["title"],
            "location": job["location"],
            "pay": job["pay"],
            "apply_url": job["apply_url"],
        }

        # Open the posting in a new tab and remember it.
        detail_tab_id = _create_tab(job["apply_url"])
        _wait(2500)

        try:
            description_html = _description_html()
            entry["description_summary"] = re.sub(
                r"\s+", " ", re.sub(r"<[^>]+>", " ", description_html)
            ).strip()[:300]

            q = _qualify(job, description_html, profile)
            entry["qualified"] = bool(q.get("qualified"))
            entry["qualify_reason"] = q.get("reason", "")

            if not entry["qualified"]:
                entry["applied"] = False
                entry["status"] = "SKIPPED"
                continue

            # Count only qualified jobs toward the requested total.
            processed += 1

            if not args.apply and not args.stop_at_apply:
                entry["applied"] = False
                entry["status"] = "QUALIFIED_DRY_RUN"
                continue

            # Stop-at-apply: open the Smart Apply page and hand off to the user.
            if args.stop_at_apply:
                open_result = _open_apply_page(job)
                entry["applied"] = open_result.get("opened", False)
                entry["apply_reason"] = open_result.get("reason", "")
                entry["final_url"] = open_result.get("final_url", "")
                entry["status"] = (
                    "APPLY_PAGE_OPENED" if entry["applied"] else "APPLY_OPEN_FAILED"
                )
                continue

            # Apply.
            apply_result = _apply_to_job(
                job, form_profile, max_steps=args.max_apply_steps
            )
            entry["applied"] = apply_result["applied"]
            entry["apply_reason"] = apply_result.get("reason", "")

            if apply_result["applied"]:
                applied_count += 1
                entry["status"] = "APPLIED_AWAITING_EMAIL"
                entry["expected_subject"] = f"Indeed Application: {job['title']}"

                if args.auto_log:
                    conf = _find_confirmation_email(job["title"], max_wait=60)
                    if conf:
                        entry["confirmation"] = conf
                        log_res = _log_application(job, conf, profile)
                        entry["logged"] = bool(log_res.get("ok"))
                        entry["status"] = (
                            "VERIFIED_LOGGED"
                            if entry["logged"]
                            else "APPLIED_LOG_FAILED"
                        )
                    else:
                        entry["status"] = "APPLIED_NO_CONFIRMATION_EMAIL"
            else:
                entry["status"] = "APPLY_FAILED"

        except Exception as e:
            entry["applied"] = False
            entry["status"] = "ERROR"
            entry["error"] = str(e)
            entry["trace"] = traceback.format_exc()
        finally:
            results.append(entry)
            # Aggressively close every tab except the search tab to prevent tab leaks.
            try:
                keep_ids = set()
                try:
                    keep_ids.add(int(search_tab_id))
                except Exception:
                    keep_ids.add(search_tab_id)
                _trim_tabs(max_tabs=1, keep=keep_ids)
            except Exception:
                pass
            _switch(search_tab_id)
            _wait(800)

    summary = {
        "ok": True,
        "query": args.query,
        "location": args.location,
        "requested": args.count,
        "processed": processed,
        "applied": applied_count,
        "jobs": results,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
