/* Knowledge Graph Explorer — talks only to this project's API.
 *
 * SECURITY: entity names, quotes and answers come from uploaded documents and the LLM,
 * i.e. untrusted text. Everything is inserted with textContent (see el()), never innerHTML,
 * and the graph library's hover labels (which ARE rendered as HTML) go through escapeHtml().
 */
"use strict";

const COLORS = {
  Person: "#4cc9f0", Company: "#f77f00", Project: "#c77dff",
  Technology: "#80ed99", Team: "#ffd166", Document: "#8d99ae",
};
const DIM = "#253047";
const STAGES = ["chunking", "extracting", "writing_graph", "indexing"];
const FINAL = new Set(["completed", "failed", "rejected", "extraction_failed"]);
const KEY_STORE = "kgApiKey";

const state = {
  key: null, mode: "3d", showDocs: false, showManual: true,
  raw: null, graph: null, hlNodes: new Set(), hlLinks: new Set(), labels: new Map(),
};
const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ helpers */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null) continue;
    if (k === "text") node.textContent = v;
    else if (k === "className") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function store(action, value) {
  try {
    if (action === "get") return sessionStorage.getItem(KEY_STORE);
    if (action === "set") sessionStorage.setItem(KEY_STORE, value);
    if (action === "del") sessionStorage.removeItem(KEY_STORE);
  } catch { /* storage blocked: key lives in memory only */ }
  return null;
}
function toast(msg, ms = 3500) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}
const idOf = (end) => (typeof end === "object" ? end.id : end);

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), "X-API-Key": state.key } });
  if (res.status === 401) {
    store("del");
    showKeyModal("That API key was rejected.");
    throw new Error("unauthorised");
  }
  if (res.status === 429) {
    const wait = res.headers.get("Retry-After") || "a few";
    toast(`Rate limit reached — try again in ${wait} s`);
    throw new Error("rate limited");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
  return body;
}

/* --------------------------------------------------------------- the graph */
function nodeColor(n) {
  if (state.hlNodes.size && !state.hlNodes.has(n.id)) return DIM;
  return COLORS[n.type] || "#cbd5e1";
}
function linkColor(l) {
  if (state.hlNodes.size) return state.hlLinks.has(l) ? "#ffffff" : "rgba(70,80,100,0.18)";
  if (l.type === "MENTIONS") return "rgba(141,153,174,0.22)";
  return l.sourced ? "rgba(150,200,255,0.7)" : "rgba(150,160,180,0.28)";
}
function linkText(l) {
  const props = Object.entries(l.properties || {}).map(([k, v]) => `${k}: ${v}`).join(", ");
  const src = l.type === "MENTIONS" ? "" : l.sourced ? ` · ${l.evidence_count} quote(s)` : " · hand-entered, no source";
  return `${l.type}${props ? ` {${props}}` : ""}${src}`;
}
const nodeSize = (n) => 2 + Math.min(n.degree, 10) * 1.2;

function makeLabel(n) {
  if (typeof SpriteText !== "function") return null;      // labels are optional; hover still works
  const s = new SpriteText(n.name);
  s.color = COLORS[n.type] || "#cbd5e1";
  s.textHeight = 3 + Math.min(n.degree, 8) * 0.25;
  s.fontWeight = "600";
  s.position.set(0, 4 + Math.cbrt(nodeSize(n)) * 4, 0);
  state.labels.set(n.id, s);
  return s;
}

function createGraph() {
  const box = $("graph");
  if (state.graph && state.graph._destructor) state.graph._destructor();
  box.replaceChildren();
  state.labels.clear();
  const common = (g) => g
    .nodeId("id")
    .nodeVal(nodeSize)
    .nodeColor(nodeColor)
    .nodeLabel((n) => `${escapeHtml(n.name)} <span style="opacity:.6">· ${escapeHtml(n.type)}</span>`)
    .linkColor(linkColor)
    .linkLabel((l) => escapeHtml(linkText(l)))
    .linkDirectionalArrowLength((l) => (l.type === "MENTIONS" ? 0 : 3.5))
    .linkDirectionalArrowRelPos(1)
    .linkDirectionalParticles((l) => (state.hlLinks.has(l) ? 3 : 0))
    .linkDirectionalParticleWidth(2.2)
    .onNodeClick(focusNode)
    .onLinkClick(showLink)
    .onBackgroundClick(clearHighlight)
    // Layout is computed in simulation STEPS, not seconds: when the browser tab is in the
    // background, frames are throttled and a time-based cooldown froze the graph half-built
    // (found while testing). warmupTicks lays it out before the first frame is even drawn.
    .warmupTicks(120)
    .cooldownTicks(150)
    .cooldownTime(60000)
    .onEngineStop(() => { if (state.needsFit) { state.needsFit = false; fitView(); } })
    .width($("graph").clientWidth).height($("graph").clientHeight);

  if (state.mode === "3d") {
    if (typeof ForceGraph3D !== "function") return graphUnavailable();
    state.graph = common(ForceGraph3D()(box))
      .backgroundColor("#070b14")
      .nodeOpacity(0.95)
      .nodeResolution(16)
      .linkWidth((l) => (state.hlLinks.has(l) ? 1.4 : l.sourced && l.type !== "MENTIONS" ? 0.5 : 0))
      .linkOpacity(0.7)
      .nodeThreeObjectExtend(true)
      .nodeThreeObject(makeLabel);
    // Found while testing: with the global three r149 (needed by the 3D labels) the library's
    // default lights (π-scaled, meant for newer three.js) blow every sphere out to white.
    state.graph.lights().forEach((l) => { l.intensity = l.type === "AmbientLight" ? 0.75 : 0.6; });
  } else {
    if (typeof ForceGraph !== "function") return graphUnavailable();
    state.graph = common(ForceGraph()(box))
      .backgroundColor("#070b14")
      .linkWidth((l) => (state.hlLinks.has(l) ? 2 : 1))
      .nodeCanvasObjectMode(() => "after")
      .nodeCanvasObject((n, ctx, scale) => {
        const size = 11 / scale;
        ctx.font = `600 ${size}px Inter, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = state.hlNodes.size && !state.hlNodes.has(n.id) ? "rgba(120,130,150,.35)" : "#e2e8f0";
        ctx.fillText(n.name, n.x, n.y + Math.sqrt(nodeSize(n)) * 4 + 2 / scale);
      });
  }
  if (state.graph) {
    // Spread the layout: the library defaults (charge -30, distance 30) packed nodes so tightly
    // that labels overlapped (found while testing). 2D needs more room than 3D.
    const twoD = state.mode === "2d";
    state.graph.d3Force("charge").strength(twoD ? -260 : -120);
    state.graph.d3Force("link").distance(twoD ? 75 : 45);
  }
  if (state.raw) setGraphData();
}

function graphUnavailable() {
  $("graph").replaceChildren(el("p", { className: "hint", style: "padding:120px 24px",
    text: "The graph library could not be loaded (no internet / CDN blocked). The side panel still works." }));
  state.graph = null;
}

function setGraphData() {
  if (!state.graph) return;
  // fresh copies: the force simulation mutates nodes/links
  const nodes = state.raw.nodes.map((n) => ({ ...n }));
  const keep = new Set(nodes.map((n) => n.id));
  const links = state.raw.links
    .filter((l) => state.showManual || l.sourced)
    .filter((l) => keep.has(l.source) && keep.has(l.target))
    .map((l) => ({ ...l }));
  state.hlNodes.clear();
  state.hlLinks.clear();
  state.graph.graphData({ nodes, links });
  state.needsFit = true;                       // fit once the layout has settled (see createGraph)
}

/* Frame the graph (or the nodes passing `filter`). Found while testing: zoomToFit called while
   the layout was still expanding left the camera INSIDE the graph. So we fit after the
   simulation stops, from the nodes' real extent, and never closer than a minimum distance. */
function fitView(filter = () => true) {
  const g = state.graph;
  if (!g) return;
  const nodes = g.graphData().nodes.filter((n) => filter(n) && Number.isFinite(n.x));
  if (!nodes.length) return;
  if (state.mode === "2d") {
    g.zoomToFit(600, 80, filter);
    setTimeout(() => state.graph && state.graph.zoom() > 3 && state.graph.zoom(3, 300), 900);
    return;
  }
  const c = ["x", "y", "z"].map((k) => nodes.reduce((s, n) => s + n[k], 0) / nodes.length);
  const radius = Math.max(...nodes.map((n) => Math.hypot(n.x - c[0], n.y - c[1], n.z - c[2])), 20);
  const dist = Math.max(220, radius * 2.8);
  const cam = g.cameraPosition();
  let dir = [cam.x - c[0], cam.y - c[1], cam.z - c[2]];
  const len = Math.hypot(...dir);
  dir = len > 1 ? dir.map((v) => v / len) : [0, 0, 1];
  g.cameraPosition({ x: c[0] + dir[0] * dist, y: c[1] + dir[1] * dist, z: c[2] + dir[2] * dist },
    { x: c[0], y: c[1], z: c[2] }, 900);
}

async function loadGraph() {
  state.raw = await api(`/graph?include_documents=${state.showDocs}`);
  const s = state.raw.stats;
  const types = Object.entries(s.nodes_by_type).map(([t, c]) => `${c} ${t}`).join(" · ");
  $("stats").textContent = `${types} — ${s.sourced_links} sourced facts`;
  setGraphData();
}

function refreshStyles() {
  const g = state.graph;
  if (!g) return;
  g.nodeColor(g.nodeColor()).linkColor(g.linkColor()).linkWidth(g.linkWidth())
    .linkDirectionalParticles(g.linkDirectionalParticles());
  for (const [id, sprite] of state.labels) {
    sprite.material.opacity = state.hlNodes.size && !state.hlNodes.has(id) ? 0.15 : 1;
    sprite.material.transparent = true;
  }
}

function highlight(ids, { fit = false } = {}) {
  if (!state.graph) return;
  state.hlNodes = new Set(ids);
  state.hlLinks = new Set(state.graph.graphData().links.filter(
    (l) => state.hlNodes.has(idOf(l.source)) && state.hlNodes.has(idOf(l.target))));
  refreshStyles();
  if (fit && state.hlNodes.size) fitView((n) => state.hlNodes.has(n.id));
}

function clearHighlight() {
  state.hlNodes.clear();
  state.hlLinks.clear();
  refreshStyles();
}

function neighbours(id) {
  const out = new Set([id]);
  for (const l of state.graph.graphData().links) {
    if (idOf(l.source) === id) out.add(idOf(l.target));
    if (idOf(l.target) === id) out.add(idOf(l.source));
  }
  return out;
}

function focusNode(n) {
  if (!state.graph) return;
  highlight(neighbours(n.id));
  if (state.mode === "3d") {
    const dist = 90;
    const r = 1 + dist / Math.hypot(n.x || 1, n.y || 1, n.z || 1);
    state.graph.cameraPosition({ x: n.x * r, y: n.y * r, z: n.z * r }, n, 1100);
  } else {
    state.graph.centerAt(n.x, n.y, 800);
    state.graph.zoom(3, 800);
  }
  showTab("details");
  showNode(n);
}

/* ------------------------------------------------------------- details tab */
function quoteBlock(ev) {
  return el("div", { className: "quote" }, `“${ev.quote}”`,
    el("span", { className: "source", text: `${ev.document} · page ${ev.page}` }));
}

async function showNode(n) {
  const box = $("details");
  box.replaceChildren(el("p", { className: "hint" }, el("span", { className: "spinner" }), "Loading…"));
  try {
    if (n.type === "Document") {
      const d = await api(`/documents/${encodeURIComponent(n.id)}`);
      box.replaceChildren(
        el("span", { className: "chip", style: `color:${COLORS.Document}`, text: "Document" }),
        el("h2", { text: d.filename }),
        el("p", { className: `muted st-${d.status}`, text: `status: ${d.status}` }),
        el("p", { className: "hint", text:
          `${d.chunks} chunks · ${d.embedded_chunks} embedded · ${d.facts} facts · ${d.pending_review} waiting for review` }),
        el("p", { className: "muted", text: `Mentions ${n.degree} entities (highlighted).` }),
      );
      return;
    }
    const e = await api(`/entities/${encodeURIComponent(n.id)}`);
    const manual = state.raw.links.filter((l) => !l.sourced && (l.source === n.id || l.target === n.id));
    const name = (id) => (state.raw.nodes.find((x) => x.id === id) || {}).name || id;
    box.replaceChildren(
      el("span", { className: "chip", style: `color:${COLORS[e.type]}`, text: e.type }),
      el("h2", { text: e.name }),
      e.aliases.length ? el("p", { className: "muted", text: `also written as: ${e.aliases.join(", ")}` }) : null,
      el("h3", { text: `Facts with evidence (${e.facts.length})` }),
      e.facts.length ? e.facts.map((f) => el("div", { className: "fact" },
        el("div", { className: "rel", text: f.fact }),
        f.evidence.map(quoteBlock))) : el("p", { className: "hint", text: "No document-backed facts yet." }),
      manual.length ? el("h3", { text: `Hand-entered, no source (${manual.length})` }) : null,
      manual.map((l) => el("div", { className: "fact manual" },
        el("div", { className: "rel", text: `(${name(l.source)})-[:${l.type}]->(${name(l.target)})` }),
        el("span", { className: "source", text: "From the seed script — not cited in answers." }))),
    );
  } catch (err) {
    box.replaceChildren(el("p", { className: "error", text: `Could not load: ${err.message}` }));
  }
}

async function showLink(l) {
  const s = l.source, t = l.target;
  highlight([s.id, t.id]);
  showTab("details");
  const box = $("details");
  const head = [
    el("span", { className: "chip", text: l.type }),
    el("h2", { text: `${s.name} → ${t.name}` }),
    Object.keys(l.properties || {}).length
      ? el("p", { className: "muted", text: Object.entries(l.properties).map(([k, v]) => `${k}: ${v}`).join(" · ") })
      : null,
  ];
  if (l.type === "MENTIONS") {
    box.replaceChildren(...head, el("p", { className: "hint", text: "This document mentions this entity." }));
    return;
  }
  if (!l.sourced) {
    box.replaceChildren(...head, el("p", { className: "hint",
      text: "Hand-entered fact with no document behind it, so GraphRAG never uses it in answers." }));
    return;
  }
  box.replaceChildren(...head, el("p", { className: "hint" }, el("span", { className: "spinner" }), "Loading evidence…"));
  try {
    const e = await api(`/entities/${encodeURIComponent(s.id)}`);
    const fact = e.facts.find((f) => f.fact.includes(`[:${l.type}`) && f.fact.endsWith(`(${t.name})`));
    box.replaceChildren(...head, el("h3", { text: "Evidence" }),
      fact ? fact.evidence.map(quoteBlock) : el("p", { className: "hint", text: "Evidence not found." }));
  } catch (err) {
    box.replaceChildren(...head, el("p", { className: "error", text: err.message }));
  }
}

/* ----------------------------------------------------------------- ask tab */
async function ask(ev) {
  ev.preventDefault();
  const q = $("question").value.trim();
  if (q.length < 3) return;
  const btn = $("askBtn");
  btn.disabled = true;
  $("answer").replaceChildren(el("p", { className: "hint" }, el("span", { className: "spinner" }),
    "Thinking… the local model can take up to a minute on the first question."));
  try {
    const r = await api("/ask", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }) });
    const warn = [];
    if (r.warnings.unsupported_terms.length)
      warn.push(`⚠ Not found in the cited sources — check by hand: ${r.warnings.unsupported_terms.join(", ")}`);
    if (r.warnings.invalid_citations.length)
      warn.push(`⚠ The model also cited sources that don't exist: ${r.warnings.invalid_citations.join(", ")}`);
    $("answer").replaceChildren(el("div", { className: `answer-box ${r.grounded ? "grounded" : "abstained"}` },
      el("div", { className: "status-line",
        text: r.grounded ? "✓ Answered from your documents" : `⚠ Not answered — ${r.abstained_reason}` }),
      el("div", { text: r.answer }),
      warn.map((w) => el("div", { className: "warn", text: w })),
      r.citations.length ? el("h3", { text: "Sources" }) : null,
      r.citations.map((c) => el("div", { className: "cite" },
        el("b", { text: `[${c.id}] ` }), `${c.kind} · ${c.document} · page ${c.page}`,
        el("div", { className: "muted", text: c.text.slice(0, 220) }))),
      r.highlight_entity_ids.length ? el("p", { className: "muted", text: "The entities behind this answer are lit up in the graph." }) : null,
    ));
    if (r.highlight_entity_ids.length) highlight(r.highlight_entity_ids, { fit: true });
  } catch (err) {
    $("answer").replaceChildren(el("p", { className: "error", text: err.message }));
  } finally {
    btn.disabled = false;
  }
}

/* -------------------------------------------------------------- upload tab */
async function uploadFile(file) {
  const card = el("div", { className: "job" }, el("strong", { text: file.name }),
    el("div", { className: "muted", text: "uploading…" }), el("div", { className: "progress" }, el("div", { style: "width:3%" })));
  $("jobs").prepend(card);
  const [, statusLine, bar] = card.children;
  try {
    const form = new FormData();
    form.append("file", file);
    const r = await api("/documents", { method: "POST", body: form });
    if (r.duplicate) statusLine.textContent = `already uploaded (status: ${r.status})`;
    pollJob(r.document_id, statusLine, bar.firstChild);
  } catch (err) {
    statusLine.textContent = err.message;
    statusLine.className = "error";
  }
}

async function pollJob(id, line, bar) {
  try {
    const d = await api(`/documents/${encodeURIComponent(id)}`);
    let pct = 5;
    if (d.status === "processing") {
      const i = Math.max(0, STAGES.indexOf(d.stage));
      let within = 0;
      const m = /^(\d+)\/(\d+)/.exec(d.progress || "");
      if (m && d.stage === "extracting") within = Number(m[1]) / Number(m[2]);
      pct = 10 + ((i + within) / STAGES.length) * 85;
    } else if (FINAL.has(d.status)) pct = 100;
    bar.style.width = `${pct}%`;
    line.className = `muted st-${d.status}`;
    line.textContent = [d.status, d.stage, d.progress].filter(Boolean).join(" · ")
      + (d.status === "retry" && d.error ? ` (${d.error})` : "")
      + (FINAL.has(d.status) && d.error ? ` — ${d.error}` : "");
    if (!FINAL.has(d.status)) return setTimeout(() => pollJob(id, line, bar), 2500);
    if (d.status === "completed") {
      line.textContent = `completed · ${d.facts} facts · ${d.pending_review} for review`;
      toast(`${d.filename} is in the graph`);
      await Promise.all([loadGraph(), refreshDocs(), refreshReview()]);
    } else refreshDocs();
  } catch (err) {
    line.textContent = err.message;
    setTimeout(() => pollJob(id, line, bar), 5000);
  }
}

async function refreshDocs() {
  try {
    const docs = await api("/documents");
    $("docList").replaceChildren(...docs.map((d) => el("div", { className: "doc" },
      el("span", { className: "name", title: d.filename, text: d.filename }),
      el("span", { className: `muted st-${d.status}`, text: d.status || "?" }))));
  } catch { /* shown elsewhere */ }
}

/* -------------------------------------------------------------- review tab */
async function refreshReview() {
  try {
    const items = await api("/review");
    $("reviewCount").textContent = items.length || "";
    $("reviewList").replaceChildren(...(items.length ? items.map((it) => {
      let p = {};
      try { p = JSON.parse(it.payload); } catch { /* keep empty */ }
      const fact = p.type ? `(${p.source})-[:${p.type}]->(${p.target})` : (p.name ? `${p.name} (${p.type})` : "fact");
      return el("div", { className: "fact" },
        el("div", { className: "rel", text: fact }),
        el("div", { className: "warn", text: it.reason }),
        p.evidence ? quoteBlock({ quote: p.evidence, document: it.document, page: it.page })
                   : el("span", { className: "source", text: `${it.document} · page ${it.page}` }));
    }) : [el("p", { className: "hint", text: "Nothing waiting for review." })]));
  } catch { /* ignore */ }
}

/* ----------------------------------------------------------------- wiring */
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "review") refreshReview();
  if (name === "upload") refreshDocs();
}

function buildLegend() {
  $("legend").replaceChildren(
    ...Object.entries(COLORS).map(([t, c]) => el("span", {}, el("i", { style: `color:${c};background:${c}` }), t)),
    el("span", {}, el("i", { className: "line", style: "background:rgba(150,200,255,.9)" }), "sourced fact"),
    el("span", {}, el("i", { className: "line", style: "background:rgba(150,160,180,.4)" }), "hand-entered"),
  );
}

function showKeyModal(msg = "") {
  $("keyError").textContent = msg;
  $("keyModal").hidden = false;
  $("apiKey").focus();
}

async function start() {
  $("keyModal").hidden = true;
  createGraph();
  try {
    await loadGraph();
  } catch (err) {
    if (err.message !== "unauthorised") toast(`Could not load graph: ${err.message}`);
    return;
  }
  refreshDocs();
  refreshReview();
}

function init() {
  buildLegend();
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("mode3d").addEventListener("click", () => switchMode("3d"));
  $("mode2d").addEventListener("click", () => switchMode("2d"));
  $("showDocs").addEventListener("change", (e) => { state.showDocs = e.target.checked; loadGraph().catch(() => {}); });
  $("showManual").addEventListener("change", (e) => { state.showManual = e.target.checked; setGraphData(); });
  $("resetView").addEventListener("click", () => { clearHighlight(); fitView(); });
  $("search").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !state.graph) return;
    const q = e.target.value.trim().toLowerCase();
    const n = state.graph.graphData().nodes.find((x) =>
      x.name.toLowerCase().includes(q) || (x.aliases || []).some((a) => a.toLowerCase().includes(q)));
    n ? focusNode(n) : toast(`No entity matching “${e.target.value}”`);
  });
  $("askForm").addEventListener("submit", ask);
  $("question").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask(e); });

  const dz = $("dropzone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("drag");
    [...e.dataTransfer.files].forEach(uploadFile);
  });
  $("fileInput").addEventListener("change", (e) => { [...e.target.files].forEach(uploadFile); e.target.value = ""; });

  $("keyForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    state.key = $("apiKey").value.trim();
    try {
      await api("/graph");
      store("set", state.key);
      $("apiKey").value = "";
      start();
    } catch (err) {
      if (err.message !== "unauthorised") $("keyError").textContent = err.message;
    }
  });
  window.addEventListener("resize", () => state.graph && state.graph.width($("graph").clientWidth).height($("graph").clientHeight));

  state.key = store("get");
  state.key ? start() : showKeyModal();
}

function switchMode(mode) {
  if (mode === state.mode) return;
  state.mode = mode;
  $("mode3d").classList.toggle("active", mode === "3d");
  $("mode2d").classList.toggle("active", mode === "2d");
  createGraph();
}

init();
