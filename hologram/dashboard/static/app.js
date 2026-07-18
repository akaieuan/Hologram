// hologram dashboard — vanilla JS, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

const state = {
  data: null,
  events: [],
  assetFilter: "",
  catFilter: "",
  feedFilter: "",       // client-side activity-feed search
  sse: null,
  view: "live",
  blenderMcp: null,
  selectedKey: null,   // `${cat}/${name}` of the asset chosen in the visualizer
  stageKey: null,      // key currently rendered in the stage (guards model reload)
  active: [],          // in-flight tool calls (from /api/active)
  live: { status: "connecting", label: "connecting" },
  skills: null,        // plugin skill registry (from /api/skills; null until fetched)
  golden: null,        // golden truths + per-asset budgets (from /api/golden)
};

// ── Helpers ──────────────────────────────────────────────────────────

function sessionColor(sid) {
  // Hash the session id to a stable hue, then set lightness/saturation per
  // theme so tags stay legible on both warm paper and warm near-black.
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  if (!sid) return { hex: dark ? "#93928f" : "#64635e" };
  let h = 0;
  for (let i = 0; i < sid.length; i++) h = (h * 31 + sid.charCodeAt(i)) >>> 0;
  const [s, l] = dark ? [52, 66] : [48, 40];
  return { hex: `hsl(${h % 360}, ${s}%, ${l}%)` };
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

function durLabel(ms) {
  if (ms == null || !isFinite(ms)) return "";
  const s = ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
  return ` <span class="fi-dur">· ${s}</span>`;
}

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
  if (ev.type === "check_run") {
    const bits = [`${ev.assets} asset${ev.assets === 1 ? "" : "s"}`];
    if (ev.errors) bits.push(`${ev.errors} error${ev.errors === 1 ? "" : "s"}`);
    if (ev.warnings) bits.push(`${ev.warnings} warning${ev.warnings === 1 ? "" : "s"}`);
    if (!ev.errors && !ev.warnings) bits.push("all clean");
    return { text: "ran checks", sub: bits.join(" · ") };
  }
  if (ev.type === "asset_diff") {
    const bits = [];
    for (const [field, names] of Object.entries(ev.gained || {})) bits.push(`+${names.length} ${field}`);
    for (const [field, names] of Object.entries(ev.lost || {})) bits.push(`-${names.length} ${field}`);
    if (!bits.length) for (const [name, d] of Object.entries(ev.changed || {})) bits.push(`${name} ${d.from}→${d.to}`);
    const summary = bits.join(", ");
    const sub = ev.path && summary ? `${ev.path} · ${summary}` : (ev.path || summary);
    return { text: "asset changed", sub };
  }
  const failed = ev.failed === true;
  if (ev.mcp_tool) {
    const short = ev.mcp_tool.replace(/^mcp__/, "");
    const params = ev.params ? Object.entries(ev.params).map(([k, v]) => `${k}=${v}`).join(" ") : "";
    return { text: `${failed ? "failed calling" : "called"} <code>${esc(short)}</code>`, sub: params };
  }
  if (ev.tool === "Bash") return { text: failed ? "shell command failed" : "ran shell command", sub: ev.command || "" };
  if (ev.tool === "Edit" || ev.tool === "Write" || ev.tool === "MultiEdit") {
    const verb = ev.tool === "Write" ? "wrote" : "edited";
    const failVerb = ev.tool === "Write" ? "failed to write" : "failed to edit";
    return { text: `${failed ? failVerb : verb} <code>${esc(ev.file_path || "file")}</code>`, sub: "" };
  }
  return { text: esc(ev.type || "event"), sub: "" };
}

function shouldShow(ev) {
  if (ev.phase === "pre") return ev.type === "skill_invoke";
  return true;
}

// Plain-text haystack for the activity-feed search — the humanized line (tags
// stripped) plus the raw fields a user is likely to grep for.
function eventHaystack(ev) {
  const { text, sub } = humanize(ev);
  const plain = String(text).replace(/<[^>]*>/g, "");
  return [plain, sub, ev.session_id, ev.tool, ev.mcp_tool, ev.type,
    ev.command, ev.file_path, ev.skill, ev.args, ev.detail, ev.error]
    .filter(Boolean).join(" ").toLowerCase();
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
    $$(".tab").forEach(b => {
      const on = b === btn;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", String(on));
    });
    $$(".view").forEach(el => el.classList.toggle("active", el.id === `view-${v}`));
    if (v === "debug") renderDebug();
    if (v === "assets") renderAssets();
    if (v === "skills") renderSkills();
  });
});

// ── Theme toggle ─────────────────────────────────────────────────────
// The initial theme is set before paint by the inline <head> script; this
// only handles flipping + persisting the user's explicit choice.

$("#theme-toggle")?.addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("hologram-theme", next); } catch (e) { /* private mode */ }
  // Session colours are hashed to inline hex per theme — redraw so the tags
  // pick up the new theme's lightness instead of keeping stale values.
  renderSummary();
  renderFeed();
  if (state.view === "assets") renderAssets();
});

// ── Live status popover ──────────────────────────────────────────────
// The header live tag opens a panel summarising what's happening right now:
// connection, project + root, Blender MCP, active sessions, in-flight calls.

const liveTrigger = $("#live-trigger");
const livePop = $("#live-pop");

function openLivePop() {
  if (!livePop) return;
  livePop.hidden = false;
  liveTrigger.setAttribute("aria-expanded", "true");
  renderLivePop();
}
function closeLivePop() {
  if (!livePop || livePop.hidden) return;
  livePop.hidden = true;
  liveTrigger.setAttribute("aria-expanded", "false");
}
liveTrigger?.addEventListener("click", e => {
  e.stopPropagation();
  if (livePop.hidden) openLivePop(); else closeLivePop();
});
document.addEventListener("click", e => {
  if (livePop && !livePop.hidden && !e.target.closest(".live-wrap")) closeLivePop();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && livePop && !livePop.hidden) { closeLivePop(); liveTrigger.focus(); }
});

function renderLivePop() {
  if (!livePop || livePop.hidden) return;
  const live = state.live || { status: "connecting", label: "connecting" };
  const d = state.data;
  const mcp = state.blenderMcp;
  const active = state.active || [];
  const now = Date.now() / 1000;

  const sessions = new Map();
  for (const ev of state.events) {
    if (!ev.session_id || !ev.ts || (now - ev.ts) > 300) continue;
    const cur = sessions.get(ev.session_id) || { count: 0, last: 0 };
    cur.count++; cur.last = Math.max(cur.last, ev.ts);
    sessions.set(ev.session_id, cur);
  }

  const connClass = live.status === "connected" ? "ok" : (live.status === "error" ? "err" : "");
  let html = `<div class="lp-conn">
      <span class="lp-cdot ${connClass}"></span>
      <span class="lp-conn-label">${esc(live.label)}</span>
    </div>
    <div class="lp-grid">
      <div class="lp-row"><span class="lp-k">project</span><span class="lp-v">${d?.project ? esc(d.project) : "—"}</span></div>
      <div class="lp-row col"><span class="lp-k">root</span><span class="lp-v lp-path">${d?.root ? esc(d.root) : "—"}</span></div>
      <div class="lp-row col"><span class="lp-k">export</span><span class="lp-v lp-path">${d?.export_root ? esc(d.export_root) : "—"}</span></div>
      <div class="lp-row"><span class="lp-k">assets</span><span class="lp-v">${d?.totals ? `${d.totals.assets} in ${d.totals.categories} cats` : "—"}</span></div>
      <div class="lp-row"><span class="lp-k">blender mcp</span><span class="lp-v">${mcp
        ? (mcp.on
            ? `<span class="lp-tag ok">on</span> <span class="lp-path">${esc(mcp.host)}:${mcp.port}</span>`
            : `<span class="lp-tag off">off</span> <span class="lp-path">${esc(mcp.host)}:${mcp.port}</span>`)
        : "<span class=\"muted\">probing…</span>"}</span></div>
    </div>`;

  // Active sessions (last 5 min)
  const sorted = [...sessions.entries()].sort((a, b) => b[1].last - a[1].last);
  html += `<div class="lp-sec-head">sessions <span class="lp-count">${sorted.length}</span></div>`;
  if (sorted.length) {
    html += `<div class="lp-chips">` + sorted.map(([sid, info]) => {
      const { hex } = sessionColor(sid);
      return `<span class="lp-sess" style="border-color:${hex}"><span class="dot" style="background:${hex}"></span>${esc(shortSid(sid))}<span class="lp-sess-count">${info.count}</span></span>`;
    }).join("") + `</div>`;
  } else {
    html += `<div class="lp-empty muted">none active</div>`;
  }

  // In-flight tool calls
  html += `<div class="lp-sec-head">in flight <span class="lp-count">${active.length}</span></div>`;
  if (active.length) {
    html += active.map(a => {
      const { hex } = sessionColor(a.session_id);
      const tool = esc((a.tool || "tool").replace(/^mcp__/, ""));
      const target = a.target ? `<span class="lp-flow-target">${esc(truncate(a.target, 60))}</span>` : "";
      const elapsed = a.duration_s != null ? `${Number(a.duration_s).toFixed(1)}s` : "";
      return `<div class="lp-flow">
          <span class="lp-flow-tool" style="color:${hex}">${tool}</span>${target}
          <span class="lp-flow-dur">${esc(elapsed)}</span>
        </div>`;
    }).join("");
  } else {
    html += `<div class="lp-empty muted">idle</div>`;
  }

  livePop.innerHTML = html;
}

// ── Summary ──────────────────────────────────────────────────────────

// Set a stat-band number and reflect its state on the cell: empty metrics
// recede (.zero), live ones glow (.live), failures go red (.bad).
function setStat(sel, val, { live = false, bad = false } = {}) {
  const el = $(sel);
  if (!el) return;
  el.textContent = val;
  const cell = el.closest(".stat-cell");
  if (!cell) return;
  const n = Number(val);
  cell.classList.toggle("bad", bad && n > 0);
  cell.classList.toggle("live", live && n > 0);
  cell.classList.toggle("zero", n === 0 && !(bad && n > 0) && !(live && n > 0));
}

function renderSummary() {
  if (!state.data) return;
  $("#project-name").textContent = state.data.project || "";
  setStat("#sum-assets", state.data.totals.assets);
  setStat("#sum-cats", state.data.totals.categories);
  $("#tab-assets-count").textContent = state.data.totals.assets || "";

  const now = Date.now() / 1000;
  const recentFails = state.events.filter(e => e.failed && e.ts && (now - e.ts) <= 300).length;
  setStat("#sum-fails", recentFails, { bad: true });

  const sessions = new Map();
  for (const ev of state.events) {
    if (!ev.session_id || !ev.ts || (now - ev.ts) > 300) continue;
    const cur = sessions.get(ev.session_id) || { count: 0, last: 0 };
    cur.count++; cur.last = Math.max(cur.last, ev.ts);
    sessions.set(ev.session_id, cur);
  }
  setStat("#sum-sessions", sessions.size, { live: true });
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
  renderLivePop();
}

// ── Feed ────────────────────────────────────────────────────────────

function renderFeed() {
  const root = $("#feed");
  const shown = state.events.filter(shouldShow);
  const q = (state.feedFilter || "").trim().toLowerCase();
  const visible = q ? shown.filter(ev => eventHaystack(ev).includes(q)) : shown;
  if (visible.length === 0) {
    if (q && shown.length) {
      root.innerHTML = `<div class="feed-empty"><p>No activity matches “${esc(state.feedFilter.trim())}”.</p>
        <p class="muted">Clear the filter to see all ${shown.length} recent events.</p></div>`;
      return;
    }
    root.innerHTML = `<div class="feed-empty"><p>No activity yet.</p>
      <p class="muted">Call the MCP tools, load the hologram plugin in Claude Code, or append to the event log — activity streams here live.</p></div>`;
    return;
  }
  root.innerHTML = "";
  for (const ev of visible.slice(0, 150)) {
    const cat = classify(ev);
    const { hex } = sessionColor(ev.session_id);
    const { text, sub } = humanize(ev);
    const failed = ev.failed === true;
    const interrupted = failed && ev.is_interrupt === true;
    const dotColor = failed ? "var(--bad)" : CAT_COLORS[cat];
    let errLine = "";
    if (failed) {
      const msg = ev.error ? esc(truncate(ev.error, 280)) : (interrupted ? "interrupted by user" : "failed");
      errLine = `<div class="fi-sub err">${msg}${durLabel(ev.duration_ms)}</div>`;
    }
    const el = document.createElement("div");
    el.className = "feed-item" + (ev._new ? " new" : "") + (failed ? " failed" : "");
    el.innerHTML = `
      <div class="fi-time">${esc(ageLabel(ev.ts))}</div>
      <div class="fi-rail"><div class="fi-rail-dot" style="background:${dotColor};"></div></div>
      <div class="fi-body">
        <div class="fi-text"><span class="session-tag" style="color:${hex};">${esc(shortSid(ev.session_id))}</span> ${text}</div>
        ${sub ? `<div class="fi-sub">${esc(truncate(sub, 280))}</div>` : ""}
        ${errLine}
      </div>`;
    root.appendChild(el);
  }
}

// ── Assets visualizer ───────────────────────────────────────────────
// A selectable list (grouped by category) drives one big inline preview +
// inspect panel — no drawer. Selection survives live refreshes; the stage only
// re-renders (and reloads the heavy model-viewer) when the chosen asset changes.

function renderAssets() {
  const scroll = $("#asset-list-scroll");
  const catSel = $("#cat-filter");
  if (!scroll || !state.data) { if (scroll) scroll.innerHTML = ""; return; }

  const cats = Object.keys(state.data.categories);
  const prev = catSel.value;
  catSel.innerHTML = `<option value="">all categories</option>` + cats.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  if (cats.includes(prev)) catSel.value = prev;

  const q = state.assetFilter.toLowerCase();
  const onlyCat = state.catFilter;
  scroll.innerHTML = "";
  let any = false, selectionVisible = false;
  let firstCat = null, firstEntry = null;

  for (const [cat, entries] of Object.entries(state.data.categories)) {
    if (onlyCat && cat !== onlyCat) continue;
    const matching = entries.filter(e => !q || e.name.toLowerCase().includes(q));
    if (!matching.length) continue;
    any = true;

    const group = document.createElement("div");
    group.className = "asset-group";
    group.innerHTML = `<div class="asset-group-head">${esc(cat)}<span class="asset-group-count">${matching.length}</span></div>`;
    for (const e of matching) {
      const key = `${cat}/${e.name}`;
      if (!firstEntry) { firstCat = cat; firstEntry = e; }
      if (key === state.selectedKey) selectionVisible = true;
      const item = document.createElement("button");
      item.type = "button";
      item.className = "asset-item" + (key === state.selectedKey ? " selected" : "");
      item.dataset.name = e.name;
      item.dataset.key = key;
      item.innerHTML = `<span class="ai-dot" aria-hidden="true"></span>
        <span class="ai-name">${esc(e.name)}</span>
        <span class="ai-age">${esc(ageLabel(e.mtime))}</span>`;
      item.addEventListener("click", () => selectAsset(cat, e));
      group.appendChild(item);
    }
    scroll.appendChild(group);
  }

  if (!any) {
    scroll.innerHTML = `<div class="muted" style="padding:var(--space-5);text-align:center">No assets match.</div>`;
    state.selectedKey = null;
    renderStageEmpty("No assets found under export_root.");
    return;
  }
  // Keep the current selection if it's still on screen; otherwise fall back to
  // the first visible asset so the stage is never empty when assets exist.
  if (!selectionVisible && firstEntry) selectAsset(firstCat, firstEntry);
}

function selectAsset(cat, entry, force = false) {
  const key = `${cat}/${entry.name}`;
  state.selectedKey = key;
  $$("#asset-list-scroll .asset-item").forEach(it =>
    it.classList.toggle("selected", it.dataset.key === key));
  if (!force && state.stageKey === key) return;  // already showing it — don't reload
  renderStage(cat, entry);
}

function renderStage(cat, entry) {
  const stage = $("#asset-stage");
  if (!stage) return;
  const key = `${cat}/${entry.name}`;
  state.stageKey = key;
  ensureViewerLoaded();
  stage.innerHTML = `
    <div class="stage-head">
      <div class="stage-title">${esc(entry.name)}</div>
      <div class="stage-cat">${esc(cat)}</div>
    </div>
    ${previewHtml(entry)}
    ${manifestHtml(entry)}
    <div class="stage-versions" id="stage-versions"></div>
    <div class="stage-details" id="stage-details">
      <div class="muted" style="padding:var(--space-4) 0">${entry.glb ? "inspecting…" : "not exported — no GLB to inspect"}</div>
    </div>`;
  if (!entry.glb) return;

  // Inspect + checks are async; a fast click-through could land the wrong
  // payload, so we bail if the selection moved on before the fetches resolved.
  const q = encodeURIComponent(entry.glb);
  Promise.all([
    fetch(`/api/inspect?path=${q}`).then(r => r.json()).catch(() => ({ error: "inspect failed" })),
    fetch(`/api/checks?path=${q}`).then(r => r.json()).catch(() => null),
  ]).then(([d, c]) => {
    if (state.stageKey !== key) return;
    const det = $("#stage-details");
    if (!det) return;
    if (d.error) {
      det.innerHTML = `<div class="finding err"><div class="msg">${esc(d.error)}</div></div>`;
      return;
    }
    det.innerHTML = checksHtml(c) + diffHtml(d) + inspectHtml(d);
  });

  // Version history is an explogo-convention extra: only fetch it when the
  // asset carries a manifest record (asset id == GLB stem == entry.name).
  if (entry.manifest) {
    fetch(`/api/history?asset=${encodeURIComponent(entry.name)}`)
      .then(r => r.json())
      .then(hd => { if (state.stageKey === key && !hd.error) renderVersions(entry.name, hd); })
      .catch(() => { /* history is optional — the stage still stands without it */ });
  }
}

// ── Manifest provenance (explogo export convention) ──────────────────
// When /api/state enriched this asset with its manifest record, surface the
// provenance the pipeline recorded: version, generator, tri count, params, and
// the rendered thumbnail. Absent manifest → returns "" (no block, no change).

function manifestHtml(entry) {
  const m = entry.manifest;
  if (!m) return "";
  const mrow = (k, v) => `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
  const rows = [];
  if (m.version != null) rows.push(mrow("version", `v${esc(m.version)}`));
  if (m.generator) rows.push(mrow("generator", `<code>${esc(m.generator)}</code>`));
  if (m.tris != null) {
    // With a golden.json present the tri count reads against its budget —
    // "12,400 / 30,000" — and goes red when the asset is over.
    const b = state.golden?.budgets?.[entry.name];
    const tris = Number(m.tris).toLocaleString();
    rows.push(b && b.budget != null
      ? mrow("tris", `<span class="${b.over ? "budget-over" : "budget-ok"}">${esc(tris)} / ${esc(Number(b.budget).toLocaleString())}</span>`)
      : mrow("tris", esc(tris)));
  }
  const review = m.review && m.review.status;
  if (review) {
    const cls = review === "gate-failed" ? "bad" : (review === "approved" ? "good" : "pend");
    rows.push(mrow("review", `<span class="review-pill ${cls}">${esc(review)}</span>${m.review.version != null ? ` <span class="muted">v${esc(m.review.version)}</span>` : ""}`));
  }
  if (m.status) rows.push(mrow("status", esc(m.status)));
  if (m.updated_at) rows.push(mrow("updated", esc(m.updated_at)));
  const thumb = m.thumbnail
    ? `<img class="asset-thumb" alt="thumbnail for ${esc(entry.name)}" loading="lazy"
         src="/api/thumb?asset=${encodeURIComponent(entry.name)}" />`
    : "";
  return `<div class="provenance">
      <h4>manifest</h4>
      <div class="prov-body">
        ${thumb}
        <div class="prov-rows">${rows.join("")}</div>
      </div>
      ${paramsHtml(m.params)}
    </div>`;
}

function paramsHtml(params) {
  if (!params || typeof params !== "object" || !Object.keys(params).length) return "";
  const rows = Object.entries(params).map(([k, v]) => {
    const val = (v !== null && typeof v === "object") ? JSON.stringify(v) : String(v);
    return `<div class="param-row"><span class="param-k">${esc(k)}</span><span class="param-v">${esc(val)}</span></div>`;
  }).join("");
  const n = Object.keys(params).length;
  return `<details class="params">
      <summary>params <span class="param-count">${n}</span></summary>
      <div class="param-list">${rows}</div>
    </details>`;
}

// ── Version history flip-through ─────────────────────────────────────
// Snapshots come from .history/<asset>/vN.glb. Selecting one introspects that
// snapshot and diffs its fingerprint against the current export (reusing the
// same diff machinery as the regression baseline). Read-only throughout.

function renderVersions(assetId, data) {
  const box = $("#stage-versions");
  if (!box) return;
  const versions = data.versions || [];
  if (!versions.length) { box.innerHTML = ""; return; }
  const cur = data.current_version;
  const chips = versions.slice().reverse().map(v =>
    `<button type="button" class="version-chip" data-v="${esc(v)}">v${esc(v)}</button>`
  ).join("");
  box.innerHTML = `<div class="versions-block">
      <h4>version history</h4>
      <div class="versions-meta">
        <span class="versions-current">current · v${esc(cur ?? "?")}</span>
        <span class="versions-count">${versions.length} snapshot${versions.length === 1 ? "" : "s"}</span>
      </div>
      <div class="versions-chips">${chips}</div>
      <div class="version-detail" id="version-detail"></div>
    </div>`;
  box.querySelectorAll(".version-chip").forEach(btn =>
    btn.addEventListener("click", () => selectVersion(assetId, btn.dataset.v, btn)));
}

function selectVersion(assetId, v, btn) {
  const detail = $("#version-detail");
  if (!detail) return;
  $$("#stage-versions .version-chip").forEach(b => b.classList.toggle("selected", b === btn));
  detail.innerHTML = `<div class="muted" style="padding:var(--space-3) 0">loading v${esc(v)}…</div>`;
  const keyAtFetch = state.stageKey;
  fetch(`/api/history?asset=${encodeURIComponent(assetId)}&v=${encodeURIComponent(v)}`)
    .then(r => r.json())
    .then(d => {
      if (state.stageKey !== keyAtFetch) return;  // selection moved on
      if (d.error) { detail.innerHTML = `<div class="finding err"><div class="msg">${esc(d.error)}</div></div>`; return; }
      detail.innerHTML = versionDiffHtml(d) + inspectHtml(d.inspect || {});
    })
    .catch(() => { detail.innerHTML = `<div class="finding err"><div class="msg">history fetch failed</div></div>`; });
}

function versionDiffHtml(d) {
  const from = d.compared_from || `v${d.version}`;
  const to = d.compared_to || "current";
  const head = `<div class="diff-head">${esc(from)} → ${esc(to)}</div>`;
  const dd = d.diff;
  if (dd == null) return `<div class="diff-block">${head}<div class="diff-row">${esc(d.note || "nothing to compare against")}</div></div>`;
  const rows = [];
  for (const [field, names] of Object.entries(dd.gained || {}))
    rows.push(`<div class="diff-row gained"><span class="diff-sign">+</span><b>${esc(field)}</b> · ${esc(names.join(", "))}</div>`);
  for (const [field, names] of Object.entries(dd.lost || {}))
    rows.push(`<div class="diff-row lost"><span class="diff-sign">−</span><b>${esc(field)}</b> · ${esc(names.join(", "))}</div>`);
  for (const [name, delta] of Object.entries(dd.changed || {}))
    rows.push(`<div class="diff-row changed"><b>${esc(name)}</b> · ${esc(String(delta.from))} → ${esc(String(delta.to))}</div>`);
  const body = rows.length ? rows.join("") : `<div class="diff-row">identical fingerprint — no structural change</div>`;
  return `<div class="diff-block">${head}${body}</div>`;
}

// "Changes since last check" — present only when /api/inspect attached a diff
// (the asset's fingerprint moved since the last `hologram check` baseline).
// Reads d.diff; never triggers a baseline write.
function diffHtml(d) {
  const dd = d && d.diff;
  if (!dd) return "";
  const rows = [];
  for (const [field, names] of Object.entries(dd.gained || {}))
    rows.push(`<div class="diff-row gained"><span class="diff-sign">+</span><b>${esc(field)}</b> · ${esc(names.join(", "))}</div>`);
  for (const [field, names] of Object.entries(dd.lost || {}))
    rows.push(`<div class="diff-row lost"><span class="diff-sign">−</span><b>${esc(field)}</b> · ${esc(names.join(", "))}</div>`);
  for (const [name, delta] of Object.entries(dd.changed || {}))
    rows.push(`<div class="diff-row changed"><b>${esc(name)}</b> · ${esc(String(delta.from))} → ${esc(String(delta.to))}</div>`);
  if (!rows.length) return "";
  return `<div class="diff-block"><div class="diff-head">changes since last check</div>${rows.join("")}</div>`;
}

// Verdicts from /api/checks: problems rendered as .finding rows (reusing the
// failure styling), a quiet pass line when everything is clean.
function checksHtml(c) {
  if (!c || c.error) return "";
  let html = "";
  if (c.load_error) {
    html += `<div class="finding err"><div class="msg"><b>checks file</b> · ${esc(c.load_error)}</div></div>`;
  }
  const findings = c.findings || [];
  const problems = findings.filter(f => !f.ok);
  if (problems.length) {
    html += `<div class="findings">` + problems.map(f =>
      `<div class="finding ${f.severity === "error" ? "err" : "warn"}"><div class="msg"><b>${esc(f.check)}</b> · ${esc(f.message)}</div></div>`
    ).join("") + `</div>`;
  } else if (findings.length) {
    html += `<div class="checks-ok"><span class="ok-mark">✓</span> ${findings.length} checks passed</div>`;
  }
  return html;
}

function inspectHtml(d) {
  // One aligned manifest row per metric — count and named contents live
  // together (no duplicate number-strip + chip-section), names flow into the
  // horizontal space, empty rows recede.
  const chips = (arr, kind) =>
    arr.length
      ? `<div class="chips">${arr.map(n => `<span class="chip"><span class="chip-dot ${kind}"></span>${esc(n)}</span>`).join("")}</div>`
      : "";
  const row = (label, count, arr = [], kind = "") => {
    const zero = Number(count) === 0;
    return `<div class="mf-row${zero ? " zero" : ""}"><dt>${label}</dt><dd class="mf-n">${count}</dd><dd class="mf-names">${chips(arr, kind)}</dd></div>`;
  };
  const skinBones = (d.skins || []).map(b => `${b.length} bones`);
  return `
    <div class="row"><span class="k">file</span><span class="v">${esc(d.filename)}</span></div>
    <div class="row"><span class="k">path</span><span class="v">${esc(d.path)}</span></div>
    <h4>contents</h4>
    <dl class="manifest">
      ${row("nodes", d.node_count, d.top_level_nodes || [], "nodes")}
      ${row("meshes", d.mesh_count)}
      ${row("animations", d.animation_count, d.animations || [], "anims")}
      ${row("materials", d.material_count, d.materials || [], "mats")}
      ${row("skins", d.skin_count, skinBones)}
    </dl>`;
}

function renderStageEmpty(msg) {
  const stage = $("#asset-stage");
  state.stageKey = null;
  if (stage) stage.innerHTML = `<div class="stage-empty muted">${esc(msg)}</div>`;
}

function pulseAssets(names) {
  if (state.view !== "assets") return;
  for (const name of names) {
    const item = $(`#asset-list-scroll .asset-item[data-name="${CSS.escape(name)}"]`);
    if (!item) continue;
    item.classList.remove("pulse"); void item.offsetWidth; item.classList.add("pulse");
  }
}

// ── GLB viewer (lazy) ───────────────────────────────────────────────
// The WebGL viewer is heavy (~900KB) and web-only, so it loads lazily on the
// first asset selection and is cached by the browser thereafter. Vendored
// locally under static/vendor/ so previews work offline.
let viewerRequested = false;
function ensureViewerLoaded() {
  if (viewerRequested) return;
  viewerRequested = true;
  const s = document.createElement("script");
  s.type = "module";
  s.src = "/static/vendor/model-viewer.min.js";
  document.head.appendChild(s);
}

function previewHtml(entry) {
  if (!entry.glb) return `<div class="preview"><div class="preview-msg">not exported</div></div>`;
  const src = `/api/glb?path=${encodeURIComponent(entry.glb)}`;
  return `<div class="preview"><model-viewer src="${src}" camera-controls touch-action="pan-y"
      exposure="1" shadow-intensity="0.4" interaction-prompt="none" loading="eager"
      ><div class="preview-msg" slot="poster">loading model…</div></model-viewer></div>`;
}

// ── Skills registry (the codex: what the plugin can do) ──────────────
// Read-only view over /api/skills — the bundled plugin skills, grouped by
// kind. The registry derives from each SKILL.md's frontmatter, so this list
// is always exactly what's installed, never a hand-maintained copy.

const SKILL_KIND_ORDER = ["lifecycle", "workflow", "audit", "gate", "reference"];
const SKILL_KIND_BLURB = {
  lifecycle: "starting, stopping, and catching up",
  workflow: "guided multi-step processes",
  audit: "read-only conformance sweeps",
  gate: "readying work for human review",
  reference: "quick-lookup knowledge",
};

function renderSkills() {
  const root = $("#skills-list");
  if (!root) return;
  if (state.skills == null) { fetchSkills(); return; }
  const skills = state.skills;
  if (!skills.length) {
    root.innerHTML = `<div class="muted" style="padding: var(--space-5) 0">No skills found — is the plugin bundled with this install?</div>`;
    return;
  }
  const byKind = new Map();
  for (const s of skills) {
    const k = SKILL_KIND_ORDER.includes(s.kind) ? s.kind : "workflow";
    if (!byKind.has(k)) byKind.set(k, []);
    byKind.get(k).push(s);
  }
  let html = "";
  for (const kind of SKILL_KIND_ORDER) {
    const group = byKind.get(kind);
    if (!group) continue;
    html += `<section class="skill-group">
        <div class="skill-group-head">${esc(kind)}
          <span class="skill-group-blurb">${esc(SKILL_KIND_BLURB[kind] || "")}</span>
        </div>` +
      group.map(s => `
        <article class="skill-row">
          <div class="skill-row-top">
            <code class="skill-trigger">${esc(s.trigger)}</code>
            <span class="skill-name">${esc(s.name)}</span>
          </div>
          <p class="skill-desc">${esc(s.description)}</p>
        </article>`).join("") +
      `</section>`;
  }
  root.innerHTML = html;
}

async function fetchSkills() {
  try {
    const d = await (await fetch("/api/skills")).json();
    state.skills = d.skills || d || [];
  } catch (e) {
    state.skills = [];
  }
  const badge = $("#tab-skills-count");
  if (badge) badge.textContent = state.skills.length || "";
  if (state.view === "skills") renderSkills();
}

async function fetchGolden() {
  try {
    const d = await (await fetch("/api/golden")).json();
    state.golden = d && d.present ? d : null;
  } catch (e) { /* optional — assets view just shows plain tri counts */ }
  if (state.view === "assets") renderAssets();
}

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
$("#feed-search")?.addEventListener("input", e => { state.feedFilter = e.target.value; renderFeed(); });

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
async function fetchActive() {
  try { renderActive((await (await fetch("/api/active")).json()).active); }
  catch (e) { /* optional — strip just stays hidden */ }
}

// ── In-flight strip ─────────────────────────────────────────────────

function renderActive(active) {
  state.active = active || [];
  renderLivePop();
  const box = $("#inflight");
  if (!box) return;
  if (!active || !active.length) { box.hidden = true; box.innerHTML = ""; return; }
  const rows = active.map(a => {
    const { hex } = sessionColor(a.session_id);
    const tool = esc((a.tool || "tool").replace(/^mcp__/, ""));
    const target = a.target ? `<span class="target">${esc(truncate(a.target, 90))}</span>` : "";
    const elapsed = a.duration_s != null ? `${Number(a.duration_s).toFixed(1)}s` : "";
    return `<div class="inflight-row">
        <div class="what"><span class="tool" style="color:${hex};">${tool}</span>${target}</div>
        <div class="elapsed">${esc(elapsed)}</div>
      </div>`;
  }).join("");
  box.innerHTML = `<div class="inflight-head"><span class="spin"></span>In flight</div>${rows}`;
  box.hidden = false;
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
  state.live = { status, label };
  const dot = $("#live-dot");
  dot.classList.remove("connected", "error");
  if (status === "connected") dot.classList.add("connected");
  if (status === "error") dot.classList.add("error");
  $("#live-label").textContent = label;
  renderLivePop();
}

// Re-render ages every 10s so "2m ago" stays accurate.
setInterval(() => { if (state.view === "live") renderFeed(); }, 10000);

// ── Boot ────────────────────────────────────────────────────────────

fetchState();
fetchEvents();
fetchBlenderMcp();
fetchActive();
fetchSkills();
fetchGolden();
connectSSE();
setInterval(() => fetchState(false), 15000);
setInterval(fetchBlenderMcp, 5000);
setInterval(fetchActive, 2000);
setInterval(fetchGolden, 60000); // goldens are human-edited; a slow refresh is plenty
