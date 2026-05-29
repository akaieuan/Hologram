// hologram dashboard — vanilla JS, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

const state = {
  data: null,
  events: [],
  assetFilter: "",
  catFilter: "",
  sse: null,
  view: "live",
  blenderMcp: null,
};

// ── Helpers ──────────────────────────────────────────────────────────

function sessionColor(sid) {
  if (!sid) return { hex: "#6a7280" };
  let h = 0;
  for (let i = 0; i < sid.length; i++) h = (h * 31 + sid.charCodeAt(i)) >>> 0;
  return { hex: `hsl(${h % 360}, 62%, 66%)` };
}
function shortSid(sid) {
  if (!sid) return "—";
  return sid.startsWith("mcp-") ? sid : sid.slice(0, 8);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}
function truncate(s, n) { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

function ageLabel(ts) {
  if (!ts) return "—";
  const age = Date.now() / 1000 - ts;
  if (age < 5) return "just now";
  if (age < 60) return `${Math.floor(age)}s ago`;
  if (age < 3600) return `${Math.floor(age / 60)}m ago`;
  if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
  return `${Math.floor(age / 86400)}d ago`;
}

function classify(ev) {
  if (ev.type === "skill_invoke") return "skill";
  if (ev.type === "mcp_server" || ev.mcp_tool) return "mcp";
  if (ev.type === "session_start" || ev.type === "session_stop") return "session";
  return "tool";
}
const CAT_COLORS = {
  skill: "var(--type-skill)", mcp: "var(--type-mcp)",
  tool: "var(--type-file)", session: "var(--type-session)",
};

function humanize(ev) {
  if (ev.type === "session_start") return { text: "started a Claude Code session", sub: ev.cwd || "" };
  if (ev.type === "session_stop") return { text: "ended the session", sub: "" };
  if (ev.type === "mcp_server") {
    const action = ev.action || "";
    if (action === "mcp.start") return { text: "MCP server came online", sub: ev.detail || "" };
    if (action === "mcp.stop") return { text: "MCP server stopped", sub: "" };
    if (action.endsWith(".start")) return { text: `started <code>${esc(action.slice(0, -6))}</code>`, sub: ev.detail || "" };
    if (action.endsWith(".end")) return { text: `finished <code>${esc(action.slice(0, -4))}</code>`, sub: ev.detail || "" };
    return { text: `<code>${esc(action)}</code>`, sub: ev.detail || "" };
  }
  if (ev.type === "skill_invoke") {
    const args = ev.args ? ` ${ev.args}` : "";
    return { text: `ran <code>/${esc(ev.skill || "?")}</code>${esc(args)}`, sub: "" };
  }
  if (ev.mcp_tool) {
    const short = ev.mcp_tool.replace(/^mcp__/, "");
    const params = ev.params ? Object.entries(ev.params).map(([k, v]) => `${k}=${v}`).join(" ") : "";
    return { text: `called <code>${esc(short)}</code>`, sub: params };
  }
  if (ev.tool === "Bash") return { text: "ran shell command", sub: ev.command || "" };
  if (ev.tool === "Edit" || ev.tool === "Write" || ev.tool === "MultiEdit") {
    const verb = ev.tool === "Write" ? "wrote" : "edited";
    return { text: `${verb} <code>${esc(ev.file_path || "file")}</code>`, sub: "" };
  }
  return { text: esc(ev.type || "event"), sub: "" };
}

function shouldShow(ev) {
  if (ev.phase === "pre") return ev.type === "skill_invoke";
  return true;
}

function touchedAssets(ev) {
  const names = new Set();
  const hay = [ev.file_path, ev.command, JSON.stringify(ev.params || {}), ev.args, ev.detail]
    .filter(Boolean).join(" ");
  if (!state.data) return [];
  for (const entries of Object.values(state.data.categories)) {
    for (const e of entries) if (e.name && hay.includes(e.name)) names.add(e.name);
  }
  return [...names];
}

// ── View switching ──────────────────────────────────────────────────

$$(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    const v = btn.dataset.view;
    state.view = v;
    $$(".tab").forEach(b => b.classList.toggle("active", b === btn));
    $$(".view").forEach(el => el.classList.toggle("active", el.id === `view-${v}`));
    if (v === "debug") renderDebug();
    if (v === "assets") renderAssets();
  });
});

// ── Summary ──────────────────────────────────────────────────────────

function renderSummary() {
  if (!state.data) return;
  $("#project-name").textContent = state.data.project || "";
  $("#sum-assets").textContent = state.data.totals.assets;
  $("#sum-cats").textContent = state.data.totals.categories;
  $("#tab-assets-count").textContent = state.data.totals.assets || "";

  const now = Date.now() / 1000;
  const sessions = new Map();
  for (const ev of state.events) {
    if (!ev.session_id || !ev.ts || (now - ev.ts) > 300) continue;
    const cur = sessions.get(ev.session_id) || { count: 0, last: 0 };
    cur.count++; cur.last = Math.max(cur.last, ev.ts);
    sessions.set(ev.session_id, cur);
  }
  const mcp = state.blenderMcp;
  let mcpHtml;
  if (!mcp) mcpHtml = `<span class="mcp-chip mcp-unknown"><span class="dot"></span>Blender MCP <span class="port">…</span></span>`;
  else if (mcp.on) mcpHtml = `<span class="mcp-chip mcp-on" title="listening on ${esc(mcp.host)}:${mcp.port}"><span class="dot"></span>Blender MCP on <span class="port">:${mcp.port}</span></span>`;
  else mcpHtml = `<span class="mcp-chip mcp-off" title="no listener on ${esc(mcp.host)}:${mcp.port}"><span class="dot"></span>Blender MCP off <span class="port">:${mcp.port}</span></span>`;

  const sorted = [...sessions.entries()].sort((a, b) => b[1].last - a[1].last);
  const chipHtml = sorted.map(([sid, info]) => {
    const { hex } = sessionColor(sid);
    return `<span class="sess-chip" style="border-color:${hex};"><span class="dot" style="background:${hex};"></span>${esc(shortSid(sid))}<span class="count">${info.count}</span></span>`;
  }).join("");
  $("#summary-sessions").innerHTML = mcpHtml + (chipHtml || `<span class="muted">no sessions active</span>`);
}

// ── Feed ────────────────────────────────────────────────────────────

function renderFeed() {
  const root = $("#feed");
  const visible = state.events.filter(shouldShow);
  if (visible.length === 0) {
    root.innerHTML = `<div class="feed-empty"><p>No activity yet.</p>
      <p class="muted">Call the MCP tools, load the hologram plugin in Claude Code, or append to the event log — activity streams here live.</p></div>`;
    return;
  }
  root.innerHTML = "";
  for (const ev of visible.slice(0, 150)) {
    const cat = classify(ev);
    const { hex } = sessionColor(ev.session_id);
    const { text, sub } = humanize(ev);
    const el = document.createElement("div");
    el.className = "feed-item" + (ev._new ? " new" : "");
    el.innerHTML = `
      <div class="fi-time">${esc(ageLabel(ev.ts))}</div>
      <div class="fi-rail"><div class="fi-rail-dot" style="background:${CAT_COLORS[cat]};"></div></div>
      <div class="fi-body">
        <div class="fi-text"><span class="session-tag" style="color:${hex};">${esc(shortSid(ev.session_id))}</span> ${text}</div>
        ${sub ? `<div class="fi-sub">${esc(truncate(sub, 280))}</div>` : ""}
      </div>`;
    root.appendChild(el);
  }
}

// ── Assets ──────────────────────────────────────────────────────────

function renderAssets() {
  const root = $("#assets");
  const catSel = $("#cat-filter");
  if (!state.data) { root.innerHTML = ""; return; }
  const cats = Object.keys(state.data.categories);
  const prev = catSel.value;
  catSel.innerHTML = `<option value="">all categories</option>` + cats.map(c => `<option value="${c}">${c}</option>`).join("");
  if (cats.includes(prev)) catSel.value = prev;

  const q = state.assetFilter.toLowerCase();
  const onlyCat = state.catFilter;
  root.innerHTML = "";
  let any = false;
  for (const [cat, entries] of Object.entries(state.data.categories)) {
    if (onlyCat && cat !== onlyCat) continue;
    for (const e of entries) {
      if (q && !e.name.toLowerCase().includes(q)) continue;
      any = true;
      const card = document.createElement("div");
      card.className = "asset-card";
      card.dataset.name = e.name;
      card.innerHTML = `<div class="name">${esc(e.name)}</div>
        <div class="meta"><span class="dot"></span><span>${esc(cat)} · ${esc(ageLabel(e.mtime))}</span></div>`;
      card.addEventListener("click", () => openAssetDrawer(cat, e));
      root.appendChild(card);
    }
  }
  if (!any) root.innerHTML = `<div class="muted" style="padding:30px;text-align:center">No assets found under export_root.</div>`;
}

function pulseAssets(names) {
  if (state.view !== "assets") return;
  for (const name of names) {
    const card = $(`#assets .asset-card[data-name="${CSS.escape(name)}"]`);
    if (!card) continue;
    card.classList.remove("pulse"); void card.offsetWidth; card.classList.add("pulse");
  }
}

// ── Drawer (GLB introspection) ──────────────────────────────────────

async function openAssetDrawer(category, entry) {
  $("#drawer-title").textContent = `${category} / ${entry.name}`;
  const body = $("#drawer-body");
  body.innerHTML = `<div class="muted">loading…</div>`;
  $("#drawer").classList.add("open");
  if (!entry.glb) { body.innerHTML = `<div class="muted">not exported</div>`; return; }
  try {
    const res = await fetch(`/api/inspect?path=${encodeURIComponent(entry.glb)}`);
    const d = await res.json();
    if (d.error) { body.innerHTML = `<div class="finding err"><div class="msg">${esc(d.error)}</div></div>`; return; }
    const chips = (arr) => `<div class="chips">${arr.map(n => `<span class="chip">${esc(n)}</span>`).join("")}</div>`;
    let html = `
      <div class="row"><span class="k">file</span><span class="v">${esc(d.filename)}</span></div>
      <div class="row"><span class="k">path</span><span class="v">${esc(d.path)}</span></div>
      <div class="stat-row">
        <div class="stat"><b>${d.node_count}</b><span>nodes</span></div>
        <div class="stat"><b>${d.mesh_count}</b><span>meshes</span></div>
        <div class="stat"><b>${d.animation_count}</b><span>anims</span></div>
        <div class="stat"><b>${d.material_count}</b><span>materials</span></div>
        <div class="stat"><b>${d.skin_count}</b><span>skins</span></div>
      </div>`;
    if (d.top_level_nodes?.length) html += `<h4>Top-level nodes</h4>${chips(d.top_level_nodes)}`;
    if (d.animations?.length) html += `<h4>Animations</h4>${chips(d.animations)}`;
    if (d.materials?.length) html += `<h4>Materials</h4>${chips(d.materials)}`;
    if (d.skins?.length) html += `<h4>Skins</h4>` + d.skins.map((b, i) => `<div class="row"><span class="k">skin ${i}</span><span class="v">${b.length} bones</span></div>`).join("");
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = `<div class="finding err"><div class="msg">inspect failed</div></div>`;
  }
}
$("#drawer-close").addEventListener("click", () => $("#drawer").classList.remove("open"));

// ── Debug ───────────────────────────────────────────────────────────

function renderDebug() {
  $("#debug-state").textContent = state.data ? JSON.stringify(state.data, null, 2) : "(no state)";
  $("#debug-events").textContent = state.events.slice(0, 100).map(e => JSON.stringify(e)).join("\n") || "(no events)";
}

// ── Copy + toast ────────────────────────────────────────────────────

function copyToClipboard(text, label = "content") {
  const doCopy = navigator.clipboard?.writeText
    ? navigator.clipboard.writeText(text)
    : new Promise(res => {
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch {}
        document.body.removeChild(ta); res();
      });
  doCopy.then(() => showToast(`copied ${label}`)).catch(() => showToast("copy failed"));
}
let toastTimer;
function showToast(m) {
  const el = $("#toast");
  el.textContent = m; el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 1600);
}
document.addEventListener("click", e => {
  const btn = e.target.closest("[data-copy-target]");
  if (btn) {
    const tgt = $(btn.dataset.copyTarget);
    if (tgt) copyToClipboard(tgt.textContent, btn.dataset.copyTarget.replace("#", ""));
  }
});

// ── Filters ─────────────────────────────────────────────────────────

$("#asset-filter").addEventListener("input", e => { state.assetFilter = e.target.value; renderAssets(); });
$("#cat-filter").addEventListener("change", e => { state.catFilter = e.target.value; renderAssets(); });

// ── Fetching ────────────────────────────────────────────────────────

async function fetchState(force = false) {
  try {
    const res = await fetch(`/api/state${force ? "?force=1" : ""}`);
    state.data = await res.json();
    renderSummary();
    if (state.view === "assets") renderAssets();
    if (state.view === "debug") renderDebug();
  } catch (e) { console.error("state fetch failed", e); }
}
async function fetchBlenderMcp() {
  try { state.blenderMcp = await (await fetch("/api/blender_mcp")).json(); renderSummary(); }
  catch (e) { /* optional */ }
}
async function fetchEvents() {
  try {
    const d = await (await fetch("/api/events?limit=500")).json();
    state.events = d.events || [];
    renderFeed(); renderSummary();
    if (state.view === "debug") renderDebug();
  } catch (e) { console.error("events fetch failed", e); }
}

// ── SSE ─────────────────────────────────────────────────────────────

function connectSSE() {
  if (state.sse) state.sse.close();
  const sse = new EventSource("/api/events/stream");
  state.sse = sse;
  sse.addEventListener("init", e => {
    setLive("connected", "live");
    state.events = JSON.parse(e.data).events || [];
    renderFeed(); renderSummary();
  });
  sse.addEventListener("append", e => {
    const ev = JSON.parse(e.data);
    ev._new = true;
    state.events.unshift(ev);
    if (state.events.length > 2000) state.events.length = 2000;
    renderFeed(); renderSummary();
    pulseAssets(touchedAssets(ev));
    clearTimeout(connectSSE._r);
    connectSSE._r = setTimeout(() => fetchState(true), 800);
  });
  sse.onerror = () => { setLive("error", "reconnecting"); setTimeout(connectSSE, 3000); };
}
function setLive(status, label) {
  const dot = $("#live-dot");
  dot.classList.remove("connected", "error");
  if (status === "connected") dot.classList.add("connected");
  if (status === "error") dot.classList.add("error");
  $("#live-label").textContent = label;
}

// Re-render ages every 10s so "2m ago" stays accurate.
setInterval(() => { if (state.view === "live") renderFeed(); }, 10000);

// ── Boot ────────────────────────────────────────────────────────────

fetchState();
fetchEvents();
fetchBlenderMcp();
connectSSE();
setInterval(() => fetchState(false), 15000);
setInterval(fetchBlenderMcp, 5000);
