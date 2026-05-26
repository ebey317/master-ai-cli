const DEFAULTS = {
  backendUrl: "http://127.0.0.1:8080",
  agentUrl: "http://127.0.0.1:8001",
  wakeRelayUrl: "http://127.0.0.1:8765/wake",
  token: "",
  mode: "review",
  sessionId: "",
  actionPermissionMode: "ask",
  approvedOrigins: [],
  permissionHistory: [],
  // Local résumé/CV file path the model can reference for file-upload
  // BROWSER_FILL targets (commit 2.1 of the dispatcher plan). Empty until
  // the user sets it from the options page. Backend reads via the
  // /extension/read_local_file endpoint.
  resumePath: "",
  allowedUploadDirs: [],
  mcpServers: []
};

const $ = (selector) => document.querySelector(selector);

function chromeGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

function chromeSet(values) {
  return new Promise((resolve) => chrome.storage.local.set(values, resolve));
}

function setStatus(text) {
  $("#status").textContent = text;
}

function randomToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/[+/=]/g, "").slice(0, 32);
}

function normalizeConfig(config) {
  const next = { ...DEFAULTS, ...config };
  if (!["ask", "act"].includes(next.actionPermissionMode)) next.actionPermissionMode = "ask";
  if (!Array.isArray(next.approvedOrigins)) next.approvedOrigins = [];
  if (!Array.isArray(next.permissionHistory)) next.permissionHistory = [];
  if (!Array.isArray(next.allowedUploadDirs)) next.allowedUploadDirs = [];
  if (!Array.isArray(next.mcpServers)) next.mcpServers = [];
  return next;
}

function renderApprovedSites(origins = []) {
  const list = $("#approvedSites");
  list.textContent = "";
  if (!origins.length) {
    const item = document.createElement("li");
    item.textContent = "No approved sites";
    list.appendChild(item);
    return;
  }
  for (const origin of origins) {
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = origin;
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.textContent = "Revoke";
    revoke.addEventListener("click", async () => {
      const stored = normalizeConfig(await chromeGet(Object.keys(DEFAULTS)));
      stored.approvedOrigins = stored.approvedOrigins.filter((value) => value !== origin);
      await chromeSet({ approvedOrigins: stored.approvedOrigins });
      renderApprovedSites(stored.approvedOrigins);
      setStatus("Permission revoked");
    });
    item.append(label, revoke);
    list.appendChild(item);
  }
}

function renderMcpServers(servers = []) {
  const list = $("#mcpServers");
  list.textContent = "";
  if (!servers.length) {
    const item = document.createElement("li");
    item.textContent = "No MCP servers";
    list.appendChild(item);
    return;
  }
  for (const server of servers) {
    const item = document.createElement("li");
    const label = document.createElement("span");
    const scopes = Array.isArray(server.scopes) && server.scopes.length
      ? ` scopes=${server.scopes.join(",")}`
      : "";
    label.textContent = `${server.name || server.url} — ${server.url}${scopes}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.addEventListener("click", async () => {
      const stored = normalizeConfig(await chromeGet(Object.keys(DEFAULTS)));
      stored.mcpServers = stored.mcpServers.filter((value) => value.id !== server.id);
      await chromeSet({ mcpServers: stored.mcpServers });
      renderMcpServers(stored.mcpServers);
      setStatus("MCP server removed");
    });
    item.append(label, remove);
    list.appendChild(item);
  }
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 3500) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    if (err?.name === "AbortError") throw new Error("health check timed out");
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function load() {
  const stored = await chromeGet(Object.keys(DEFAULTS));
  const config = normalizeConfig(stored);
  if (!config.sessionId) config.sessionId = `sensei-${crypto.randomUUID()}`;
  $("#backendUrl").value = config.backendUrl || DEFAULTS.backendUrl;
  $("#agentUrl").value = config.agentUrl || "";
  $("#wakeRelayUrl").value = config.wakeRelayUrl || "";
  $("#token").value = config.token || "";
  $("#mode").value = config.mode || "review";
  $("#sessionId").value = config.sessionId;
  $("#actionPermissionMode").value = config.actionPermissionMode;
  $("#resumePath").value = config.resumePath || "";
  renderApprovedSites(config.approvedOrigins);
  renderMcpServers(config.mcpServers);
  await chromeSet({ sessionId: config.sessionId });
  setStatus("Loaded");
}

async function save() {
  const values = {
    backendUrl: $("#backendUrl").value.trim().replace(/\/+$/, "") || DEFAULTS.backendUrl,
    agentUrl: $("#agentUrl").value.trim().replace(/\/+$/, ""),
    wakeRelayUrl: $("#wakeRelayUrl").value.trim(),
    token: $("#token").value.trim(),
    mode: $("#mode").value,
    sessionId: $("#sessionId").value.trim() || `sensei-${crypto.randomUUID()}`,
    actionPermissionMode: $("#actionPermissionMode").value,
    resumePath: $("#resumePath").value.trim()
  };
  await chromeSet(values);
  setStatus("Saved");
}

async function addMcpServer() {
  const name = $("#mcpName").value.trim();
  const url = $("#mcpUrl").value.trim().replace(/\/+$/, "");
  const scopes = $("#mcpScopes").value.split(",").map((s) => s.trim()).filter(Boolean);
  if (!url) { setStatus("Set an MCP server URL first"); return; }
  const stored = normalizeConfig(await chromeGet(Object.keys(DEFAULTS)));
  const server = {
    id: `mcp-${crypto.randomUUID()}`,
    name: name || url,
    url,
    scopes,
  };
  stored.mcpServers = [server, ...stored.mcpServers.filter((item) => item.url !== url)].slice(0, 20);
  await chromeSet({ mcpServers: stored.mcpServers });
  $("#mcpName").value = "";
  $("#mcpUrl").value = "";
  $("#mcpScopes").value = "";
  renderMcpServers(stored.mcpServers);
  setStatus("MCP server added");
}

async function testResumeRead() {
  const backendUrl = $("#backendUrl").value.trim().replace(/\/+$/, "") || DEFAULTS.backendUrl;
  const token = $("#token").value.trim();
  const path = $("#resumePath").value.trim();
  if (!path) { setStatus("Set a résumé path first"); return; }
  setStatus("Reading résumé file...");
  try {
    const res = await fetchWithTimeout(`${backendUrl}/extension/read_local_file`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Master-AI-Token": token
      },
      body: JSON.stringify({ path })
    }, 8000);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
    if (!data.ok) throw new Error(data.error || "backend refused");
    setStatus(`Resume readable — ${data.size} bytes, ${data.mime}`);
  } catch (err) {
    setStatus(`Read failed: ${err.message}`);
  }
}

async function testBackend() {
  const backendUrl = $("#backendUrl").value.trim().replace(/\/+$/, "") || DEFAULTS.backendUrl;
  const token = $("#token").value.trim();
  setStatus("Testing");
  try {
    const res = await fetchWithTimeout(`${backendUrl}/health`, {
      headers: { "X-Master-AI-Token": token }
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
    setStatus(data.ok ? "Backend ready" : "Backend degraded");
  } catch (err) {
    setStatus(`Test failed: ${err.message}`);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  load();
  $("#optionsForm").addEventListener("submit", (event) => {
    event.preventDefault();
    save();
  });
  $("#testBackend").addEventListener("click", testBackend);
  $("#testResumeRead").addEventListener("click", testResumeRead);
  $("#addMcpServer").addEventListener("click", addMcpServer);
  $("#generateToken").addEventListener("click", () => {
    $("#token").value = randomToken();
    setStatus("Token generated");
  });
  $("#clearApprovedSites").addEventListener("click", async () => {
    await chromeSet({ approvedOrigins: [] });
    renderApprovedSites([]);
    setStatus("Approved sites cleared");
  });
});
