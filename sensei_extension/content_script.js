(() => {
  if (globalThis.__SENSEI_CONTENT_SCRIPT_LOADED__) return;
  globalThis.__SENSEI_CONTENT_SCRIPT_LOADED__ = true;

const ACTION_TARGETS =
  "button, a, input, textarea, select, [role='button'], [aria-label], [contenteditable='true']";

const DEFAULT_VISIBLE_TEXT_LIMIT = 1800;
const READ_TEXT_LIMIT = 5000;
const READ_PAGE_FULL_LIMIT = 500;
const FOCUSED_TEXT_LIMIT = 1200;
const INTERACTIVE_LIMIT = 80;
const CONSOLE_LIMIT = 40;
const PAGE_STABLE_DEBOUNCE_MS = 650;
const PAGE_STABLE_MAX_WAIT_MS = 3500;
const SKIP_TEXT_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS"]);
const SUBMIT_TEXT_RE = /^(submit|submit application|send application|apply now|finish|complete application)$/i;
const CONFIRM_URL_RE = /confirmation|submitted|thank-you|success|applied/i;
const CONFIRM_TEXT_RE = /application (has been )?submitted|thanks for applying|we('?ve| have) received your application|your application is complete/i;
const REFERENCE_BATTERY = [
  /reference\s*(number|id|#)[:\s]*([A-Z0-9-]{4,})/i,
  /confirmation\s*(number|id|#)[:\s]*([A-Z0-9-]{4,})/i,
  /application\s*(number|id|#)[:\s]*([A-Z0-9-]{4,})/i,
  /tracking\s*(number|id|#)[:\s]*([A-Z0-9-]{4,})/i,
];
const INDEED_REFERENCE_RE = /smartapply\.indeed\.com\/.+\/([a-f0-9]{16,})/i;
const DEFAULT_ROUTER_BASE = "http://127.0.0.1:8080";
const DISPATCH_PATH = "/dispatch";

// ─── Ref-ID grounding system — Pupil element map ─────────────────────────────
// Every call to interactiveElements() stamps visible DOM elements with a stable
// data-pupil-id attribute and records them in window.__pupilElementMap. Actions
// resolve refs through the map (O(1) lookup) before falling back to selector/
// text search. Counter and map survive script re-injection (|| guard).
globalThis.__pupilCounter = globalThis.__pupilCounter || 0;
globalThis.__pupilElementMap = globalThis.__pupilElementMap || {};

globalThis.__SENSEI_FIRST_SUBMIT_PAUSE_STATE__ = globalThis.__SENSEI_FIRST_SUBMIT_PAUSE_STATE__ || {
  first_app_pause_armed: true,
  pending_submit: null,
};
globalThis.__SENSEI_SIMPLIFY_DISMISS_STATE__ = globalThis.__SENSEI_SIMPLIFY_DISMISS_STATE__ || {
  page_key: "",
  attempts: 0,
};

try { console.log("injected"); } catch (_err) {}

// ─── Iframe relay — when this script runs inside a child frame, install a
// postMessage listener so the top-level content script (or side panel) can
// delegate actions into the frame by posting SENSEI_IFRAME_ACTION messages.
// The relay converts postMessage → chrome.runtime.sendMessage so the service
// worker can track frameId and route the result back to the caller.
if (typeof window !== "undefined" && window !== window.top) {
  window.addEventListener("message", (evt) => {
    if (!evt.data || evt.data.type !== "SENSEI_IFRAME_ACTION") return;
    // Only relay messages from the same extension (top frame posts them with
    // a shared secret or we rely on the service worker's sender.frameId check).
    try {
      chrome.runtime.sendMessage(
        { type: "SENSEI_IFRAME_ACTION", action: evt.data.action || {} },
        (result) => {
          // Post result back up to the top frame so side_panel.js can receive it.
          try { window.top.postMessage({ type: "SENSEI_IFRAME_RESULT", result, actionId: evt.data.actionId }, "*"); }
          catch (_e) { /* cross-origin top — result delivered via service worker */ }
        }
      );
    } catch (_e) {
      // chrome.runtime not available (e.g. context invalidated); ignore.
    }
  });
}

globalThis.__SENSEI_PAGE_OBSERVER_STATE__ = globalThis.__SENSEI_PAGE_OBSERVER_STATE__ || {
  version: 0,
  last_change_ts: Date.now(),
  last_reason: "init",
  url: location.href,
  ready_state: document.readyState,
};

function bumpPageObservation(reason) {
  const obs = globalThis.__SENSEI_PAGE_OBSERVER_STATE__;
  obs.version += 1;
  obs.last_change_ts = Date.now();
  obs.last_reason = safePageText ? safePageText(reason, 80) : String(reason || "").slice(0, 80);
  obs.url = location.href;
  obs.ready_state = document.readyState;
}

function installPageObservationHooks() {
  if (globalThis.__SENSEI_PAGE_OBSERVER_INSTALLED__) return;
  globalThis.__SENSEI_PAGE_OBSERVER_INSTALLED__ = true;

  const wrapHistory = (name) => {
    const original = history[name];
    if (typeof original !== "function") return;
    history[name] = function senseiHistoryWrapper(...args) {
      const out = original.apply(this, args);
      bumpPageObservation(`history.${name}`);
      return out;
    };
  };
  wrapHistory("pushState");
  wrapHistory("replaceState");
  window.addEventListener("hashchange", () => bumpPageObservation("hashchange"), true);
  window.addEventListener("popstate", () => bumpPageObservation("popstate"), true);
  document.addEventListener("readystatechange", () => {
    if (document.readyState === "complete") bumpPageObservation("readyState.complete");
  }, true);

  let mutationTimer = 0;
  let _lastInteractiveCount = document.querySelectorAll(ACTION_TARGETS).length;
  const scheduleMutationBump = () => {
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(() => {
      bumpPageObservation("mutation.debounced");
      // ── Stale map guard ────────────────────────────────────────────────
      // When the interactive element count shifts by more than 5 (SPA route
      // change, dynamic form injection, etc.) invalidate the ref-ID map so
      // the next interactiveElements() call rebuilds it fresh. Lazy — not
      // rebuilt here, just cleared so the first observation after the change
      // stamps clean refs without collisions.
      try {
        const currentCount = document.querySelectorAll(ACTION_TARGETS).length;
        if (Math.abs(currentCount - _lastInteractiveCount) > 5) {
          _lastInteractiveCount = currentCount;
          globalThis.__pupilElementMap = {};
          globalThis.__pupilCounter = 0;
          document.querySelectorAll("[data-pupil-id]").forEach(
            (stale) => stale.removeAttribute("data-pupil-id")
          );
        }
      } catch (_e) { /* never break page observation */ }
    }, PAGE_STABLE_DEBOUNCE_MS);
  };
  const root = document.querySelector("main,[role='main'],#main,[data-testid*='main'],[aria-label*='main' i]") || document.body || document.documentElement;
  if (root && MutationObserver) {
    const observer = new MutationObserver(scheduleMutationBump);
    observer.observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["aria-label", "aria-selected", "aria-expanded", "role", "hidden", "style", "class"],
    });
    globalThis.__SENSEI_PAGE_OBSERVER__ = observer;
  }
}

globalThis.__SENSEI_CONSOLE_EVENTS__ = globalThis.__SENSEI_CONSOLE_EVENTS__ || [];
if (!globalThis.__SENSEI_CONSOLE_CAPTURE_INSTALLED__) {
  globalThis.__SENSEI_CONSOLE_CAPTURE_INSTALLED__ = true;
  const pushConsoleEvent = (level, args) => {
    try {
      const message = Array.from(args || []).map((arg) => {
        if (arg instanceof Error) return `${arg.name}: ${arg.message}`;
        if (typeof arg === "object") return JSON.stringify(arg).slice(0, 800);
        return String(arg);
      }).join(" ");
      globalThis.__SENSEI_CONSOLE_EVENTS__.push({
        level,
        message: safePageText ? safePageText(message, 800) : String(message).slice(0, 800),
        ts: new Date().toISOString()
      });
      globalThis.__SENSEI_CONSOLE_EVENTS__ = globalThis.__SENSEI_CONSOLE_EVENTS__.slice(-CONSOLE_LIMIT);
    } catch (_err) {
      // Console capture must never break the page.
    }
  };
  for (const level of ["error", "warn"]) {
    const original = console[level];
    console[level] = function senseiConsoleWrapper(...args) {
      pushConsoleEvent(level, args);
      return original.apply(this, args);
    };
  }
  window.addEventListener("error", (event) => {
    pushConsoleEvent("error", [`${event.message || "Script error"} at ${event.filename || "unknown"}:${event.lineno || 0}`]);
  });
  window.addEventListener("unhandledrejection", (event) => {
    pushConsoleEvent("error", [`Unhandled promise rejection: ${event.reason?.message || event.reason || "unknown"}`]);
  });
}

// ─── RANK 1 page_context sanitizer — client defense-in-depth.
// Mirrors stt_server.py contract bit-exact (same step order, same scrub
// targets, same replacement literal). Server re-runs all of this on receipt
// and Test #8 in the server suite pins that the server alone is sufficient,
// so any client divergence is caught. Spec lives in:
// ~/.claude/plans/auto-did-not-actually-stateful-wozniak.md.
const _BIDI_ZWSP_RE = /[​‌‍‎‏‪-‮⁦-⁩﻿]/g;
const _DIRECTIVE_VERBS = [
  // Order: longest-first so RUNTERM matches before RUN.
  "RUNTERM", "REMEMBER", "CREATE", "READ", "EDIT",
  "THINK", "DONE", "RUN", "ASK",
];
const _SCRUB_REPLACEMENT = "[scrubbed directive]";

function _escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function _verbRe(verb) {
  return new RegExp(`\\b${_escapeRegex(verb)}\\s*:`, "gi");
}
function _spacedVerbRe(verb) {
  const spaced = verb.split("").map(_escapeRegex).join("[ \\t]");
  return new RegExp(`\\b${spaced}\\s*:`, "gi");
}
const _VERB_PATTERNS = _DIRECTIVE_VERBS.map(_verbRe);
const _SPACED_VERB_PATTERNS = _DIRECTIVE_VERBS
  .filter((v) => v.length >= 2)
  .map(_spacedVerbRe);
const _BROWSER_RE = /\bBROWSER_[A-Z_]+\s*:/gi;
const _BLOCK_MARKER_RE = /<<<CONTENT|>>>CONTENT|<<<FIND|>>>FIND|<<<REPLACE|>>>REPLACE/gi;
// <PLAN READY> matched first so <PLAN> doesn't shadow it.
const _PLAN_MARKER_RE = /<PLAN READY>|<\/PLAN>|<PLAN>/gi;

function sanitizePageString(text) {
  if (!text) return text || "";
  let cleaned = String(text).replace(_BIDI_ZWSP_RE, "");
  for (const pattern of _VERB_PATTERNS) cleaned = cleaned.replace(pattern, _SCRUB_REPLACEMENT);
  for (const pattern of _SPACED_VERB_PATTERNS) cleaned = cleaned.replace(pattern, _SCRUB_REPLACEMENT);
  cleaned = cleaned.replace(_BROWSER_RE, _SCRUB_REPLACEMENT);
  cleaned = cleaned.replace(_BLOCK_MARKER_RE, _SCRUB_REPLACEMENT);
  cleaned = cleaned.replace(_PLAN_MARKER_RE, _SCRUB_REPLACEMENT);
  return cleaned;
}

function clipText(value, limit) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!limit || text.length <= limit) return text;
  return `${text.slice(0, limit).trim()}...`;
}

function safePageText(value, limit) {
  return sanitizePageString(clipText(value, limit));
}

function _normalizeRouterBase(raw) {
  const value = String(raw || "").trim();
  if (!value) return "";
  if (/^https?:\/\//i.test(value)) return value.replace(/\/+$/, "");
  return "";
}

function _routerBaseFromAction(action) {
  const extras = action?.extras && typeof action.extras === "object" ? action.extras : {};
  const candidates = [
    action?.router_base,
    action?.routerBase,
    action?.backend_url,
    action?.backendUrl,
    extras.router_base,
    extras.routerBase,
    extras.backend_url,
    extras.backendUrl,
    extras.dispatch_base,
    extras.dispatchBase,
    globalThis.__SENSEI_ROUTER_BASE__,
  ];
  for (const raw of candidates) {
    const normalized = _normalizeRouterBase(raw);
    if (normalized) return normalized;
  }
  return DEFAULT_ROUTER_BASE;
}

async function postDispatchEvent(event, payload, action = null) {
  const base = _routerBaseFromAction(action);
  const url = `${base}${DISPATCH_PATH}`;
  const body = {
    event: safePageText(event || "", 80),
    payload: payload && typeof payload === "object" ? payload : {},
    ts: new Date().toISOString(),
  };
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
      mode: "cors",
      credentials: "omit",
    });
    return { ok: resp.ok, status: resp.status };
  } catch (err) {
    return { ok: false, error: String(err?.message || err), dispatch_url: url };
  }
}

function shouldSkipTextNode(node) {
  let el = node.parentElement;
  while (el && el !== document.body) {
    if (SKIP_TEXT_TAGS.has(el.tagName) || el.hidden || el.getAttribute("aria-hidden") === "true") {
      return true;
    }
    el = el.parentElement;
  }
  return false;
}

function visibleText(limit = DEFAULT_VISIBLE_TEXT_LIMIT) {
  const body = document.body;
  if (!body) return "";

  const parts = [];
  let length = 0;
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (shouldSkipTextNode(node) || !String(node.nodeValue || "").trim()) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    }
  });

  while (walker.nextNode() && length < limit) {
    const text = String(walker.currentNode.nodeValue || "").replace(/\s+/g, " ").trim();
    if (!text) continue;
    const remaining = limit - length;
    parts.push(text.length > remaining ? text.slice(0, remaining).trim() : text);
    length += text.length + 1;
  }

  const output = parts.join(" ").trim();
  return sanitizePageString(length >= limit ? `${output.trim()}...` : output);
}

function selectionText() {
  return safePageText(String(window.getSelection?.() || ""), 1200);
}

function cssEscape(value) {
  if (window.CSS?.escape) return CSS.escape(value);
  return String(value || "").replace(/["\\]/g, "\\$&").replace(/[^\w-]/g, "\\$&");
}

function elementText(el, limit = READ_TEXT_LIMIT) {
  if (!el) return "";
  if (el === document.body || el === document.documentElement) return visibleText(limit);
  return safePageText(el.value || el.innerText || el.textContent || el.getAttribute("aria-label") || "", limit);
}

function elementRole(el) {
  const explicit = el.getAttribute("role");
  if (explicit) return explicit;
  const tag = el.tagName.toLowerCase();
  if (tag === "a") return "link";
  if (tag === "button") return "button";
  if (tag === "select") return "select";
  if (tag === "textarea") return "textbox";
  if (tag === "input") {
    const type = String(el.getAttribute("type") || "text").toLowerCase();
    if (type === "checkbox") return "checkbox";
    if (type === "radio") return "radio";
    if (type === "submit" || type === "button") return "button";
    return "textbox";
  }
  return tag;
}

function elementName(el) {
  return safePageText([
    el.getAttribute("aria-label"),
    el.getAttribute("title"),
    el.getAttribute("placeholder"),
    el.value,
    el.textContent
  ].filter(Boolean).join(" "), 120);
}

function selectorFor(el) {
  const tag = el.tagName.toLowerCase();
  const id = el.getAttribute("id");
  if (id) return `#${cssEscape(id)}`;
  const name = el.getAttribute("name");
  if (name) return `${tag}[name="${cssEscape(name)}"]`;
  const aria = el.getAttribute("aria-label");
  if (aria) return `${tag}[aria-label="${cssEscape(aria)}"]`;
  const placeholder = el.getAttribute("placeholder");
  if (placeholder) return `${tag}[placeholder="${cssEscape(placeholder)}"]`;
  return tag;
}

function structuralSelectorFor(el) {
  const parts = [];
  let current = el;
  while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.documentElement) {
    const tag = current.tagName.toLowerCase();
    const id = current.getAttribute("id");
    if (id && sanitizePageString(id) === id) {
      parts.unshift(`${tag}#${cssEscape(id)}`);
      break;
    }

    let index = 1;
    let sibling = current.previousElementSibling;
    while (sibling) {
      if (sibling.tagName === current.tagName) index += 1;
      sibling = sibling.previousElementSibling;
    }
    parts.unshift(`${tag}:nth-of-type(${index})`);
    current = current.parentElement;
  }
  return parts.length ? parts.join(" > ") : el.tagName.toLowerCase();
}

function safeSelectorFor(el) {
  const selector = selectorFor(el);
  const tag = el?.tagName?.toLowerCase?.() || "";
  const role = String(el?.getAttribute?.("role") || "").toLowerCase();
  const forceStructural = (
    selector === tag &&
    (tag === "tr" || tag === "td" || tag === "li" ||
     role === "row" || role === "gridcell" || role === "treeitem" || role === "listitem")
  );
  if (forceStructural) return structuralSelectorFor(el);
  return sanitizePageString(selector) === selector ? selector : structuralSelectorFor(el);
}

function collectDeep(selector, limit = 1500) {
  const found = [];
  const seen = new Set();

  const add = (el) => {
    if (!el || seen.has(el) || found.length >= limit) return;
    seen.add(el);
    found.push(el);
  };

  const walkRoot = (root) => {
    if (!root || found.length >= limit) return;
    try {
      root.querySelectorAll(selector).forEach(add);
    } catch (_err) {
      return;
    }
    let all = [];
    try {
      all = Array.from(root.querySelectorAll("*"));
    } catch (_err) {
      all = [];
    }
    for (const el of all) {
      if (found.length >= limit) break;
      if (el.shadowRoot) walkRoot(el.shadowRoot);
      if (el.tagName === "IFRAME" || el.tagName === "FRAME") {
        try {
          if (!el.contentDocument) continue;
          walkRoot(el.contentDocument);
        } catch (_err) {
          // Cross-origin frames are summarized separately; content is not readable here.
        }
      }
    }
  };

  walkRoot(document);
  return found;
}

function elementBounds(el) {
  try {
    const rect = el.getBoundingClientRect();
    return {
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    };
  } catch (_err) {
    return null;
  }
}

function interactiveElements(limit = INTERACTIVE_LIMIT) {
  const candidates = collectDeep(ACTION_TARGETS, limit * 10)
    .filter(isVisible)
    .slice(0, limit);

  // ── Ref-ID stamping ──────────────────────────────────────────────────────
  // Each visible interactive element gets a stable data-pupil-id on first
  // observation. Existing stamps are preserved across repeated calls so refs
  // remain stable within a page load. The map is rebuilt incrementally.
  candidates.forEach((el) => {
    if (!el.hasAttribute("data-pupil-id")) {
      globalThis.__pupilCounter += 1;
      el.setAttribute("data-pupil-id", `p_${String(globalThis.__pupilCounter).padStart(3, "0")}`);
    }
    const ref = el.getAttribute("data-pupil-id");
    const role = elementRole(el);
    const label = elementName(el) || "";
    globalThis.__pupilElementMap[ref] = {
      type: role,
      label,
      placeholder: el.getAttribute("placeholder") || "",
      value: currentElementValue ? currentElementValue(el) : (el.value || ""),
      el,
    };
  });

  // Return structured objects — the model sees ref IDs, not fragile text selectors.
  // Layer 3 constraint annotations (required, maxlength, etc.) are preserved.
  return candidates.map((el) => {
    const cons = _extractFieldConstraints(el);
    const entry = {
      ref: el.getAttribute("data-pupil-id"),
      type: elementRole(el),
      label: elementName(el) || "",
      placeholder: el.getAttribute("placeholder") || "",
      selector: safeSelectorFor(el),
    };
    if (Object.keys(cons).length) entry.constraints = cons;
    return entry;
  });
}

function semanticFallbackElements(limit = 140) {
  const selectors = [
    "main", "[role='main']", "header", "[role='banner']", "nav", "[role='navigation']",
    "section", "article", "form", "[role='form']", "dialog", "[role='dialog']",
    "h1,h2,h3,[role='heading']", ACTION_TARGETS, "table,[role='table'],[role='grid']",
    "tr,[role='row']", "li,[role='listitem']"
  ].join(",");
  return collectDeep(selectors, limit * 12)
    .filter(isVisible)
    .slice(0, limit)
    .map((el) => ({
      role: elementRole(el),
      name: elementName(el) || elementText(el, 160),
      selector: safeSelectorFor(el),
      bounds: elementBounds(el),
      value_present: Boolean(currentElementValue(el).trim()),
      expanded: el.getAttribute("aria-expanded") || "",
      selected: el.getAttribute("aria-selected") || "",
      checked: el.getAttribute("aria-checked") || "",
    }))
    .filter((item) => item.name || item.role);
}

function iframeSummaries(limit = 30) {
  return Array.from(document.querySelectorAll("iframe,frame"))
    .filter(isVisible)
    .slice(0, limit)
    .map((frame, index) => {
      const base = {
        index,
        selector: safeSelectorFor(frame),
        src: safePageText(frame.getAttribute("src") || frame.src || "", 800),
        title: safePageText(frame.getAttribute("title") || "", 200),
        name: safePageText(frame.getAttribute("name") || "", 160),
        bounds: elementBounds(frame),
      };
      try {
        const doc = frame.contentDocument;
        if (!doc) throw new Error("contentDocument unavailable");
        const text = safePageText(doc.body?.innerText || doc.body?.textContent || "", 600);
        return {
          ...base,
          same_origin: true,
          title_observed: safePageText(doc.title || "", 200),
          summary_text: text,
          interactive_count: doc.querySelectorAll(ACTION_TARGETS).length,
        };
      } catch (err) {
        return {
          ...base,
          cross_origin: true,
          unobserved_reason: "cross-origin frame; content script records metadata only",
        };
      }
    });
}

function consoleState() {
  const events = Array.isArray(globalThis.__SENSEI_CONSOLE_EVENTS__)
    ? globalThis.__SENSEI_CONSOLE_EVENTS__.slice(-CONSOLE_LIMIT)
    : [];
  return events.map((event) => ({
    level: safePageText(event.level || "", 20),
    message: safePageText(event.message || "", 800),
    ts: safePageText(event.ts || "", 40)
  }));
}

function domState() {
  const forms = collectDeep("form", 80).filter(isVisible).slice(0, 20).map((form, index) => ({
    index,
    selector: safeSelectorFor(form),
    fields: Array.from(form.querySelectorAll("input, textarea, select")).filter(isVisible).slice(0, 40).map((field) => ({
      role: elementRole(field),
      name: renderedLabelFor(field) || elementName(field) || field.getAttribute("name") || "",
      selector: safeSelectorFor(field),
      type: safePageText(field.getAttribute("type") || field.tagName.toLowerCase(), 40),
      value_present: Boolean(currentElementValue(field).trim())
    }))
  }));
  const headings = collectDeep("h1,h2,h3,[role='heading']", 120)
    .filter(isVisible)
    .slice(0, 20)
    .map((el) => elementText(el, 160))
    .filter(Boolean);
  return {
    forms,
    headings,
    counts: {
      forms: document.forms?.length || 0,
      inputs: document.querySelectorAll("input, textarea, select").length,
      buttons: document.querySelectorAll("button,[role='button']").length,
      links: document.querySelectorAll("a[href]").length,
    }
  };
}

function pageContext(options = {}) {
  const includeVisibleText = options.includeVisibleText !== false;
  const includeInteractiveElements = options.includeInteractiveElements !== false;
  const visibleTextLimit = Number(options.visibleTextLimit || DEFAULT_VISIBLE_TEXT_LIMIT);
  const includeSemanticFallback = options.includeSemanticFallback !== false;
  const active = document.activeElement;
  const focused = active && active !== document.body && active !== document.documentElement
    ? elementText(active, FOCUSED_TEXT_LIMIT)
    : "";
  const context = {
    url: safePageText(location.href, 2000),
    title: safePageText(document.title || "", 300),
    selection: selectionText(),
    focused_text: focused
  };
  if (includeInteractiveElements) context.interactive_elements = interactiveElements();
  if (includeVisibleText) context.visible_text = visibleText(visibleTextLimit);
  if (includeSemanticFallback) context.semantic_fallback = semanticFallbackElements();
  context.iframes = iframeSummaries();
  context.dom_state = domState();
  context.console_state = consoleState();
  context.observe_state = {
    version: globalThis.__SENSEI_PAGE_OBSERVER_STATE__.version,
    last_reason: globalThis.__SENSEI_PAGE_OBSERVER_STATE__.last_reason,
    last_change_ms_ago: Math.max(0, Date.now() - globalThis.__SENSEI_PAGE_OBSERVER_STATE__.last_change_ts),
    url: safePageText(globalThis.__SENSEI_PAGE_OBSERVER_STATE__.url || location.href, 2000),
    ready_state: safePageText(document.readyState, 40),
    triggers: [
      "url",
      "hashchange",
      "history.pushState",
      "history.replaceState",
      "popstate",
      "readyState.complete",
      "main-content MutationObserver debounced"
    ]
  };
  return context;
}

async function waitForPageStable(minQuietMs = PAGE_STABLE_DEBOUNCE_MS, maxWaitMs = PAGE_STABLE_MAX_WAIT_MS) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    const obs = globalThis.__SENSEI_PAGE_OBSERVER_STATE__;
    if (document.readyState === "complete" && Date.now() - obs.last_change_ts >= minQuietMs) {
      return { stable: true, waited_ms: Date.now() - start };
    }
    await sleep(80);
  }
  return { stable: false, waited_ms: Date.now() - start };
}

async function pageContextAsync(options = {}) {
  if (Number(options.waitForStableMs || 0) > 0) {
    await waitForPageStable(
      Math.max(100, Math.min(Number(options.waitForStableMs), 2000)),
      Math.max(500, Math.min(Number(options.maxWaitMs || PAGE_STABLE_MAX_WAIT_MS), 8000))
    );
  }
  return pageContext(options);
}

// Phase 9 — workflow recording. Captures actionable DOM events as both a
// lightweight rrweb-style event stream and directly replayable BROWSER_* steps.
globalThis.__SENSEI_WORKFLOW_RECORDING__ = globalThis.__SENSEI_WORKFLOW_RECORDING__ || {
  active: false,
  started_at: "",
  events: [],
  rrweb_events: [],
  steps: [],
  handlers: null,
  rrweb_stop: null,
};

function recordedLabelFor(el) {
  return safePageText(renderedLabelFor(el) || elementName(el) || elementText(el, 80), 120);
}

function pushWorkflowEvent(type, el, extra = {}) {
  const rec = globalThis.__SENSEI_WORKFLOW_RECORDING__;
  if (!rec.active) return;
  const selector = el ? safeSelectorFor(el) : "";
  const label = el ? recordedLabelFor(el) : "";
  const event = {
    type,
    ts: Date.now(),
    url: safePageText(location.href, 2000),
    title: safePageText(document.title || "", 300),
    selector,
    label,
    role: el ? elementRole(el) : "",
    ...extra,
  };
  rec.events.push(event);
  if (rec.events.length > 500) rec.events.shift();
  const step = eventToWorkflowStep(event);
  if (step) {
    const prev = rec.steps[rec.steps.length - 1];
    if (prev && prev.kind === step.kind && prev.target === step.target) {
      rec.steps[rec.steps.length - 1] = step;
    } else {
      rec.steps.push(step);
      if (rec.steps.length > 120) rec.steps.shift();
    }
  }
}

function eventToWorkflowStep(event) {
  if (!event || !event.selector) return null;
  if (event.type === "click") {
    return {
      kind: "BROWSER_CLICK",
      target: event.selector,
      label: event.label || event.selector,
    };
  }
  if (event.type === "dblclick") {
    return {
      kind: "BROWSER_DOUBLE_CLICK",
      target: event.selector,
      label: event.label || event.selector,
    };
  }
  if (event.type === "input" || event.type === "change") {
    return {
      kind: "BROWSER_FILL",
      target: event.selector,
      value: safePageText(event.value || "", 1000),
      label: event.label || event.selector,
    };
  }
  return null;
}

function startWorkflowRecording() {
  const rec = globalThis.__SENSEI_WORKFLOW_RECORDING__;
  if (rec.active) return { ok: true, already_recording: true };
  rec.active = true;
  rec.started_at = new Date().toISOString();
  rec.events = [{
    type: "navigation",
    ts: Date.now(),
    url: safePageText(location.href, 2000),
    title: safePageText(document.title || "", 300),
  }];
  rec.rrweb_events = [];
  rec.steps = [{
    kind: "BROWSER_NAV",
    target: location.href,
    label: document.title || location.href,
  }];
  if (globalThis.rrweb?.record) {
    try {
      rec.rrweb_stop = globalThis.rrweb.record({
        emit(event) {
          rec.rrweb_events.push(event);
          if (rec.rrweb_events.length > 500) rec.rrweb_events.shift();
        }
      });
    } catch (_err) {
      rec.rrweb_stop = null;
    }
  }
  const onClick = (event) => pushWorkflowEvent("click", event.target);
  const onDblClick = (event) => pushWorkflowEvent("dblclick", event.target);
  const onInput = (event) => pushWorkflowEvent("input", event.target, {
    value: currentElementValue(event.target),
  });
  const onChange = (event) => pushWorkflowEvent("change", event.target, {
    value: currentElementValue(event.target),
  });
  document.addEventListener("click", onClick, true);
  document.addEventListener("dblclick", onDblClick, true);
  document.addEventListener("input", onInput, true);
  document.addEventListener("change", onChange, true);
  rec.handlers = { onClick, onDblClick, onInput, onChange };
  return { ok: true, started_at: rec.started_at, url: location.href };
}

function stopWorkflowRecording() {
  const rec = globalThis.__SENSEI_WORKFLOW_RECORDING__;
  if (!rec.active) {
    return { ok: true, active: false, events: rec.events || [], steps: rec.steps || [] };
  }
  const h = rec.handlers || {};
  document.removeEventListener("click", h.onClick, true);
  document.removeEventListener("dblclick", h.onDblClick, true);
  document.removeEventListener("input", h.onInput, true);
  document.removeEventListener("change", h.onChange, true);
  if (rec.rrweb_stop) {
    try { rec.rrweb_stop(); } catch (_err) {}
  }
  rec.active = false;
  rec.handlers = null;
  rec.rrweb_stop = null;
  return {
    ok: true,
    active: false,
    started_at: rec.started_at,
    stopped_at: new Date().toISOString(),
    events: rec.events || [],
    rrweb_events: rec.rrweb_events || [],
    steps: rec.steps || [],
    url: location.href,
    title: document.title || "",
  };
}

installPageObservationHooks();

function isVisible(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
}

function hasAriaHiddenAncestor(el) {
  let cur = el;
  while (cur && cur.nodeType === Node.ELEMENT_NODE) {
    if (cur.getAttribute && cur.getAttribute("aria-hidden") === "true") return true;
    cur = cur.parentElement;
  }
  return false;
}

function actionableCategoryFor(el) {
  if (!el) return "text_input";
  const tag = String(el.tagName || "").toLowerCase();
  const role = String(el.getAttribute?.("role") || "").toLowerCase();
  if (tag === "a" || role === "link") return "link";
  if (tag === "button" || role === "button") return "button";
  if (tag === "select" || role === "combobox") return "select";
  if (tag === "textarea") return "textarea";
  if (tag === "input") {
    const type = String(el.getAttribute("type") || "text").toLowerCase();
    if (type === "email") return "email_input";
    if (type === "password") return "password_input";
    if (type === "checkbox") return "checkbox";
    if (type === "radio") return "radio";
    if (type === "button" || type === "submit" || type === "reset") return "button";
    return "text_input";
  }
  if (role === "checkbox") return "checkbox";
  if (role === "radio") return "radio";
  if (role === "textbox") return "text_input";
  if (el.isContentEditable) return "textarea";
  return "text_input";
}

function actionableTypeFor(el) {
  const tag = String(el?.tagName || "").toLowerCase();
  if (!tag) return "";
  if (tag === "input") return String(el.getAttribute("type") || "text").toLowerCase();
  return tag;
}

function actionableValueFor(el, category) {
  if (!el) return null;
  if (category === "checkbox") return el.checked ? "checked" : null;
  if (category === "radio") {
    const checked = Boolean(el.checked || el.getAttribute("aria-checked") === "true");
    if (!checked) return null;
    return safePageText(el.value || renderedLabelFor(el) || "selected", 200);
  }
  if (category === "button" || category === "link") return null;
  const v = currentElementValue(el);
  return v ? safePageText(v, 300) : null;
}

function actionableLabelFor(el) {
  return safePageText(
    renderedLabelFor(el) || elementName(el) || el.innerText || el.textContent || "",
    240
  );
}

function isActionableCandidate(el) {
  if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
  if (el.hasAttribute("disabled")) return false;
  if (hasAriaHiddenAncestor(el)) return false;
  if (!isVisible(el)) return false;
  const category = actionableCategoryFor(el);
  return [
    "button",
    "text_input",
    "email_input",
    "password_input",
    "select",
    "checkbox",
    "radio",
    "link",
    "textarea",
  ].includes(category);
}

function _readPageElementRect(el) {
  const bounds = elementBounds(el);
  if (!bounds) return { x: 0, y: 0, w: 0, h: 0 };
  return {
    x: Number(bounds.x || 0),
    y: Number(bounds.y || 0),
    w: Number(bounds.width || 0),
    h: Number(bounds.height || 0),
  };
}

function _readPageRole(el) {
  return safePageText(String(el.getAttribute("role") || String(el.tagName || "").toLowerCase()), 60);
}

function _readPageName(el) {
  return safePageText(
    renderedLabelFor(el)
      || elementName(el)
      || (isVisible(el) ? elementText(el, 240) : "")
      || el.getAttribute("name")
      || "",
    240
  );
}

function _isInteractiveForReadPage(el) {
  if (!el) return false;
  if (isActionableCandidate(el)) return true;
  const tag = String(el.tagName || "").toLowerCase();
  return tag === "form" || String(el.getAttribute("role") || "").toLowerCase() === "form";
}

function _collectReadPageFallbackElements(limit = READ_PAGE_FULL_LIMIT) {
  const out = [];
  const selector = "main,section,article,form,[role],button,a,input,select,textarea,label,h1,h2,h3,p,li,div,span";
  const seen = new Set();
  for (const el of collectDeep(selector, limit * 20)) {
    if (!el || seen.has(el)) continue;
    seen.add(el);
    if (hasAriaHiddenAncestor(el)) continue;
    if (!isVisible(el)) continue;
    const rect = _readPageElementRect(el);
    if (rect.w <= 0 || rect.h <= 0) continue;
    const text = _readPageName(el);
    const interactive = _isInteractiveForReadPage(el);
    if (!interactive && !text) continue;
    out.push(el);
    if (out.length >= limit) break;
  }
  return out;
}

function collectReadPageFullElements(limit = READ_PAGE_FULL_LIMIT) {
  const selector = [
    "form",
    "[role='form']",
    "button",
    "a[href]",
    "input",
    "textarea",
    "select",
    "[role]",
    "[aria-label]",
    "[contenteditable='true']",
  ].join(",");
  const seeded = collectDeep(selector, limit * 30);
  const source = seeded.length >= 25 ? seeded : _collectReadPageFallbackElements(limit * 4);
  const rows = [];
  const seen = new Set();
  for (const el of source) {
    if (!el || seen.has(el)) continue;
    seen.add(el);
    if (hasAriaHiddenAncestor(el)) continue;
    const visible = isVisible(el);
    if (!visible) continue;
    const rect = _readPageElementRect(el);
    if (rect.w <= 0 || rect.h <= 0) continue;
    const interactive = _isInteractiveForReadPage(el);
    const roleAttr = String(el.getAttribute("role") || "");
    const roleLike = Boolean(roleAttr);
    const name = _readPageName(el);
    if (!interactive && !roleLike && !name) continue;
    const category = actionableCategoryFor(el);
    rows.push({
      _el: el,
      role: _readPageRole(el),
      name,
      value: safePageText(currentElementValue(el), 300),
      visible,
      rect,
      selector: safeSelectorFor(el),
      tag: String(el.tagName || "").toLowerCase(),
      type: actionableTypeFor(el),
      category,
      label: actionableLabelFor(el),
      required: Boolean(el.required || String(el.getAttribute("aria-required") || "").toLowerCase() === "true"),
      _priority: interactive ? 3 : roleLike ? 2 : 1,
    });
  }
  rows.sort((a, b) => {
    if (b._priority !== a._priority) return b._priority - a._priority;
    if (a.rect.y !== b.rect.y) return a.rect.y - b.rect.y;
    return a.rect.x - b.rect.x;
  });
  const out = [];
  for (const row of rows.slice(0, limit)) {
    out.push({
      ...row,
      ref: `ref_${out.length + 1}`,
    });
  }
  return out;
}

function elementRefLookup(limit = READ_PAGE_FULL_LIMIT) {
  const lookup = new Map();
  for (const row of collectReadPageFullElements(limit)) lookup.set(row._el, row.ref);
  return lookup;
}

function parseProgressText(raw) {
  const text = String(raw || "");
  const patterns = [
    /(?:step|page|section|question)\s*(\d{1,3})\s*(?:\/|of)\s*(\d{1,3})/i,
    /(\d{1,3})\s*of\s*(\d{1,3})\s*(?:steps?|pages?|sections?|questions?)/i,
  ];
  for (const re of patterns) {
    const m = text.match(re);
    if (!m) continue;
    const current = Number(m[1]);
    const total = Number(m[2]);
    if (Number.isFinite(current) && Number.isFinite(total) && current > 0 && total >= current) {
      return { current, total };
    }
  }
  return null;
}

function detectProgress() {
  try {
    const bar = document.querySelector("progress[value][max],[aria-valuenow][aria-valuemax]");
    if (bar) {
      const current = Number(bar.getAttribute("aria-valuenow") || bar.getAttribute("value"));
      const total = Number(bar.getAttribute("aria-valuemax") || bar.getAttribute("max"));
      if (Number.isFinite(current) && Number.isFinite(total) && current > 0 && total >= current) {
        return { current, total };
      }
    }
  } catch (_err) {}
  const fromVisible = parseProgressText(visibleText(5000));
  if (fromVisible) return fromVisible;
  return parseProgressText(`${document.title || ""} ${location.href || ""}`);
}

function buttonLikeElements() {
  const selector = "button,input[type='submit'],input[type='button'],[role='button'],a[role='button']";
  return collectDeep(selector, 400).filter((el) => isVisible(el) && !hasAriaHiddenAncestor(el));
}

function isPrimaryStyledButton(el) {
  if (!el) return false;
  const classes = [
    el.className || "",
    el.id || "",
    el.getAttribute("data-testid") || "",
    el.getAttribute("aria-label") || "",
    el.getAttribute("name") || "",
    el.getAttribute("type") || "",
  ].join(" ").toLowerCase();
  if (/(?:^|\b)(primary|submit|apply|finish|complete|btn-primary|button--primary)(?:\b|$)/.test(classes)) return true;
  try {
    const style = window.getComputedStyle(el);
    const bg = String(style.backgroundColor || "");
    const fg = String(style.color || "");
    const weight = Number(style.fontWeight) || 0;
    if (weight >= 600 && bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent" && fg) return true;
  } catch (_err) {}
  return false;
}

function submitSignalsForElement(el) {
  const signals = [];
  const label = safePageText((renderedLabelFor(el) || elementText(el, 180) || ""), 180);
  const compact = label.replace(/\s+/g, " ").trim();
  if (SUBMIT_TEXT_RE.test(compact)) signals.push("visible_text");
  const tag = String(el.tagName || "").toLowerCase();
  const role = String(el.getAttribute("role") || "").toLowerCase();
  const type = String(el.getAttribute("type") || "").toLowerCase();
  if (role === "button" || tag === "button" || (tag === "input" && type === "submit")) signals.push("button_role_or_tag");
  if (el.closest && el.closest("form,[role='form']")) signals.push("inside_form");
  const attrs = [
    el.getAttribute("aria-label") || "",
    el.getAttribute("data-testid") || "",
    el.getAttribute("data-action") || "",
    el.getAttribute("name") || "",
  ].join(" ");
  if (/submit|apply|finish|complete|send/i.test(attrs)) signals.push("submit_intent_attr");
  const buttons = buttonLikeElements();
  const actionContainer = el.closest("footer,[role='toolbar'],[class*='footer' i],[class*='toolbar' i],[class*='action' i]");
  const peers = actionContainer
    ? buttons.filter((btn) => actionContainer.contains(btn))
    : buttons;
  if (peers.length > 0) {
    const rect = el.getBoundingClientRect();
    const maxRight = Math.max(...peers.map((btn) => {
      const r = btn.getBoundingClientRect();
      return r.left + r.width;
    }));
    const maxBottom = Math.max(...peers.map((btn) => {
      const r = btn.getBoundingClientRect();
      return r.top + r.height;
    }));
    const right = rect.left + rect.width;
    const bottom = rect.top + rect.height;
    if (right >= maxRight - 2 || bottom >= maxBottom - 2) signals.push("rightmost_or_bottommost");
  }
  return signals;
}

function detectSubmitPage(refLookup = null) {
  const buttons = buttonLikeElements();
  const refs = refLookup || elementRefLookup();
  const ranked = [];
  for (const el of buttons) {
    const signals = submitSignalsForElement(el);
    if (signals.length >= 2) {
      const rect = _readPageElementRect(el);
      ranked.push({
        ref: refs.get(el) || null,
        signals,
        signal_count: signals.length,
        selector: safeSelectorFor(el),
        name: _readPageName(el),
        rect,
      });
    }
  }
  ranked.sort((a, b) => {
    if (b.signal_count !== a.signal_count) return b.signal_count - a.signal_count;
    if (b.rect.y !== a.rect.y) return b.rect.y - a.rect.y;
    return b.rect.x - a.rect.x;
  });
  return {
    is_submit_page: ranked.length > 0,
    submit_ref: ranked[0]?.ref || null,
    submit_signals: ranked[0]?.signals || [],
    submit_candidates: ranked,
  };
}

function extractReferenceNumberDetailed() {
  const body = `${visibleText(12000)}\n${document.body?.innerText || ""}`;
  for (const re of REFERENCE_BATTERY) {
    const m = body.match(re);
    const candidate = safePageText((m && (m[2] || m[1])) || "", 140);
    if (candidate) {
      return { reference_number: candidate, source: re.source, fallback: false };
    }
  }
  const urlMatch = String(location.href || "").match(INDEED_REFERENCE_RE);
  if (urlMatch && urlMatch[1]) {
    return {
      reference_number: safePageText(urlMatch[1], 140),
      source: "indeed_url",
      fallback: false,
    };
  }
  const fallback = `no_reference_captured_fallback:${safePageText(location.href || "", 2000)}#${Date.now()}`;
  return {
    reference_number: fallback,
    source: "fallback",
    fallback: true,
  };
}

function extractReferenceNumber() {
  return extractReferenceNumberDetailed().reference_number;
}

function detectConfirmationPage() {
  const urlHit = CONFIRM_URL_RE.test(location.href || "");
  const bodyText = visibleText(12000);
  const bodyHit = CONFIRM_TEXT_RE.test(bodyText);
  let liveHit = false;
  try {
    const liveNodes = collectDeep("[role='status'],[aria-live]", 120).filter((el) => isVisible(el));
    liveHit = liveNodes.some((el) => CONFIRM_TEXT_RE.test(elementText(el, 1200)));
  } catch (_err) {}
  const ref = extractReferenceNumberDetailed();
  const refHit = Boolean(ref.reference_number && !ref.fallback);
  const is_confirmation_page = Boolean(urlHit || bodyHit || liveHit || refHit);
  return {
    is_confirmation_page,
    confirmation_ref: is_confirmation_page ? ref.reference_number : null,
    confirmation_source: ref.source,
    reference_fallback: ref.fallback,
  };
}

function buildReadPageFullPayload() {
  const rows = collectReadPageFullElements(READ_PAGE_FULL_LIMIT);
  const refLookup = new Map();
  for (const row of rows) refLookup.set(row._el, row.ref);
  const submit = detectSubmitPage(refLookup);
  const confirm = detectConfirmationPage();
  const forms = collectDeep("form,[role='form']", 120)
    .filter((el) => isVisible(el) && !hasAriaHiddenAncestor(el))
    .map((formEl) => {
      const formRef = refLookup.get(formEl) || null;
      const fieldRefs = rows
        .filter((row) => formEl.contains(row._el) && row.ref !== formRef)
        .map((row) => row.ref);
      const submitRefs = submit.submit_candidates
        .filter((candidate) => {
          const match = rows.find((row) => row.ref === candidate.ref);
          return Boolean(match && formEl.contains(match._el));
        })
        .map((candidate) => candidate.ref)
        .filter(Boolean);
      return {
        form_ref: formRef,
        fields: fieldRefs,
        submit_candidates: submitRefs,
      };
    });
  return {
    url: safePageText(location.href || "", 2000),
    title: safePageText(document.title || "", 300),
    viewport: {
      w: Math.round(window.innerWidth || 0),
      h: Math.round(window.innerHeight || 0),
      scroll_y: Math.round(window.scrollY || 0),
    },
    elements: rows.map((row) => ({
      ref: row.ref,
      role: row.role,
      name: row.name,
      value: row.value || "",
      visible: row.visible,
      rect: row.rect,
      selector: row.selector,
    })),
    forms,
    // Compatibility fields kept for orchestrator/session summaries.
    progress: detectProgress(),
    is_submit_page: submit.is_submit_page,
    submit_candidates: submit.submit_candidates,
    is_confirmation_page: confirm.is_confirmation_page,
    confirmation_ref: confirm.confirmation_ref,
  };
}

async function maybePostConfirmationDetected(action = null) {
  const confirm = detectConfirmationPage();
  if (!confirm.is_confirmation_page) return { posted: false, confirmation: confirm };
  const payload = {
    url: safePageText(location.href || "", 2000),
    reference_number: confirm.confirmation_ref || "",
    company: safePageText((document.title || "").split("-")[0] || "", 160),
    title: safePageText(document.title || "", 240),
  };
  const dispatch = await postDispatchEvent("confirmation_detected", payload, action);
  return { posted: true, confirmation: confirm, dispatch };
}

function submittedCountFromAction(action) {
  const direct = Number(action?.applications_submitted_this_session);
  if (Number.isFinite(direct)) return Math.max(0, direct);
  const extras = action?.extras && typeof action.extras === "object" ? action.extras : {};
  const extra = Number(extras.applications_submitted_this_session);
  if (Number.isFinite(extra)) return Math.max(0, extra);
  const summary = action?.state_summary && typeof action.state_summary === "object" ? action.state_summary : {};
  const nested = Number(summary.applications_submitted_this_session);
  if (Number.isFinite(nested)) return Math.max(0, nested);
  return 0;
}

async function dismissSimplifyOverlays(action = null) {
  const state = globalThis.__SENSEI_SIMPLIFY_DISMISS_STATE__;
  const pageKey = `${location.origin}${location.pathname}${location.search}`;
  if (state.page_key !== pageKey) {
    state.page_key = pageKey;
    state.attempts = 0;
  }
  if (state.attempts >= 5) {
    return { closed: 0, attempts: state.attempts, capped: true };
  }
  const overlaySelectors = [
    ".simplify-overlay[role='dialog'], .simplify-overlay",
    "[class*='simplify' i][role='dialog']",
    "[aria-label*='simplify' i][role='dialog']",
  ];
  const iframeSelector = "iframe[src*='simplify.jobs' i]";
  const closeSelectors = [
    "button[aria-label='Close']",
    "button[aria-label*='close' i]",
    "button[title='Close']",
    "button[title*='close' i]",
    "[role='button'][aria-label*='close' i]",
    "button, [role='button']",
  ];
  const overlays = collectDeep(overlaySelectors.join(","), 80).filter((el) => isVisible(el));
  const simplifyFrames = collectDeep(iframeSelector, 20).filter((el) => isVisible(el));
  if (!overlays.length && !simplifyFrames.length) {
    return { closed: 0, attempts: state.attempts, found: false };
  }
  state.attempts += 1;
  let closed = 0;
  const closeButtonIn = (root) => {
    for (const sel of closeSelectors) {
      try {
        const nodes = Array.from(root.querySelectorAll(sel));
        for (const node of nodes) {
          if (!isVisible(node)) continue;
          const text = safePageText(node.innerText || node.textContent || node.getAttribute("aria-label") || "", 80);
          if (sel === "button, [role='button']" && !/^×$|^x$|close/i.test(text)) continue;
          return node;
        }
      } catch (_err) {}
    }
    return null;
  };
  for (const overlay of overlays) {
    const closeBtn = closeButtonIn(overlay) || closeButtonIn(document);
    if (!closeBtn) continue;
    try {
      closeBtn.click();
      closed += 1;
    } catch (_err) {}
  }
  for (const frame of simplifyFrames) {
    const host = frame.closest("div,[role='dialog'],section,aside") || document;
    const closeBtn = closeButtonIn(host);
    if (!closeBtn) continue;
    try {
      closeBtn.click();
      closed += 1;
    } catch (_err) {}
  }
  if (closed > 0) {
    await postDispatchEvent("simplify_dismissed", {
      url: safePageText(location.href || "", 2000),
      attempts: state.attempts,
      closed,
    }, action);
  }
  return { closed, attempts: state.attempts, found: true };
}

// Shadow-piercing walker — used by the DOM-fallback page-read path when the
// extension cannot attach the debugger (chrome:// pages, extension popups,
// or any tab where Accessibility.getFullAXTree is unavailable). Closed shadow
// roots stay unreachable through this walker; closed-shadow regions are only
// trusted via the AX tree. See plan §2.
function walkShadowPiercing(root, callback) {
  if (!root || typeof callback !== "function") return;
  const stack = [root];
  while (stack.length) {
    const node = stack.pop();
    if (!node) continue;
    if (node.nodeType === Node.ELEMENT_NODE) {
      callback(node);
      if (node.shadowRoot && node.shadowRoot.mode === "open") {
        const kids = node.shadowRoot.querySelectorAll("*");
        for (let i = kids.length - 1; i >= 0; i -= 1) stack.push(kids[i]);
      }
    }
    if (node.children && node.children.length) {
      for (let i = node.children.length - 1; i >= 0; i -= 1) stack.push(node.children[i]);
    }
  }
}

function trySelector(target) {
  const raw = String(target || "").trim();
  if (!raw) return null;
  if (raw.startsWith("/") || raw.startsWith("(/")) {
    try {
      const result = document.evaluate(raw, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
      return result?.singleNodeValue || null;
    } catch (_err) {
      return null;
    }
  }
  try {
    return document.querySelector(raw);
  } catch (_err) {
    return null;
  }
}

function normalizedText(el) {
  return [
    el.value,
    el.getAttribute("aria-label"),
    el.getAttribute("title"),
    el.getAttribute("placeholder"),
    clipText(el.textContent, 800)
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim().toLowerCase();
}

function extractTextNeedleFromXPath(raw) {
  const m = raw.match(/(?:text\(\)|contains\(\s*(?:\.|text\(\))\s*,)\s*=?\s*['"]([^'"]+)['"]/i);
  return m ? m[1].toLowerCase() : null;
}

function findElement(target) {
  let raw = String(target || "").trim();
  if (!raw) return null;

  // ── Fast-path: ref-ID lookup ─────────────────────────────────────────────
  // If the model emits a p_NNN ref, resolve it directly through the element
  // map without any selector parsing. Falls through to legacy resolution if
  // the ref is unknown (stale map or model hallucination).
  if (/^p_\d{3,}$/.test(raw)) {
    const entry = globalThis.__pupilElementMap?.[raw];
    if (entry?.el && isVisible(entry.el)) return entry.el;
    // Ref not in map (or element is no longer visible) — fall through to
    // legacy selector/text resolution so the action degrades gracefully.
  }

  // Strip whitespace-delimited inline comments the model sometimes appends to
  // selectors, e.g. `a[data-testid="x"]  # click next page`. Do NOT treat `#id`
  // selectors as comments unless there's whitespace before the `#`.
  raw = raw.replace(/\s+#.*$/, "").trim();
  if (!raw) return null;

  // Normalize common "selector=" / "css=" / "xpath=" prefixes. The model
  // often copies the "selector=..." annotation from page_context verbatim.
  // Treat it as the underlying selector string.
  const prefixMatch = raw.match(/^(selector|css|xpath)\s*=\s*(.+)$/i);
  if (prefixMatch) {
    raw = String(prefixMatch[2] || "").trim();
    if (!raw) return null;
  }

  // ref-json-fix (2026-05-15): model sometimes emits JSON-wrapped targets like
  // {"selector":"X"}, {"ref":"r-77"}, {"target":"X"}. Extract the resolvable
  // field. If only "ref" with no mapping is present, return null so the
  // executor surfaces target_not_found and the model adapts via
  // SELECTOR-ADAPT DISCIPLINE.
  if (raw.startsWith("{") || raw.startsWith("[")) {
    try {
      const parsed = JSON.parse(raw);
      const extracted = parsed.selector || parsed.target || parsed.target_selector || parsed.css || parsed.xpath || "";
      if (extracted) {
        raw = String(extracted).trim();
      } else if (parsed.ref) {
        // Resolve JSON-wrapped ref {"ref":"p_001"} via the element map.
        const refKey = String(parsed.ref).trim();
        const entry = globalThis.__pupilElementMap?.[refKey];
        if (entry?.el && isVisible(entry.el)) return entry.el;
        // Ref unknown or element gone — surface target_not_found so model adapts.
        return null;
      } else if (parsed.text) {
        // Fall through to text-needle search with the text field.
        raw = String(parsed.text).trim();
      } else {
        return null;
      }
    } catch (_err) {
      // Malformed JSON — fall through to legacy selector resolution.
    }
  }

  // Prefix normalization again after JSON extraction (e.g. {"selector":"selector=#id"}).
  const prefixMatch2 = raw.match(/^(selector|css|xpath)\s*=\s*(.+)$/i);
  if (prefixMatch2) {
    raw = String(prefixMatch2[2] || "").trim();
    if (!raw) return null;
  }
  // In case the selector was inside the prefix or JSON wrapper, strip trailing
  // whitespace-delimited comments again.
  raw = raw.replace(/\s+#.*$/, "").trim();
  if (!raw) return null;

  const bySelector = trySelector(raw);
  if (bySelector && isVisible(bySelector)) return bySelector;

  const isXPath = raw.startsWith("/") || raw.startsWith("(/");
  const xpathNeedle = isXPath ? extractTextNeedleFromXPath(raw) : null;
  const needle = (xpathNeedle || raw).toLowerCase();
  const candidates = collectDeep(ACTION_TARGETS, 2000).filter(isVisible);
  return candidates.find((el) => normalizedText(el) === needle)
    || candidates.find((el) => normalizedText(el).includes(needle))
    || null;
}

function parseFillTarget(action) {
  const raw = String(action?.target || "").trim();
  const extras = action?.extras || {};
  if (raw.startsWith("{")) {
    try {
      const parsed = JSON.parse(raw);
      return {
        selector: parsed.selector || parsed.target || "",
        value: parsed.value || parsed.text || "",
        overwrite: Boolean(parsed.overwrite || parsed.force)
      };
    } catch (_err) {
      // Fall through to delimiter parsing.
    }
  }
  const match = raw.match(/^(.*?)\s*(?:=>|:=|::)\s*([\s\S]*)$/);
  if (match) return { selector: match[1].trim(), value: match[2].trim(), overwrite: Boolean(action?.overwrite || extras.overwrite || extras.force) };
  // LAYER 1 (SENSEI HARDENING 2026-05-20): MCP-queued actions carry the
  // value at action.value (top level), not in extras. Read top-level first
  // so {kind:"BROWSER_FILL", target:"...", value:"..."} works without an
  // extras wrapper or a "::"-delimited target string.
  return {
    selector: raw,
    value: action?.value || action?.text || extras.value || extras.text || "",
    overwrite: Boolean(action?.overwrite || extras.overwrite || extras.force),
  };
}

function currentElementValue(el) {
  if (!el) return "";
  if (String(el.getAttribute("type") || "").toLowerCase() === "file") {
    return Array.from(el.files || []).map((file) => file.name).join(", ");
  }
  if (el.isContentEditable) return String(el.textContent || "");
  if ("value" in el) return String(el.value || "");
  return "";
}

function fillValuesDiffer(current, requested) {
  return String(current || "").trim() !== String(requested || "").trim();
}

function renderedLabelFor(el) {
  if (!el) return "";
  const id = el.getAttribute("id");
  const explicitLabel = id ? document.querySelector(`label[for="${cssEscape(id)}"]`) : null;
  const wrappingLabel = el.closest?.("label");
  return safePageText([
    explicitLabel?.innerText,
    wrappingLabel?.innerText,
    el.getAttribute("aria-label"),
    el.getAttribute("title"),
    el.getAttribute("placeholder"),
    el.getAttribute("name")
  ].filter(Boolean).join(" "), 300);
}

function base64ToBytes(base64) {
  const binary = atob(String(base64 || ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function isFileUploadAction(action, parsed) {
  if (action?.extras?.fileUpload?.base64) return true;
  const value = String(parsed?.value || "").trim();
  return /^file:\/\//i.test(value) ||
    ((value.startsWith("/") || value.startsWith("~/")) &&
      /\.(pdf|docx?|odt|rtf|txt|csv|jpe?g|png|webp|gif|zip)$/i.test(value));
}

function findFillElement(parsed, allowHidden = false) {
  const bySelector = trySelector(parsed.selector);
  if (bySelector && (allowHidden || isVisible(bySelector))) return bySelector;
  return findElement(parsed.selector);
}

function setFileInputValue(el, fileUpload) {
  if (!el || String(el.getAttribute("type") || "").toLowerCase() !== "file") {
    return { ok: false, error: "target is not a file input" };
  }
  if (!fileUpload?.base64) return { ok: false, error: "missing file payload" };
  const bytes = base64ToBytes(fileUpload.base64);
  const file = new File([bytes], fileUpload.name || "upload.bin", {
    type: fileUpload.mime || "application/octet-stream",
  });
  const transfer = new DataTransfer();
  transfer.items.add(file);
  el.files = transfer.files;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return {
    ok: true,
    file_name: file.name,
    file_size: file.size,
    mime: file.type || "",
  };
}

function chromeStorageGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

async function uploadAllowlistVerdict(path) {
  const normalizedPath = String(path || "").trim().replace(/[\\/]+$/, "").replace(/\\/g, "/");
  if (!normalizedPath) return { valid: false, reason: "path outside upload allowlist" };
  if (/^~\/(?:Documents|Desktop|Downloads)(?:\/|$)/.test(normalizedPath)) {
    return { valid: true };
  }
  if (/^\/home\/[^/]+\/(?:Documents|Desktop|Downloads)(?:\/|$)/.test(normalizedPath)) {
    return { valid: true };
  }
  if (/^\/Users\/[^/]+\/(?:Documents|Desktop|Downloads)(?:\/|$)/.test(normalizedPath)) {
    return { valid: true };
  }
  const stored = await chromeStorageGet(["allowedUploadDirs"]);
  const allowedDirs = Array.isArray(stored?.allowedUploadDirs) ? stored.allowedUploadDirs : [];
  const ok = allowedDirs
    .map((dir) => String(dir || "").trim().replace(/[\\/]+$/, "").replace(/\\/g, "/"))
    .filter(Boolean)
    .some((dir) => normalizedPath === dir || normalizedPath.startsWith(`${dir}/`));
  return ok ? { valid: true } : { valid: false, reason: "path outside upload allowlist" };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseJsonTarget(action) {
  const raw = String(action?.target || "").trim();
  if (!raw.startsWith("{")) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_err) {
    return {};
  }
}

function parseWaitMs(target, fallback = 1000, max = 10000) {
  const raw = String(target || "").trim();
  const match = raw.match(/\d+/);
  const parsed = match ? Number(match[0]) : fallback;
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(0, Math.min(parsed, max));
}

function normalizeSearchText(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

function cleanDriveName(value) {
  return safePageText(
    String(value || "")
      .replace(/\bMore actions\b.*$/i, "")
      .replace(/\bShared folder\b/ig, "folder")
      .replace(/\s+/g, " ")
      .trim(),
    180
  );
}

function firstUsefulLine(value) {
  const lines = String(value || "")
    .split(/\n+/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean);
  return lines[0] || "";
}

function driveItemKind(raw) {
  const text = normalizeSearchText(raw);
  if (/\bfolder\b/.test(text)) return "folder";
  if (/\b(pdf|document|spreadsheet|presentation|image|jpeg|jpg|png|docx|xlsx|zip)\b/.test(text)) return "file";
  return "unknown";
}

function driveItemCandidates(limit = 80) {
  const seen = new Map();
  const ariaRows = collectDeep(
    "table[aria-label*='Item List'] tr,[role='row'][aria-label],[role='row'],[role='treeitem']",
    limit * 4
  ).filter(isVisible);
  const fallbackSelectors = [
    "table[aria-label*='Item List'] tr",
    "[role='row']",
    "[role='gridcell'][aria-label]",
    "[role='gridcell']",
    "[role='listitem'][aria-label]",
    "[role='treeitem']",
    "[data-target='doc'][aria-label]"
  ].join(",");
  const candidates = (ariaRows.length ? ariaRows : collectDeep(fallbackSelectors, limit * 4)).filter(isVisible);

  for (const el of candidates) {
    const primary = el.matches?.("[aria-label]")
      ? el
      : el.querySelector?.("[aria-label], a[href], button, [role='gridcell'], td, div, span") || el;
    const aria = primary?.getAttribute?.("aria-label") || el.getAttribute("aria-label") || "";
    const text = primary?.innerText || primary?.textContent || el.innerText || el.textContent || "";
    const raw = [aria, firstUsefulLine(text)].filter(Boolean).join(" ");
    const name = cleanDriveName(aria || firstUsefulLine(text));
    const normalized = normalizeSearchText(name || raw);
    if (!normalized || normalized.length < 2) continue;
    if (/^(new|search|settings|help|support|google apps|account|list view|grid view)$/.test(normalized)) continue;

    const selector = safeSelectorFor(primary || el);
    const selected = el.getAttribute("aria-selected") === "true"
      || el.getAttribute("aria-current") === "true"
      || primary?.getAttribute?.("aria-selected") === "true"
      || primary?.getAttribute?.("aria-current") === "true";
    const kind = driveItemKind(raw);
    const rec = {
      name: name || safePageText(firstUsefulLine(text) || aria, 180),
      kind,
      is_folder: kind === "folder",
      selected,
      role: primary?.getAttribute?.("role") || el.getAttribute("role") || elementRole(primary || el),
      selector,
      aria_label: safePageText(aria, 260),
      text: safePageText(el.innerText || el.textContent || text, 500),
      href: primary?.href || el.href || el.querySelector?.("a[href]")?.href || ""
    };
    const key = `${normalized}|${rec.kind}|${rec.selector}`;
    if (!seen.has(key)) seen.set(key, rec);
    if (seen.size >= limit) break;
  }

  return Array.from(seen.values());
}

function driveEmptyReason(text) {
  const haystack = String(text || "");
  const patterns = [
    /Drop files here/i,
    /use the ['"]?New['"]? button/i,
    /This folder is empty/i,
    /No files or folders/i,
    /No items/i,
    /No results/i
  ];
  const match = patterns.find((pattern) => pattern.test(haystack));
  return match ? match.source.replace(/\\/g, "") : "";
}

function driveState() {
  const text = visibleText(9000);
  const items = driveItemCandidates();
  const selected = items.filter((item) => item.selected);
  const emptyReason = driveEmptyReason(text);
  const isDrive = /(^|\.)drive\.google\.com$/i.test(location.hostname);
  const summary = emptyReason
    ? `Drive page appears empty: ${emptyReason}.`
    : `Drive page has ${items.length} visible item candidate${items.length === 1 ? "" : "s"}.`;
  return {
    is_drive: isDrive,
    url: safePageText(location.href, 2000),
    title: safePageText(document.title || "", 300),
    empty: Boolean(emptyReason),
    empty_reason: emptyReason,
    summary,
    items,
    selected_items: selected,
    visible_text: text
  };
}

function genericListState() {
  const selectors = [
    "li",
    "[role='row']",
    "[role='listitem']",
    "[role='gridcell']",
    "tr",
    "article"
  ].join(",");
  const items = Array.from(document.querySelectorAll(selectors))
    .filter(isVisible)
    .slice(0, 80)
    .map((el) => ({
      text: elementText(el, 500),
      selector: safeSelectorFor(el),
      role: el.getAttribute("role") || elementRole(el)
    }))
    .filter((item) => item.text);
  return {
    url: safePageText(location.href, 2000),
    title: safePageText(document.title || "", 300),
    summary: `Page has ${items.length} visible list item candidate${items.length === 1 ? "" : "s"}.`,
    items
  };
}

function findTextOnPage(target) {
  const needle = normalizeSearchText(target);
  const matches = [];
  if (!needle) return matches;
  const candidates = collectDeep("*", 1800)
    .filter(isVisible)
    .slice(0, 1200);
  for (const el of candidates) {
    const text = elementText(el, 500);
    if (!text || !normalizeSearchText(text).includes(needle)) continue;
    matches.push({
      text,
      selector: safeSelectorFor(el),
      role: el.getAttribute("role") || elementRole(el)
    });
    if (matches.length >= 30) break;
  }
  return matches;
}

function parseScrollTarget(target) {
  const raw = String(target || "down").trim().toLowerCase();
  const amountMatch = raw.match(/-?\d+/);
  const amount = amountMatch ? Number(amountMatch[0]) : Math.round(window.innerHeight * 0.85);
  if (raw.includes("top")) return { top: 0, left: window.scrollX, behavior: "smooth" };
  if (raw.includes("bottom")) return { top: document.documentElement.scrollHeight, left: window.scrollX, behavior: "smooth" };
  if (raw.includes("up")) return { top: window.scrollY - Math.abs(amount), left: window.scrollX, behavior: "smooth" };
  if (raw.includes("left")) return { top: window.scrollY, left: window.scrollX - Math.abs(amount), behavior: "smooth" };
  if (raw.includes("right")) return { top: window.scrollY, left: window.scrollX + Math.abs(amount), behavior: "smooth" };
  return { top: window.scrollY + Math.abs(amount), left: window.scrollX, behavior: "smooth" };
}

function nativeValueSetterFor(el) {
  const prototypes = [];
  if (el instanceof HTMLInputElement) prototypes.push(HTMLInputElement.prototype);
  if (el instanceof HTMLTextAreaElement) prototypes.push(HTMLTextAreaElement.prototype);
  if (el instanceof HTMLSelectElement) prototypes.push(HTMLSelectElement.prototype);
  prototypes.push(Object.getPrototypeOf(el));
  for (const proto of prototypes) {
    const descriptor = proto && Object.getOwnPropertyDescriptor(proto, "value");
    if (descriptor?.set) return descriptor.set;
  }
  return null;
}

function dispatchFormEvents(el, value) {
  try {
    el.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      cancelable: true,
      inputType: "insertReplacementText",
      data: String(value || "")
    }));
  } catch (_err) {
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function setElementValue(el, value) {
  if (!el) return;
  el.focus();
  if (el instanceof HTMLInputElement) {
    const type = String(el.getAttribute("type") || "text").toLowerCase();
    if (type === "checkbox") {
      const desired = /^(1|true|yes|y|on|checked)$/i.test(String(value || "").trim());
      el.checked = desired;
      dispatchFormEvents(el, desired ? "checked" : "");
      return;
    }
    if (type === "radio") {
      const targetValue = normalizeSearchText(value);
      const group = el.name ? Array.from(document.querySelectorAll(`input[type="radio"][name="${cssEscape(el.name)}"]`)) : [el];
      const pick = group.find((candidate) => {
        const candidateLabel = renderedLabelFor(candidate);
        const candidateValue = String(candidate.value || "");
        return normalizeSearchText(candidateValue) === targetValue || normalizeSearchText(candidateLabel) === targetValue;
      }) || el;
      try { pick.click(); } catch (_err) {}
      pick.checked = true;
      dispatchFormEvents(pick, pick.value || "checked");
      return;
    }
  }
  if (el.isContentEditable) {
    el.textContent = value;
    dispatchFormEvents(el, value);
    return;
  }
  if (el instanceof HTMLSelectElement) {
    const requested = String(value || "");
    const option = Array.from(el.options || []).find((opt) =>
      opt.value === requested || normalizeSearchText(opt.textContent) === normalizeSearchText(requested)
    );
    if (option) el.value = option.value;
    else el.value = requested;
    dispatchFormEvents(el, value);
    return;
  }
  if ("value" in el) {
    const setter = nativeValueSetterFor(el);
    if (setter) setter.call(el, value);
    else el.value = value;
  } else {
    el.textContent = value;
  }
  dispatchFormEvents(el, value);
}

// Profile-matched form fill (powers BROWSER_FILL_FORM). Walks the visible form
// fields under scopeEl, matches each against the saved profile by name/id/
// placeholder/<label>/aria-label, and fills via the existing setElementValue
// path so React/Angular/Vue controlled inputs notice the change.
const _PROFILE_FIELD_PATTERNS = [
  { key: "email", test: (id, el) => /\bemail\b/.test(id) || el.type === "email" },
  { key: "phone", test: (id, el) => /\b(phone|mobile|cell|telephone|tel)\b/.test(id) || el.type === "tel" },
  { key: "full_name", test: (id) => /\b(full[\s-]?name|legal[\s-]?name|your[\s-]?name|applicant[\s-]?name|name)\b/.test(id) && !/\b(company|business|file|user|first|last|middle|maiden)\b/.test(id) },
  { key: "city", test: (id) => /\b(city|town|locality)\b/.test(id) },
  { key: "recent_job", test: (id, el) => /\b(most[\s-]?recent|current|last|previous)[\s-]?(job|employer|position|title|company|role)\b/.test(id) || (el.tagName === "TEXTAREA" && /\b(experience|work[\s-]?history|employment)\b/.test(id)) },
];

const _PROFILE_NESTED_PATTERNS = [
  { path: "demographics.race", test: (id) => /\brace|ethnicity|african american|black\b/.test(id) },
  { path: "demographics.hispanic_or_latino", test: (id) => /\bhispanic|latino\b/.test(id) },
  { path: "demographics.gender", test: (id) => /\bgender|sex\b/.test(id) },
  { path: "demographics.veteran_status", test: (id) => /\bveteran|protected veteran|self[-\s]?identify\b/.test(id) },
  { path: "demographics.disability_status", test: (id) => /\bdisability|disabled|disclosure|self[-\s]?identify\b/.test(id) },
  { path: "work_authorization.authorized_us", test: (id) => /\bauthorized\b.*\b(us|u\.s\.|united states)\b|\beligible to work\b/.test(id) },
  { path: "work_authorization.authorized_us", test: (id) => /\bus citizen|u\.s\. citizen|citizenship\b/.test(id) },
  { path: "work_authorization.sponsorship_needed", test: (id) => /\bsponsorship|sponsor|visa\b/.test(id) },
  { path: "work_authorization.eighteen_or_older", test: (id) => /\b18\b|\beighteen\b|\bolder\b/.test(id) },
  { path: "screener_defaults.drivers_license", test: (id) => /\bdriver'?s?\s*license|valid license\b/.test(id) },
  { path: "screener_defaults.epa_type_ii", test: (id) => /\bepa\b.*\btype\b.*\bii\b|\bepa certification\b/.test(id) },
  { path: "screener_defaults.reliable_transportation", test: (id) => /\breliable transportation|transportation\b/.test(id) },
  { path: "screener_defaults.sms_text_consent", test: (id) => /\bsms|text message|text consent|receive texts?\b/.test(id) },
  { path: "screener_defaults.background_check_consent", test: (id) => /\bbackground check\b/.test(id) },
  { path: "screener_defaults.drug_test_willing", test: (id) => /\bdrug test|drug screening\b/.test(id) },
  { path: "screener_defaults.highest_education", test: (id) => /\bhighest level of education|highest education|education|school\b/.test(id) },
  { path: "screener_defaults.years_hvac_experience", test: (id) => /\byears?\b.*\bhvac\b.*\bexperience\b|\bhvac\b.*\byears?\b/.test(id) },
  { path: "screener_defaults.how_did_you_hear", test: (id) => /\bhow did you hear|source|job board|internet\b/.test(id) },
  { path: "screener_defaults.willing_to_relocate_local", test: (id) => /\brelocate|relocation\b/.test(id) },
  { path: "screener_defaults.salary_expectation", test: (id) => /\bsalary|compensation|pay expectation\b/.test(id) },
];

const _PROFILE_DEFAULT_ANSWERS = {
  "demographics.race": "Black or African American",
  "demographics.hispanic_or_latino": "No",
  "demographics.gender": "Male",
  "demographics.veteran_status": "I don't wish to answer",
  "demographics.disability_status": "I don't wish to answer",
  "work_authorization.authorized_us": "Yes",
  "work_authorization.sponsorship_needed": "No",
  "work_authorization.eighteen_or_older": "Yes",
  "screener_defaults.drivers_license": "Yes",
  "screener_defaults.epa_type_ii": "Yes",
  "screener_defaults.reliable_transportation": "Yes",
  "screener_defaults.sms_text_consent": "Yes",
  "screener_defaults.background_check_consent": "Yes",
  "screener_defaults.drug_test_willing": "Yes",
  "screener_defaults.highest_education": "High school diploma or GED",
  "screener_defaults.years_hvac_experience": "8",
  "screener_defaults.how_did_you_hear": "Internet",
  "screener_defaults.willing_to_relocate_local": "Yes",
};

const _HARD_SKIP_PATTERNS = [
  { category: "password", re: /\bpassword|passcode|pin\b/ },
  { category: "ssn", re: /\bssn|social[-\s]?security\b/ },
  { category: "dob", re: /\bdob|date of birth|birthdate\b/ },
  { category: "government_id", re: /\bgovernment id|national id|state id\b/ },
  { category: "drivers_license_number", re: /\bdriver'?s? license number|dl number\b/ },
  { category: "felony_freetext", re: /\bfelony|conviction|charge\b/ },
  { category: "felony_freetext", re: /\bexplain|describe your background\b/ },
  { category: "banking", re: /\bbanking|bank account|routing|account number|ach\b/ },
  { category: "card", re: /\bcard number|credit card|debit card|cvv|cvc\b/ },
  { category: "passport", re: /\bpassport\b/ },
  { category: "tos_consent", re: /\bterms of service|terms and conditions|privacy policy\b/ },
];

// Sensitive-field gate (post-emit deterministic safety property).
// Hard-no categories: BROWSER_FILL_FORM will NEVER fill an element matching
// these patterns, regardless of what's in the profile. The dispatcher returns
// a `skipped_sensitive` array with full citation so the next turn can emit
// NEEDS_INPUT: <category> :: <label> instead. Two-tier signal logic:
//   • DEFINITIVE (1-signal trigger): el.type=password / autocomplete tokens.
//   • STACKED (2+-signal trigger): name|id regex + label substring + url path,
//     where url-path is a TIEBREAKER, never a primary signal alone.
//   • FORM-ACTION FALLBACK: when site authors randomize input names for
//     anti-bot reasons, the form's action URL (/login, /checkout, etc.) is
//     promoted from tiebreaker to qualifying signal.
const _SENSITIVE_CATEGORY_PATTERNS = {
  password: {
    type_definitive: (el) => String(el.type || "").toLowerCase() === "password",
    autocomplete_definitive: (el) => /^(current-password|new-password|one-time-code)$/i.test(String(el.autocomplete || "")),
    name_id: (id) => /\bpass(?:word|wd|code)?\b|\bpasscode\b/.test(id),
    label: (id) => /\bpassword\b|\bpasscode\b|\bpin\b/.test(id),
    url_path: (u) => /\/(login|signin|sign-in|auth|password|reset|account)\b/.test(u),
  },
  ssn: {
    autocomplete_definitive: (el) => /^(off|new-password)$/i.test("") && false,  // SSN has no canonical autocomplete; rely on stacked signals
    name_id: (id) => /\b(ssn|social[-_\s]?security|tax[-_\s]?id|sin|nin)\b/.test(id),
    label: (id) => /\b(social\s*security|ssn|sin|tax\s*identification|tax\s*id|national\s*insurance)\b/.test(id),
    url_path: (u) => /\/(verify|identity|background|tax|onboard)\b/.test(u),
  },
  cc_number: {
    autocomplete_definitive: (el) => /^cc-number$/i.test(String(el.autocomplete || "")),
    name_id: (id) => /\b(cc|card|credit)[-_\s]?(num|number)\b|^card$|\bcardnumber\b/.test(id),
    label: (id) => /\b(card\s*number|credit\s*card\s*number|cc\s*number|debit\s*card)\b/.test(id),
    url_path: (u) => /\/(checkout|payment|billing|pay|order)\b/.test(u),
  },
  cvv: {
    autocomplete_definitive: (el) => /^cc-csc$/i.test(String(el.autocomplete || "")),
    name_id: (id) => /\b(cvv|cvc|csc|security[-_\s]?code|verification[-_\s]?code)\b/.test(id),
    label: (id) => /\b(cvv|cvc|csc|security\s*code|card\s*verification)\b/.test(id),
    url_path: (u) => /\/(checkout|payment|billing|pay|order)\b/.test(u),
  },
  routing_number: {
    name_id: (id) => /\brouting[-_\s]?(num|number)?\b|\baba\b/.test(id),
    label: (id) => /\brouting\s*number\b|\baba\s*routing\b/.test(id),
    url_path: (u) => /\/(bank|ach|transfer|deposit)\b/.test(u),
  },
  account_number: {
    name_id: (id) => /\b(bank|checking|savings)?[-_\s]?account[-_\s]?(num|number)\b/.test(id),
    label: (id) => /\b(bank\s*account\s*number|account\s*number)\b/.test(id),
    url_path: (u) => /\/(bank|ach|transfer|deposit)\b/.test(u),
  },
};

const _SENSITIVE_PATH_FALLBACK_RE = /\/(login|signin|sign-in|auth|checkout|payment|billing|bank|ach|onboard|tax|verify)\b/;

function _detectSensitiveField(el, urlPath) {
  const idText = _identifierStringForElement(el);
  let formAction = "";
  try { formAction = String(el.closest && el.closest("form") && el.closest("form").action || ""); } catch (_e) {}
  const lowFormAction = formAction.toLowerCase();
  for (const [category, patterns] of Object.entries(_SENSITIVE_CATEGORY_PATTERNS)) {
    const signals = [];
    if (patterns.type_definitive && patterns.type_definitive(el)) signals.push("type");
    if (patterns.autocomplete_definitive && patterns.autocomplete_definitive(el)) signals.push("autocomplete");
    // Definitive 1-signal trigger
    if (signals.length >= 1) return { category, signals, tier: "definitive" };
    if (patterns.name_id && patterns.name_id(idText)) signals.push("name_id");
    if (patterns.label && patterns.label(idText)) signals.push("label");
    if (urlPath && patterns.url_path && patterns.url_path(urlPath)) signals.push("url_path");
    // Stacked 2-signal trigger
    if (signals.length >= 2) return { category, signals, tier: "stacked" };
    // Form-action fallback: anti-bot randomized names get rescued when the
    // form action declares /login or /checkout (1 in-pattern signal + form_action = qualifies).
    if (signals.length >= 1 && _SENSITIVE_PATH_FALLBACK_RE.test(lowFormAction)) {
      signals.push("form_action");
      return { category, signals, tier: "form_action_fallback" };
    }
  }
  return null;
}

// Captcha detection (pre-check before form-walk and re-check after each fill).
// Returns null when no captcha visible, else a citation the result envelope
// can carry so the next turn's model emits NEEDS_INPUT: captcha.
const _CAPTCHA_SELECTORS = [
  'iframe[src*="recaptcha"]',
  'iframe[src*="hcaptcha"]',
  'iframe[src*="turnstile"]',
  'iframe[src*="challenges.cloudflare.com"]',
  'div.cf-turnstile',
  'div.g-recaptcha',
  'div.h-captcha',
  'div.cf-challenge',
  '#challenge-form',
  '[data-sitekey]:not(iframe)',
];

function _detectCaptchaOnPage(scope) {
  const root = scope instanceof Element ? scope : document;
  for (const sel of _CAPTCHA_SELECTORS) {
    try {
      const el = root.querySelector(sel);
      if (!el) continue;
      // Visible-only — `data-sitekey` widgets can exist in shadow roots and not yet be rendered.
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0 && !el.querySelector('iframe')) continue;
      let captchaType = "unknown";
      if (/recaptcha/i.test(sel)) captchaType = "recaptcha";
      else if (/hcaptcha/i.test(sel)) captchaType = "hcaptcha";
      else if (/turnstile|cloudflare/i.test(sel)) captchaType = "turnstile";
      else if (/cf-challenge|challenge-form/i.test(sel)) captchaType = "cf-challenge";
      else if (/sitekey/i.test(sel)) captchaType = "sitekey-widget";
      return { selector: sel, captcha_type: captchaType, sitekey: el.getAttribute('data-sitekey') || null };
    } catch (_e) {}
  }
  return null;
}

function _identifierStringForElement(el) {
  const parts = [];
  if (el.name) parts.push(el.name);
  if (el.id) parts.push(el.id);
  if (el.placeholder) parts.push(el.placeholder);
  const ariaLabel = el.getAttribute("aria-label");
  if (ariaLabel) parts.push(ariaLabel);
  try {
    const label = renderedLabelFor(el);
    if (label) parts.push(label);
  } catch (_e) {}
  return parts.join(" ").toLowerCase().replace(/[^a-z0-9\s_-]/g, " ").replace(/\s+/g, " ").trim();
}

function _resolveProfileKey(el) {
  const id = _identifierStringForElement(el);
  if (!id) return null;
  for (const pattern of _PROFILE_FIELD_PATTERNS) {
    if (pattern.test(id, el)) return pattern.key;
  }
  return null;
}

function _profileValueByPath(profile, path) {
  let cur = profile;
  for (const part of String(path || "").split(".")) {
    if (!cur || typeof cur !== "object") return "";
    cur = cur[part];
  }
  const raw = cur == null ? "" : String(cur);
  if (raw.trim()) return raw;
  return String(_PROFILE_DEFAULT_ANSWERS[path] || "");
}

function _hardSkipForElement(el) {
  const categoryByType = String(el?.type || "").toLowerCase();
  if (categoryByType === "password") {
    return { category: "password", signals: ["type=password"] };
  }
  const idText = _identifierStringForElement(el);
  if (!idText) return null;
  for (const pattern of _HARD_SKIP_PATTERNS) {
    if (!pattern.re.test(idText)) continue;
    if (pattern.category === "felony_freetext") {
      const tag = String(el.tagName || "").toLowerCase();
      const role = String(el.getAttribute("role") || "").toLowerCase();
      if (!(tag === "textarea" || role === "textbox" || el.isContentEditable || String(el.type || "").toLowerCase() === "text")) {
        continue;
      }
    }
    return { category: pattern.category, signals: [pattern.re.source] };
  }
  if (/\b(background check)\b/.test(idText) && /\b(will pass|i will pass|confirm i will pass)\b/.test(idText)) {
    return { category: "background_check_pass_assertion", signals: ["will_pass_assertion"] };
  }
  return null;
}

function _resolveProfileValue(el, profile) {
  const directKey = _resolveProfileKey(el);
  if (directKey) {
    const direct = profile && Object.prototype.hasOwnProperty.call(profile, directKey) ? profile[directKey] : "";
    if (direct != null && String(direct).trim()) {
      return {
        value: String(direct),
        source: directKey,
      };
    }
  }
  const id = _identifierStringForElement(el);
  if (!id) return null;
  for (const pattern of _PROFILE_NESTED_PATTERNS) {
    if (!pattern.test(id, el)) continue;
    const v = _profileValueByPath(profile, pattern.path);
    if (String(v).trim()) {
      return {
        value: String(v),
        source: pattern.path,
      };
    }
  }
  return null;
}

// Track A — Layer 3: Contextual Field-Length Boundaries (2026-05-23)
// Extracts DOM-declared constraints so the LLM receives hard rules alongside
// each field — stops "too much / too little text" failures.
// Used by: interactiveElements() (LLM sees constraints on every field),
//          fillFormByProfileMatch() (skipped_no_match entries get constraints
//          so the LLM knows bounds when generating custom-question answers).
function _extractFieldConstraints(el) {
  const c = {};
  if (!el || typeof el.getAttribute !== "function") return c;
  const ml  = el.getAttribute("maxlength")   || el.getAttribute("maxLength");
  const mnl = el.getAttribute("minlength")   || el.getAttribute("minLength");
  const pat = el.getAttribute("pattern");
  const tp  = el.getAttribute("type");
  const mn  = el.getAttribute("min");
  const mx  = el.getAttribute("max");
  const stp = el.getAttribute("step");
  const ac  = el.getAttribute("autocomplete");
  const req = el.required || String(el.getAttribute("aria-required") || "").toLowerCase() === "true";
  if (ml  !== null && ml  !== "") c.maxlength  = parseInt(ml,  10);
  if (mnl !== null && mnl !== "") c.minlength  = parseInt(mnl, 10);
  if (pat !== null && pat !== "") c.pattern    = pat;
  // Only include type when it adds signal beyond the default text/textarea
  if (tp  !== null && tp  !== "" && tp !== "text") c.type = tp;
  if (mn  !== null && mn  !== "") c.min  = mn;
  if (mx  !== null && mx  !== "") c.max  = mx;
  if (stp !== null && stp !== "") c.step = stp;
  // autocomplete hints help the model choose the right field-fill strategy
  if (ac  !== null && ac  !== "" && ac !== "on" && ac !== "off") c.autocomplete = ac;
  if (req) c.required = true;
  return c;
}

function fillFormByProfileMatch(scopeEl, profile) {
  const scope = scopeEl instanceof Element ? scopeEl : document;
  const urlPath = (() => { try { return window.location.pathname || ""; } catch (_e) { return ""; } })();
  const refLookup = elementRefLookup();
  const unmatchedRequiredByRef = new Map();
  // Pre-check: captcha visible → refuse the entire walk. The next-turn model
  // owes a NEEDS_INPUT: captcha emission; filling under a captcha gate is the
  // exact failure pattern (anti-bot drops the application).
  const captcha_pre = _detectCaptchaOnPage(scope);
  if (captcha_pre) {
    return {
      filled: [],
      filled_count: 0,
      skipped_no_match: [],
      skipped_sensitive: [],
      unmatched_required: [],
      fields: [],
      captcha_present: captcha_pre,
    };
  }
  const selector = "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([disabled]):not([readonly]), textarea:not([disabled]):not([readonly]), select:not([disabled])";
  const candidates = scope.querySelectorAll(selector);
  const filled = [];
  const skipped_no_match = [];
  const skipped_sensitive = [];
  const legacyFields = [];
  const addUnmatchedRequired = (ref, label) => {
    if (!ref) return;
    if (!unmatchedRequiredByRef.has(ref)) {
      unmatchedRequiredByRef.set(ref, {
        ref,
        label: safePageText(label || "", 240),
      });
    }
  };
  // Password-confirm pairing — when the walk sees two consecutive password-
  // category sensitives in the same form, the second is annotated as a
  // password_confirm pair so the model emits ONE NEEDS_INPUT for the pair,
  // not two for independent fields. Same pattern catches new-password +
  // confirm-new-password on change-password flows.
  let last_password_entry = null;
  for (const el of candidates) {
    try {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      const ref = refLookup.get(el) || `ref_${filled.length + skipped_no_match.length + skipped_sensitive.length + 1}`;
      const labelText = renderedLabelFor(el) || el.placeholder || el.name || el.id || el.getAttribute("aria-label") || "";
      const required = Boolean(el.required || String(el.getAttribute("aria-required") || "").toLowerCase() === "true");
      const hardSkip = _hardSkipForElement(el);
      if (hardSkip) {
        skipped_sensitive.push({
          ref,
          category: hardSkip.category,
          signals: hardSkip.signals || [],
          label: safePageText(labelText, 240),
        });
        if (required) addUnmatchedRequired(ref, labelText);
        continue;
      }
      // Sensitive-field gate fires BEFORE profile-key match. The model never
      // gets to invent a password value through this code path.
      const sensitive = _detectSensitiveField(el, urlPath);
      if (sensitive) {
        const entry = {
          ref,
          category: sensitive.category,
          signals: sensitive.signals,
          label: safePageText(labelText, 240),
        };
        // If this is a password and we already saw one in this walk, annotate
        // as the confirm half of the pair. Model emits one NEEDS_INPUT, not two.
        if (sensitive.category === "password" && last_password_entry) {
          entry.category = "password_confirm";
        } else if (sensitive.category === "password") {
          last_password_entry = entry;
        }
        skipped_sensitive.push(entry);
        if (required) addUnmatchedRequired(ref, labelText);
        continue;
      }
      const profileHit = _resolveProfileValue(el, profile);
      if (!profileHit) {
        // Layer 3: include field constraints so the LLM knows hard limits
        // when generating text for custom questions (essay fields, etc.)
        const fieldCons = _extractFieldConstraints(el);
        const noMatchEntry = { ref, label: safePageText(labelText, 240) };
        if (Object.keys(fieldCons).length) noMatchEntry.constraints = fieldCons;
        skipped_no_match.push(noMatchEntry);
        if (required) addUnmatchedRequired(ref, labelText);
        continue;
      }
      const current = ("value" in el ? String(el.value || "") : "").trim();
      if (current && current.toLowerCase() === String(profileHit.value).toLowerCase()) {
        filled.push({ ref, value: safePageText(current, 300) });
        legacyFields.push({ key: profileHit.source, name: el.name || el.id || "", action: "preserved" });
        continue;
      }
      if (current && current.toLowerCase() !== String(profileHit.value).toLowerCase()) {
        skipped_no_match.push({ ref, label: safePageText(labelText, 240) });
        if (required) addUnmatchedRequired(ref, labelText);
        continue;
      }
      setElementValue(el, String(profileHit.value));
      filled.push({ ref, value: safePageText(String(profileHit.value), 300) });
      legacyFields.push({ key: profileHit.source, name: el.name || el.id || "", action: "filled" });
      // Mid-walk captcha re-check (lazy-loaded anti-bot drops captcha after
      // first few fills). Bail to the same shape as pre-check on detection.
      const captcha_mid = _detectCaptchaOnPage(scope);
      if (captcha_mid) {
        return {
          filled,
          filled_count: filled.length,
          skipped_no_match,
          skipped_sensitive,
          unmatched_required: Array.from(unmatchedRequiredByRef.values()),
          fields: legacyFields,
          captcha_present: captcha_mid,
          captcha_phase: "mid-walk",
        };
      }
    } catch (_e) {
      continue;
    }
  }
  return {
    filled,
    filled_count: filled.length,
    skipped_no_match,
    skipped_sensitive,
    unmatched_required: Array.from(unmatchedRequiredByRef.values()),
    fields: legacyFields,
    captcha_present: null,
  };
}

// Cover-letter merge helper: replaces {{key}} / [key] / <key> with profile
// values. Used by BROWSER_FILL_FORM for textareas that match recent_job +
// contain a template, and exported for any future template-style fills.
function interpolateTemplate(text, profile) {
  if (!text || typeof text !== "string" || !profile) return text;
  return text.replace(/(?:\{\{|\[|<)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\}\}|\]|>)/g, (match, key) => {
    if (Object.prototype.hasOwnProperty.call(profile, key)) {
      return String(profile[key] || "");
    }
    return match;
  });
}

async function fetchProfileFromRouter(action) {
  const base = _routerBaseFromAction(action);
  const paths = ["/profile", "/apply_profile"];
  for (const path of paths) {
    try {
      const resp = await fetch(`${base}${path}`, { method: "GET", mode: "cors", credentials: "omit" });
      if (!resp.ok) continue;
      const data = await resp.json();
      if (!data || typeof data !== "object") continue;
      if (data.profile && typeof data.profile === "object") return data.profile;
      if (data.apply_profile && typeof data.apply_profile === "object") return data.apply_profile;
      if (data.ready === true && data.profile && typeof data.profile === "object") return data.profile;
      const looksProfile = data.full_name || data.email || data.demographics || data.work_authorization || data.screener_defaults;
      if (looksProfile) return data;
    } catch (_err) {
      continue;
    }
  }
  return null;
}

function _pauseState() {
  return globalThis.__SENSEI_FIRST_SUBMIT_PAUSE_STATE__;
}

function _shouldResumeSubmit(action) {
  const extras = action?.extras && typeof action.extras === "object" ? action.extras : {};
  const command = String(
    action?.command
    || extras.command
    || action?.resume_command
    || extras.resume_command
    || ""
  ).toLowerCase();
  if (command === "resume_click") return true;
  if (extras.resume === true || action?.resume === true) return true;
  return false;
}

function _isAbortCommand(action) {
  const extras = action?.extras && typeof action.extras === "object" ? action.extras : {};
  const command = String(action?.command || extras.command || "").toLowerCase();
  return command === "abort";
}

async function _resumePendingSubmit(resumeToken = "") {
  const pauseState = _pauseState();
  const pending = pauseState.pending_submit;
  if (!pending) return { ok: false, error: "no pending submit" };
  if (resumeToken && pending.resume_token && String(resumeToken) !== String(pending.resume_token)) {
    return { ok: false, error: "resume token mismatch" };
  }
  const target = pending.selector;
  const el = findElement(target);
  if (!el) return { ok: false, error: "pending submit target not found", target };
  try { _mirrorMoveGhost(el); } catch (_err) {}
  el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
  el.click();
  await waitForPageStable(350, 1800);
  pauseState.first_app_pause_armed = false;
  pauseState.pending_submit = null;
  return { ok: true, resumed: true, target };
}

// In-page visual mirror (product spec #6): red frame around the tab when an
// action is in flight, ghost cursor at the target coordinates before click
// dispatch, current-step text overlay in the corner. Drawn via injected
// <div>s with extreme z-index and pointer-events:none so the operator's real
// cursor still works. Does NOT use coordinates from action.target directly;
// resolves the target element via DOM and reads its center rect — survives
// page reflow between emit and dispatch.
const _SENSEI_MIRROR_ROOT_ID = "__sensei_mirror_root__";

function _ensureMirrorRoot() {
  let root = document.getElementById(_SENSEI_MIRROR_ROOT_ID);
  if (root) return root;
  root = document.createElement("div");
  root.id = _SENSEI_MIRROR_ROOT_ID;
  root.style.cssText = "position:fixed;top:0;left:0;width:0;height:0;pointer-events:none;z-index:2147483646;";
  // Red border around the viewport.
  const frame = document.createElement("div");
  frame.id = "__sensei_mirror_frame__";
  frame.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;border:3px solid #c7761a;border-radius:4px;pointer-events:none;box-sizing:border-box;opacity:0;transition:opacity 180ms ease;";
  root.appendChild(frame);
  // Ghost cursor (chevron-style triangle so it doesn't look like the OS arrow).
  const ghost = document.createElement("div");
  ghost.id = "__sensei_mirror_ghost__";
  ghost.style.cssText = "position:fixed;top:-32px;left:-32px;width:24px;height:24px;pointer-events:none;opacity:0;transition:top 220ms ease,left 220ms ease,opacity 120ms ease;";
  ghost.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="#c7761a" stroke="#fff" stroke-width="0.8"><path d="M2 2 L2 12 L5 9 L7.5 14 L9.5 13 L7 8 L11 8 Z"/></svg>';
  root.appendChild(ghost);
  // Step-text strip pinned to the top-right (out of the way of typical
  // application forms which sit center-left).
  const strip = document.createElement("div");
  strip.id = "__sensei_mirror_step__";
  strip.style.cssText = "position:fixed;top:8px;right:8px;max-width:380px;padding:6px 10px;background:rgba(15,18,22,0.92);color:#e9d6b5;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;line-height:1.35;border:1px solid #c7761a;border-radius:4px;pointer-events:none;opacity:0;transition:opacity 180ms ease;white-space:pre-wrap;word-break:break-word;";
  root.appendChild(strip);
  document.documentElement.appendChild(root);
  return root;
}

let _mirrorIdleTimer = null;

function _mirrorShowStep(text) {
  try {
    _ensureMirrorRoot();
    const frame = document.getElementById("__sensei_mirror_frame__");
    const strip = document.getElementById("__sensei_mirror_step__");
    if (frame) frame.style.opacity = "1";
    if (strip) { strip.textContent = String(text || "").slice(0, 280); strip.style.opacity = "1"; }
    // Auto-idle 8s after the last action. New actions reset the timer.
    if (_mirrorIdleTimer) clearTimeout(_mirrorIdleTimer);
    _mirrorIdleTimer = setTimeout(() => { _mirrorIdle(); }, 8000);
  } catch (_e) {}
}

function _mirrorMoveGhost(targetEl) {
  try {
    _ensureMirrorRoot();
    const ghost = document.getElementById("__sensei_mirror_ghost__");
    if (!ghost) return;
    if (!targetEl || !(targetEl.getBoundingClientRect)) {
      ghost.style.opacity = "0";
      return;
    }
    const rect = targetEl.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) { ghost.style.opacity = "0"; return; }
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    ghost.style.left = (cx - 12) + "px";
    ghost.style.top = (cy - 12) + "px";
    ghost.style.opacity = "1";
  } catch (_e) {}
}

function _mirrorIdle() {
  try {
    const frame = document.getElementById("__sensei_mirror_frame__");
    const strip = document.getElementById("__sensei_mirror_step__");
    const ghost = document.getElementById("__sensei_mirror_ghost__");
    if (frame) frame.style.opacity = "0";
    if (strip) strip.style.opacity = "0";
    if (ghost) ghost.style.opacity = "0";
  } catch (_e) {}
}

function _mirrorDescribeAction(action) {
  const kind = String(action?.kind || "").toUpperCase();
  const target = String(action?.target || "").slice(0, 160);
  if (!kind) return "";
  if (!target) return `▶ ${kind}`;
  return `▶ ${kind}\n${target}`;
}

async function executeBrowserAction(action) {
  const kind = String(action?.kind || "").toUpperCase();
  // Mirror: show step + frame before the dispatch starts.
  try { _mirrorShowStep(_mirrorDescribeAction(action)); } catch (_e) {}
  if (kind.startsWith("BROWSER_") && kind !== "BROWSER_NAV" && kind !== "BROWSER_WAIT") {
    try { await dismissSimplifyOverlays(action); } catch (_e) {}
  }
  if (kind === "BROWSER_WAIT") {
    const ms = parseWaitMs(action?.target, 1000, 15000);
    await sleep(ms);
    return {
      ok: true,
      waited_ms: ms,
      page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: true, waitForStableMs: 150 })
    };
  }

  if (kind === "BROWSER_SCROLL") {
    const target = parseScrollTarget(action?.target);
    window.scrollTo(target);
    await sleep(250);
    await waitForPageStable(250, 1200);
    return {
      ok: true,
      scroll: { x: window.scrollX, y: window.scrollY },
      page_context: await pageContextAsync({ includeVisibleText: true, visibleTextLimit: 2200, waitForStableMs: 150 })
    };
  }

  if (kind === "BROWSER_KEY") {
    // Dispatch keyboard event to focused element or a specific target.
    // target format: "Escape" | "Tab" | "Enter" | "ArrowDown" | "ArrowUp" | etc.
    // Optionally prefix with selector: "input#name :: Enter"
    const raw = String(action?.target || "").trim();
    let targetEl = null;
    let keySpec = raw;
    if (raw.includes("::")) {
      const [sel, key] = raw.split("::", 2);
      keySpec = key.trim();
      try { targetEl = document.querySelector(sel.trim()); } catch (_e) {}
    }
    const el = targetEl || document.activeElement || document.body;
    const keyMap = {
      escape: "Escape", esc: "Escape",
      tab: "Tab",
      enter: "Enter", return: "Enter",
      space: " ",
      arrowdown: "ArrowDown", down: "ArrowDown",
      arrowup: "ArrowUp", up: "ArrowUp",
      arrowleft: "ArrowLeft", left: "ArrowLeft",
      arrowright: "ArrowRight", right: "ArrowRight",
      backspace: "Backspace", delete: "Delete",
      home: "Home", end: "End",
      pagedown: "PageDown", pageup: "PageUp",
    };
    const key = keyMap[keySpec.toLowerCase()] || keySpec;
    const opts = { key, bubbles: true, cancelable: true, composed: true };
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keypress", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
    if (key === "Tab") {
      // Move browser focus to next/prev focusable element
      const all = Array.from(document.querySelectorAll(
        "a,button,input,select,textarea,[tabindex]:not([tabindex='-1'])"
      )).filter(e => !e.disabled && e.offsetParent !== null);
      const idx = all.indexOf(document.activeElement);
      const next = all[idx + 1] || all[0];
      if (next) next.focus();
    }
    await sleep(120);
    await waitForPageStable(200, 800);
    return {
      ok: true,
      key,
      target_tag: el.tagName || "unknown",
      page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: true, waitForStableMs: 150 })
    };
  }

  if (kind === "BROWSER_READ_PAGE" || kind === "BROWSER_OBSERVE") {
    const ctx = await pageContextAsync({
      includeVisibleText: true,
      includeInteractiveElements: true,
      visibleTextLimit: READ_TEXT_LIMIT,
      waitForStableMs: PAGE_STABLE_DEBOUNCE_MS,
    });
    return {
      ok: true,
      page_context: ctx,
      text: ctx.visible_text || ctx.title || "page observed"
    };
  }

  if (kind === "BROWSER_READ_PAGE_FULL") {
    const full = buildReadPageFullPayload();
    const dispatch_result = await postDispatchEvent("page_read", full, action);
    const confirmation_post = await maybePostConfirmationDetected(action);
    return {
      ok: true,
      ...full,
      dispatch_result,
      confirmation_post,
      text: full.title || full.url || "page observed",
    };
  }

  if (kind === "BROWSER_READ") {
    const target = String(action?.target || "").trim();
    const el = target ? findElement(target) : null;
    return {
      ok: true,
      url: location.href,
      title: document.title,
      text: el ? elementText(el, READ_TEXT_LIMIT) : visibleText(READ_TEXT_LIMIT),
      page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
    };
  }

  if (kind === "BROWSER_CLICK") {
    if (_isAbortCommand(action)) {
      const pauseState = _pauseState();
      pauseState.first_app_pause_armed = false;
      pauseState.pending_submit = null;
      return { ok: false, aborted: true, reason: "abort_command" };
    }
    const el = findElement(action.target);
    if (!el) return { ok: false, error: "target not found" };
    const submitSignals = submitSignalsForElement(el);
    const submitPage = detectSubmitPage();
    const submittedCount = submittedCountFromAction(action);
    const pauseState = _pauseState();
    if (submittedCount > 0) {
      pauseState.first_app_pause_armed = false;
    }
    const isSubmitCandidate = submitPage.is_submit_page && submitSignals.length >= 2;
    if (isSubmitCandidate && pauseState.first_app_pause_armed && submittedCount === 0 && !_shouldResumeSubmit(action)) {
      const resumeToken = `resume_${Date.now()}`;
      pauseState.pending_submit = {
        selector: safeSelectorFor(el),
        name: _readPageName(el),
        resume_token: resumeToken,
      };
      await postDispatchEvent("submit_deferred", {
        url: safePageText(location.href || "", 2000),
        title: safePageText(document.title || "", 300),
        target: safeSelectorFor(el),
        submit_signals: submitSignals,
        resume_token: resumeToken,
      }, action);
      return {
        ok: true,
        deferred: true,
        reason: "first_submit_pause",
        is_submit_page: true,
        submit_signals: submitSignals,
        applications_submitted_this_session: submittedCount,
        resume_token: resumeToken,
      };
    }
    if (isSubmitCandidate && _shouldResumeSubmit(action)) {
      pauseState.first_app_pause_armed = false;
      pauseState.pending_submit = null;
    }
    el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    // Mirror: position the ghost cursor over the resolved target before
    // dispatching the click event. The animation is purely for the operator's
    // benefit; the actual click fires immediately via el.click().
    try { _mirrorMoveGhost(el); } catch (_e) {}
    // ── Receipt snapshot (before click) ───────────────────────────────────
    const _click_url_before = location.href;
    const _click_dom_before = document.querySelectorAll(ACTION_TARGETS).length;
    el.click();
    await waitForPageStable(350, 1400);
    const confirmation_post = await maybePostConfirmationDetected(action);
    const action_receipt = {
      ref: action.target,
      action: "BROWSER_CLICK",
      url_before: _click_url_before,
      url_after: location.href,
      url_changed: _click_url_before !== location.href,
      dom_delta: document.querySelectorAll(ACTION_TARGETS).length - _click_dom_before,
      submit_detected: submitSignals.length >= 2,
    };
    return {
      ok: true,
      clicked: action.target,
      confirmation_post,
      action_receipt,
      page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
    };
  }

  if (kind === "BROWSER_DOUBLE_CLICK") {
    const el = findElement(action.target);
    if (!el) return { ok: false, error: "target not found" };
    el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    try { _mirrorMoveGhost(el); } catch (_e) {}
    el.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true, view: window }));
    await waitForPageStable(350, 1400);
    return { ok: true, double_clicked: action.target, page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 }) };
  }

  // ── RIGHT CLICK ───────────────────────────────────────────────────────────
  if (kind === "BROWSER_RIGHT_CLICK") {
    const el = findElement(action.target);
    if (!el) return { ok: false, error: "target not found" };
    el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    try { _mirrorMoveGhost(el); } catch (_e) {}
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const opts = { bubbles: true, cancelable: true, composed: true, button: 2, buttons: 2,
                   clientX: cx, clientY: cy, screenX: cx, screenY: cy, view: window };
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    el.dispatchEvent(new MouseEvent("mouseup",   opts));
    el.dispatchEvent(new MouseEvent("contextmenu", opts));
    await waitForPageStable(350, 1200);
    return { ok: true, right_clicked: action.target, page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 }) };
  }

  // ── HOVER ─────────────────────────────────────────────────────────────────
  if (kind === "BROWSER_HOVER") {
    const el = findElement(action.target);
    if (!el) return { ok: false, error: "target not found" };
    el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    try { _mirrorMoveGhost(el); } catch (_e) {}
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const opts = { bubbles: true, cancelable: true, composed: true, clientX: cx, clientY: cy, screenX: cx, screenY: cy, view: window };
    el.dispatchEvent(new MouseEvent("mouseover", opts));
    el.dispatchEvent(new MouseEvent("mouseenter", opts));
    await sleep(300);
    await waitForPageStable(250, 800);
    return {
      ok: true,
      hovered: action.target,
      page_context: await pageContextAsync({ includeVisibleText: true, visibleTextLimit: 1500, waitForStableMs: 150 })
    };
  }

  // ── DRAG ──────────────────────────────────────────────────────────────────
  if (kind === "BROWSER_DRAG") {
    const fromEl = findElement(action.target);
    const toEl = findElement(action.to || action?.extras?.to || "");
    if (!fromEl) return { ok: false, error: "drag source not found" };
    if (!toEl) return { ok: false, error: "drag destination not found" };
    fromEl.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    await sleep(150);
    const fromRect = fromEl.getBoundingClientRect();
    const toRect = toEl.getBoundingClientRect();
    const fromX = fromRect.left + fromRect.width / 2;
    const fromY = fromRect.top + fromRect.height / 2;
    const toX = toRect.left + toRect.width / 2;
    const toY = toRect.top + toRect.height / 2;
    const mkOpts = (x, y, extra = {}) => ({ bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, screenX: x, screenY: y, view: window, ...extra });
    fromEl.dispatchEvent(new MouseEvent("mousedown", mkOpts(fromX, fromY, { button: 0 })));
    fromEl.dispatchEvent(new DragEvent("dragstart", { ...mkOpts(fromX, fromY), dataTransfer: new DataTransfer() }));
    await sleep(80);
    toEl.dispatchEvent(new DragEvent("dragover", mkOpts(toX, toY)));
    toEl.dispatchEvent(new DragEvent("drop", mkOpts(toX, toY)));
    fromEl.dispatchEvent(new DragEvent("dragend", mkOpts(toX, toY)));
    fromEl.dispatchEvent(new MouseEvent("mouseup", mkOpts(toX, toY, { button: 0 })));
    await waitForPageStable(350, 1200);
    return {
      ok: true,
      dragged: action.target,
      dropped_on: action.to,
      page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
    };
  }

  if (kind === "BROWSER_FILL") {
    const parsed = parseFillTarget(action);
    const fileUpload = action?.extras?.fileUpload || null;
    const wantsFileUpload = isFileUploadAction(action, parsed);
    const el = findFillElement(parsed, wantsFileUpload);
    if (!el) return {
      ok: false,
      result: "failure",
      reason: "target_not_found",
      expected: parsed.selector,
      observed: "[element missing]",
      error: "target not found",
    };
    // Sensitive-field gate (post-emit, pre-dispatch) — refuses to fill
    // password / SSN / cc_number / cvv / routing / account fields even when
    // the model emits an explicit BROWSER_FILL with a value. The deterministic
    // floor under the probabilistic Modelfile teaching. 2026-05-16 probe
    // confirmed the local 7B emits hallucinated passwords without this gate.
    if (!wantsFileUpload && !fileUpload) {
      let urlPath = ""; try { urlPath = window.location.pathname || ""; } catch (_e) {}
      const sensitive = _detectSensitiveField(el, urlPath);
        if (sensitive) {
          let labelText = "";
          try { labelText = renderedLabelFor(el); } catch (_e) {}
          return {
            ok: false,
            result: "failure",
            reason: "sensitive_field_gate",
            category: sensitive.category,
            tier: sensitive.tier,
            signals: sensitive.signals,
            target: parsed.selector,
            label: labelText || el.placeholder || "",
            page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
          };
        }
      }
      if (wantsFileUpload || fileUpload) {
        const upload = setFileInputValue(el, fileUpload);
        if (!upload.ok) {
          return {
            ok: false,
            result: "failure",
            reason: "file_upload_failed",
            expected: parsed.selector,
            observed: upload.error || "unknown",
            error: upload.error,
          };
        }
        return {
          ok: true,
          result: "success",
          observed: upload.file_name || "",
          file_name: upload.file_name,
          file_size: upload.file_size,
          mime: upload.mime || "",
          page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
        };
      }
    const current = currentElementValue(el);
    const requested = String(parsed.value || "");
    const overwrite = Boolean(parsed.overwrite || action?.overwrite || action?.extras?.overwrite || action?.extras?.force);
      if (current.trim() && fillValuesDiffer(current, requested) && !overwrite) {
        return {
          ok: false,
          result: "failure",
          reason: "value_conflict",
          expected: safePageText(requested, 500),
          observed: safePageText(current, 500),
          target: parsed.selector,
          label: renderedLabelFor(el) || "",
          page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
        };
      }
      if (current.trim() && !fillValuesDiffer(current, requested)) {
        return {
          ok: true,
          result: "success",
          observed: current,
          preserved_existing: true,
          page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
        };
      }
    // Operator's spec (SENSEI HARDENING STEP 1): explicit post-condition
    // verification with literal element.value compare. After setting the
    // value, dispatch input + change events, await 100ms, re-read the
    // LIVE element from the DOM, and return exactly one of two shapes:
    //   ok:false / result:'failure' / reason:'value_did_not_persist' / expected / observed
    //   ok:true  / result:'success' / observed / action_receipt
    // No other return path exists past this point.
    const _fill_url_before = location.href;
    const val = parsed.value;
    el.value = val;
    try { el.dispatchEvent(new Event("input",  { bubbles: true })); } catch (_e) {}
    try { el.dispatchEvent(new Event("change", { bubbles: true })); } catch (_e) {}
    await new Promise(r => setTimeout(r, 100));
    let probe = el;
    if (probe && probe.isConnected === false) {
      try { probe = findFillElement(parsed, false) || el; } catch (_e) { probe = el; }
    }
    const actual = (probe && "value" in probe) ? String(probe.value || "") : "";
    if (actual !== val) {
      return {
        ok: false,
        result: "failure",
        reason: "value_did_not_persist",
        expected: val,
        observed: actual,
      };
    }
    const action_receipt = {
      ref: action.target,
      action: "BROWSER_FILL",
      url_before: _fill_url_before,
      url_after: location.href,
      value_after: actual,
      value_matches: actual === val,
    };
    return {
      ok: true,
      result: "success",
      observed: actual,
      action_receipt,
    };
  }

  if (kind === "BROWSER_FILL_FORM") {
    let profile = action?.profile;
    if (!profile || typeof profile !== "object") {
      profile = await fetchProfileFromRouter(action);
    }
    if (!profile || typeof profile !== "object") {
      return {
        ok: false,
        error: "no profile available — server returned empty profile payload. Save/apply profile via Sensei then retry."
      };
    }
    let scope = document;
    const selectorHint = String(action?.target || "").trim();
    if (selectorHint) {
      try {
        const candidate = document.querySelector(selectorHint);
        if (candidate) scope = candidate;
      } catch (_e) { /* fall back to document scope */ }
    }
    const result = fillFormByProfileMatch(scope, profile);
    await waitForPageStable(350, 1500);
    // Surface the post-emit gate's findings so the next-turn model can act:
    // - captcha_present → emit NEEDS_INPUT: captcha
    // - skipped_sensitive non-empty → emit NEEDS_INPUT: <category> per entry
    // - filled=0 and nothing skipped → no profile-matched fields on this page
    const sensitiveCount = (result.skipped_sensitive || []).length;
    const unmatchedCount = (result.unmatched_required || []).length;
    const filledCount = Number(result.filled_count || 0);
    const captcha = result.captcha_present;
    let advisoryError;
    if (captcha) {
      advisoryError = `captcha detected (${captcha.captcha_type}${result.captcha_phase ? `, ${result.captcha_phase}` : ""}). Emit NEEDS_INPUT: captcha :: ${captcha.captcha_type} on this page — operator must solve before continuing.`;
    } else if (filledCount === 0 && sensitiveCount > 0) {
      const cats = [...new Set(result.skipped_sensitive.map(s => s.category))].join(", ");
      advisoryError = `every visible field on this form is sensitive (${cats}). Emit NEEDS_INPUT for each entry in skipped_sensitive instead of filling.`;
    } else if (filledCount === 0) {
      advisoryError = "no profile-matched fields found on this page";
    }
    return {
      ok: filledCount > 0 && !captcha,
      filled_count: filledCount,
      filled: result.filled || [],
      skipped_no_match: result.skipped_no_match,
      skipped_sensitive: result.skipped_sensitive || [],
      unmatched_required: result.unmatched_required || [],
      unmatched_required_count: unmatchedCount,
      captcha_present: captcha,
      fields: result.fields,
      scope: selectorHint || "document",
      error: advisoryError,
      page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: true, waitForStableMs: 200 })
    };
  }

  // Wave 1.4 (2026-05-17 PM) — BROWSER_UPLOAD_FILE: <selector> :: <absolute path>
  // File-input upload via CDP. content_script can't use chrome.debugger
  // directly, so we forward to service_worker (which has chrome.debugger and
  // knows our tabId from _sender). Two-step CDP under the hood:
  // Runtime.evaluate to resolve the selector, then DOM.setFileInputFiles to
  // push the file by absolute path. Page-side input+change events fire
  // server-side too. Mode-aware confirm gate lives in side_panel.js via
  // classifyBrowserAction → SENSITIVE_FILL — that gate runs BEFORE this
  // handler is reached. Existing setFileInputValue() at line ~1472 is a
  // base64-payload fallback for the rare case where a path-based upload
  // isn't viable; this new directive is the canonical path-based primitive.
  if (kind === "BROWSER_UPLOAD_FILE") {
    const raw = String(action?.target || "").trim();
    // Accept "<selector> :: <path>" OR "<selector> => <path>" OR "<selector> := <path>"
    const sepMatch = raw.match(/^(.+?)\s*(?:::|=>|:=)\s*(.+)$/);
    if (!sepMatch) {
      return {
        ok: false,
        error: "BROWSER_UPLOAD_FILE requires '<selector> :: <absolute path>' (sep '::' or '=>' or ':=')",
      };
    }
    const selector = sepMatch[1].trim();
    let absolutePath = sepMatch[2].trim();
    // Strip surrounding quotes if the model wrapped the path
    if ((absolutePath.startsWith('"') && absolutePath.endsWith('"')) ||
        (absolutePath.startsWith("'") && absolutePath.endsWith("'"))) {
      absolutePath = absolutePath.slice(1, -1);
    }
    // Reject relative paths; CDP needs absolute
    if (!absolutePath.startsWith("/") && !absolutePath.startsWith("~")) {
      return {
        ok: false,
        error: `BROWSER_UPLOAD_FILE path must be absolute (got '${absolutePath}'). Use $HOME or ~ prefix.`,
      };
    }
    const uploadGate = await uploadAllowlistVerdict(absolutePath);
    if (!uploadGate.valid) {
      return {
        valid: false,
        reason: "path outside upload allowlist",
        selector,
        path: absolutePath,
      };
    }
    // Pre-flight: does the selector resolve to a file input on this page?
    // Don't bail on a miss — let service_worker's CDP path do the real
    // resolution since the page may be deep in iframes/shadow DOM that
    // querySelector here can't reach. Just record a hint for the result.
    let preflight_hint = "";
    try {
      const el = document.querySelector(selector);
      if (el && String(el.getAttribute("type") || "").toLowerCase() !== "file") {
        preflight_hint = `warning: element matched but type=${el.getAttribute("type") || "(unset)"}`;
      } else if (!el) {
        preflight_hint = "warning: content_script querySelector did not find the element; CDP may still resolve via shadow/iframe";
      }
    } catch (_e) {}
    // Forward to service_worker
    try {
      const result = await new Promise((resolve) => {
        chrome.runtime.sendMessage(
          { type: "SENSEI_UPLOAD_FILE", selector, path: absolutePath },
          (resp) => {
            const err = chrome.runtime.lastError;
            if (err) {
              resolve({ ok: false, error: String(err.message || err) });
              return;
            }
            resolve(resp || { ok: false, error: "no response from service_worker" });
          },
        );
      });
      if (!result?.ok) {
        return {
          ok: false,
          error: result?.error || "upload failed",
          selector,
          path: absolutePath,
          preflight_hint,
        };
      }
      return {
        ok: true,
        uploaded: selector,
        path: absolutePath,
        file_name: result.file_name,
        file_size: result.file_size,
        file_type: result.file_type,
        files_length: result.files_length,
        preflight_hint,
        page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 200 }),
      };
    } catch (err) {
      return { ok: false, error: String(err?.message || err), selector, path: absolutePath };
    }
  }

  if (kind === "BROWSER_SUBMIT") {
    if (_isAbortCommand(action)) {
      const pauseState = _pauseState();
      pauseState.first_app_pause_armed = false;
      pauseState.pending_submit = null;
      return { ok: false, aborted: true, reason: "abort_command" };
    }
    const target = String(action?.target || "").trim();
    let submitEl = target ? findElement(target) : null;
    if (!submitEl) {
      submitEl = document.querySelector("button[type='submit'],input[type='submit'],button,[role='button']");
    }
    const form = submitEl?.closest?.("form") || document.querySelector("form");
    const submitSignals = submitEl ? submitSignalsForElement(submitEl) : [];
    const submitPage = detectSubmitPage();
    const submittedCount = submittedCountFromAction(action);
    const pauseState = _pauseState();
    if (submittedCount > 0) {
      pauseState.first_app_pause_armed = false;
    }
    const isSubmitCandidate = submitPage.is_submit_page && submitSignals.length >= 2;
    if (isSubmitCandidate && pauseState.first_app_pause_armed && submittedCount === 0 && !_shouldResumeSubmit(action)) {
      const resumeToken = `resume_${Date.now()}`;
      pauseState.pending_submit = {
        selector: submitEl ? safeSelectorFor(submitEl) : target || "form",
        name: submitEl ? _readPageName(submitEl) : "submit",
        resume_token: resumeToken,
      };
      await postDispatchEvent("submit_deferred", {
        url: safePageText(location.href || "", 2000),
        title: safePageText(document.title || "", 300),
        target: target || pauseState.pending_submit.selector,
        submit_signals: submitSignals,
        resume_token: resumeToken,
      }, action);
      return {
        ok: true,
        deferred: true,
        reason: "first_submit_pause",
        is_submit_page: true,
        submit_signals: submitSignals,
        applications_submitted_this_session: submittedCount,
        resume_token: resumeToken,
      };
    }
    if (isSubmitCandidate && _shouldResumeSubmit(action)) {
      pauseState.first_app_pause_armed = false;
      pauseState.pending_submit = null;
    }
    if (form) {
      if (typeof form.requestSubmit === "function") form.requestSubmit(submitEl && submitEl.form === form ? submitEl : undefined);
      else form.submit();
      await waitForPageStable(450, 2000);
      const confirmation_post = await maybePostConfirmationDetected(action);
      return {
        ok: true,
        submitted: true,
        target: target || "form",
        confirmation_post,
        page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
      };
    }
    if (submitEl) {
      submitEl.click();
      await waitForPageStable(450, 2000);
      const confirmation_post = await maybePostConfirmationDetected(action);
      return {
        ok: true,
        submitted: true,
        target: target || "submit_element",
        confirmation_post,
        page_context: await pageContextAsync({ includeVisibleText: false, includeInteractiveElements: false, waitForStableMs: 150 })
      };
    }
    return { ok: false, error: "submit target not found" };
  }

  if (kind === "BROWSER_NAV") {
    const url = String(action?.target || "").trim();
    if (!url) return { ok: false, error: "missing url" };
    window.location = url;
    return { ok: true, url: url, title: document.title };
  }

  if (kind === "BROWSER_CLOSE_TAB") {
    try {
      const ack = await chrome.runtime.sendMessage({ type: "SENSEI_CLOSE_CURRENT_TAB" });
      if (ack && ack.ok) return { ok: true, closed_tab: true };
      return { ok: false, error: ack?.error || "close_tab failed" };
    } catch (err) {
      return { ok: false, error: String(err?.message || err) };
    }
  }

  if (kind === "BROWSER_FIND") {
    const matches = findTextOnPage(action?.target);
    return {
      ok: true,
      query: safePageText(action?.target || "", 200),
      count: matches.length,
      matches,
      text: matches.length ? matches.map((m) => m.text).join("\n").slice(0, READ_TEXT_LIMIT) : ""
    };
  }

  if (kind === "BROWSER_EXTRACT_LIST") {
    const opts = parseJsonTarget(action);
    const mode = normalizeSearchText(opts.mode || action?.target || "");
    const state = mode.includes("drive") || /(^|\.)drive\.google\.com$/i.test(location.hostname)
      ? driveState()
      : genericListState();
    return { ok: true, list_state: state, text: state.summary };
  }

  if (kind === "BROWSER_DRIVE_INSPECT_FOLDER") {
    const state = driveState();
    return { ok: true, drive_state: state, text: state.summary };
  }

  if (kind === "BROWSER_READ_IMAGES") {
    const minPx = Number.isFinite(action?.min_size) ? action.min_size : 80;
    const selector = action?.target || null;
    const root = selector ? document.querySelector(selector) : document;
    if (!root) return { ok: false, error: `selector_not_found: ${selector}` };
    const seen = new Set();
    const results = [];
    const addUrl = (url, rect, alt) => {
      if (!url || url.startsWith("data:")) return;
      const key = url.split("?")[0];
      if (seen.has(key)) return;
      seen.add(key);
      results.push({ url, w: Math.round(rect?.width || 0), h: Math.round(rect?.height || 0), alt: alt || "" });
    };
    root.querySelectorAll("img").forEach(el => {
      const rect = el.getBoundingClientRect();
      if (rect.width < minPx && rect.height < minPx) return;
      if (el.srcset) {
        const parts = el.srcset.split(",").map(s => s.trim().split(/\s+/));
        const sorted = parts.filter(p => p.length >= 1).sort((a, b) => (parseFloat(b[1]) || 0) - (parseFloat(a[1]) || 0));
        if (sorted.length > 0) { addUrl(sorted[0][0], rect, el.alt); return; }
      }
      const src = el.getAttribute("data-src") || el.getAttribute("data-deferred-src") || el.src;
      addUrl(src, rect, el.alt);
    });
    root.querySelectorAll("[style]").forEach(el => {
      const rect = el.getBoundingClientRect();
      if (rect.width < minPx && rect.height < minPx) return;
      const bg = el.style.backgroundImage || "";
      const m = bg.match(/url\(["']?([^"')]+)["']?\)/);
      if (m) addUrl(m[1], rect, "");
    });
    return { ok: true, url: location.href, title: document.title, count: results.length, images: results.slice(0, 80) };
  }

  if (kind === "BROWSER_JS") {
    const code = action?.target || "";
    if (!code) return { ok: false, error: "BROWSER_JS: target (code) required" };
    try {
      // eslint-disable-next-line no-new-func
      const fn = new Function(code);
      const result = fn();
      if (result === undefined) return { ok: true, result: null };
      if (typeof result === "object") {
        try { return { ok: true, result: JSON.parse(JSON.stringify(result)) }; } catch (_e) {}
      }
      return { ok: true, result: String(result) };
    } catch (e) {
      return { ok: false, error: String(e?.message || e) };
    }
  }

  if (kind === "BROWSER_SCREENSHOT") {
    // Screenshot is handled in the background service worker via chrome.tabs.captureVisibleTab.
    // Content script signals that it should delegate upward.
    return { ok: false, error: "BROWSER_SCREENSHOT must be handled by background" };
  }

  return { ok: false, error: `unsupported browser action: ${kind}` };
}

if (globalThis.__SENSEI_ENABLE_TEST_API__) {
  globalThis.__SENSEI_TEST_API__ = {
    buildReadPageFullPayload,
    fillFormByProfileMatch,
    submitSignalsForElement,
    detectSubmitPage,
    detectConfirmationPage,
    extractReferenceNumber,
    dismissSimplifyOverlays,
    executeBrowserAction,
  };
}

if (globalThis.chrome?.runtime?.onMessage?.addListener) {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "SENSEI_PING") {
      sendResponse({ ok: true });
      return false;
    }

    if (message?.type === "SENSEI_PAGE_CONTEXT") {
      pageContextAsync(message.options || {})
        .then((ctx) => sendResponse({ ok: true, page_context: ctx }))
        .catch((err) => sendResponse({ ok: false, error: String(err?.message || err), page_context: pageContext({ includeVisibleText: false }) }));
      return true;
    }

    if (message?.type === "SENSEI_EXECUTE_ACTION") {
      executeBrowserAction(message.action)
        .then((result) => sendResponse(result))
        .catch((err) => sendResponse({ ok: false, error: String(err?.message || err) }));
      return true;
    }

    if (message?.type === "SENSEI_ROUTER_COMMAND") {
      const command = String(message.command || "").toLowerCase();
      if (command === "resume_click") {
        _resumePendingSubmit(message.resume_token || "")
          .then((result) => sendResponse(result))
          .catch((err) => sendResponse({ ok: false, error: String(err?.message || err) }));
        return true;
      }
      if (command === "abort") {
        const pauseState = _pauseState();
        pauseState.first_app_pause_armed = false;
        pauseState.pending_submit = null;
        sendResponse({ ok: true, aborted: true });
        return false;
      }
      sendResponse({ ok: false, error: "unsupported router command" });
      return false;
    }

    // Phase 5.2 — return the console-event ring buffer the page has been
    // accumulating since load. The buffer caps itself at CONSOLE_LIMIT so the
    // response stays bounded.
    if (message?.type === "SENSEI_READ_CONSOLE_EVENTS") {
      const filter = String(message.filter || "all").toLowerCase();
      const all = Array.isArray(globalThis.__SENSEI_CONSOLE_EVENTS__)
        ? globalThis.__SENSEI_CONSOLE_EVENTS__.slice(-CONSOLE_LIMIT)
        : [];
      const filtered = (filter === "all" || !filter)
        ? all
        : all.filter((e) => String(e?.level || "").toLowerCase() === filter);
      sendResponse(filtered);
      return false;
    }

    if (message?.type === "SENSEI_RECORD_START") {
      sendResponse(startWorkflowRecording());
      return false;
    }

    if (message?.type === "SENSEI_RECORD_STOP") {
      sendResponse(stopWorkflowRecording());
      return false;
    }

    // Iframe bridge — top-frame dispatch path. Side panel sends an action with
    // a target like "iframe::p_007". The top-frame content script strips the
    // "iframe::" prefix and posts the action into all child frames via
    // postMessage; the relay in each frame picks it up, forwards to the service
    // worker which routes back. The action_id lets the caller correlate results.
    if (message?.type === "SENSEI_IFRAME_DISPATCH") {
      const action = message.action || {};
      const actionId = message.actionId || crypto.randomUUID();
      try {
        const frames = document.querySelectorAll("iframe");
        frames.forEach((iframe) => {
          try {
            iframe.contentWindow?.postMessage(
              { type: "SENSEI_IFRAME_ACTION", action, actionId },
              "*"
            );
          } catch (_e) { /* cross-origin frame — postMessage rejects silently */ }
        });
        sendResponse({ ok: true, dispatched_to_frames: frames.length, actionId });
      } catch (err) {
        sendResponse({ ok: false, error: String(err?.message || err) });
      }
      return false;
    }

    return false;
  });
}
})();
