const palette = {
  identity: "#a88bfa",
  relationship: "#f2a65a",
  axis: "#63c7d7",
  project: "#82c99a",
  boundary: "#e57c7c",
  procedural: "#6aa6e8",
  semantic: "#aaa5b4",
  episodic: "#d8c36e",
};

let state = { nodes: [], edges: [], timeline: [], query: "" };
let kindFilter = "all";
let selectedId = "";
let activeView = "graph";
let frozen = false;
let sim = null;
let packetLoaded = false;
let candidatesLoaded = false;
let graphVersion = 0;

const $ = (s) => document.querySelector(s);
const esc = (s) =>
  (s ?? "").toString().replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isActivated = (node) =>
  Boolean(node.activation?.primary ?? node.activation?.score);

async function load(q = "") {
  const res = await fetch("/api/state" + (q ? `?q=${encodeURIComponent(q)}` : ""));
  state = await res.json();
  $("#stats").textContent = `${state.stats.nodes} nodes · ${state.stats.edges} edges · ${state.stats.cues} cues`;
  $("#queryLabel").textContent = q ? `cue: ${q}` : "all active nodes";
  renderKinds();
  renderCards();
  renderTimeline();
  renderGraph();
}

function filteredNodes() {
  let nodes = state.nodes;
  if (kindFilter !== "all") nodes = nodes.filter((n) => n.kind === kindFilter);
  return nodes;
}

function visibleNodes() {
  const nodes = filteredNodes();
  if (state.query) {
    const active = nodes.filter(isActivated);
    return active.length ? active : nodes;
  }
  return nodes;
}

function renderKinds() {
  const counts = { all: state.nodes.length };
  state.nodes.forEach((n) => (counts[n.kind] = (counts[n.kind] || 0) + 1));
  $("#kinds").innerHTML = Object.entries(counts)
    .map(([k, v]) => `<div class="kind ${kindFilter === k ? "active" : ""}" data-kind="${esc(k)}"><span>${k === "all" ? "All memories" : k}</span><b>${v}</b></div>`)
    .join("");
  document.querySelectorAll(".kind").forEach((el) => {
    el.onclick = () => {
      kindFilter = el.dataset.kind;
      renderKinds();
      renderCards();
      renderGraph();
    };
  });
}

function renderCards() {
  const nodes = visibleNodes().slice();
  nodes.sort((a, b) => (b.activation?.score || b.priority / 100) - (a.activation?.score || a.priority / 100));
  $("#memoryCards").innerHTML =
    nodes
      .map(
        (n) => `<article class="memory-card ${esc(n.kind)} ${selectedId === n.id ? "selected" : ""}" data-id="${esc(n.id)}">
          <div class="card-head"><strong>${esc(n.title)}</strong><span class="score">${n.activation?.score ? n.activation.score.toFixed(2) : n.priority}</span></div>
          <p>${esc(n.summary)}</p>
          <div class="chips">${n.tags.slice(0, 5).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>
        </article>`
      )
      .join("") || '<div class="empty" style="padding:15px">Không có node trong filter này.</div>';
  document.querySelectorAll(".memory-card").forEach((el) => (el.onclick = () => inspect(el.dataset.id)));
}

function renderTimeline() {
  const html =
    state.timeline
      .map(
        (e) => `<div class="timeline-item">
          <time>${esc(e.occurred_at.slice(0, 10))}</time>
          <strong>${esc(e.title)}</strong>
          <p>${esc(e.summary)}</p>
        </div>`
      )
      .join("") || '<div class="empty">Chưa có timeline.</div>';
  $("#timeline").innerHTML = html;
  $("#timelineView").innerHTML = html;
}

async function renderPacket() {
  if (packetLoaded) return;
  const res = await fetch("/api/context");
  const packet = await res.json();
  $("#packetView").innerHTML = `<div class="packet-path">${esc(packet.path)}</div><pre>${esc(packet.content || "Chưa có context packet.")}</pre>`;
  packetLoaded = true;
}

async function renderCandidates() {
  const [pendingRes, tyReviewRes, heldRes] = await Promise.all([
    fetch("/api/candidates?status=pending"),
    fetch("/api/candidates?status=ty_review_required"),
    fetch("/api/candidates?status=held"),
  ]);
  const pending = (await pendingRes.json()).items || [];
  const tyReview = (await tyReviewRes.json()).items || [];
  const held = (await heldRes.json()).items || [];
  const cards = (items, empty, heldState = false) =>
    items
      .map(
        (item) => {
          const consensus = item.consensus || {};
          const votes = (item.attestations || [])
            .map((vote) => `<span class="chip">${esc(vote.reviewer_branch)}: ${esc(vote.decision)}</span>`)
            .join("");
          return `<article class="candidate-card ${heldState ? "held" : ""}">
          <div class="candidate-head">
            <strong>${esc(item.title)}</strong>
            <span class="sensitivity ${esc(item.sensitivity)}">${esc(item.sensitivity)}</span>
          </div>
          <p>${esc(item.summary)}</p>
          <div class="meta">${esc(item.kind)} · importance ${Number(item.importance || 0).toFixed(2)} · ${item.evidence_count} evidence · confidence ${item.confidence.toFixed(2)} · ${esc(item.source_ref)}</div>
          <div class="chips">${(item.capture_reasons || []).map((reason) => `<span class="chip">${esc(reason)}</span>`).join("")}</div>
          <div class="meta">quorum ${Number(consensus.approval_count || 0)}/${Number(consensus.quorum_required || 2)}${consensus.materialization_blocker ? ` · ${esc(consensus.materialization_blocker)}` : ""}</div>
          <div class="chips">${votes}</div>
          <div class="candidate-actions">
            <button type="button" data-action="approve" data-id="${esc(item.id)}">${heldState ? "Override anchor" : "Override materialize"}</button>
            <button type="button" class="reject" data-action="reject" data-id="${esc(item.id)}">${heldState ? "Discard" : "Correction reject"}</button>
          </div>
        </article>`;
        }
      )
      .join("") || `<div class="empty">${empty}</div>`;
  $("#candidatesView").innerHTML = `
    <section class="candidate-section">
      <h2>Pending review <span>${pending.length}</span></h2>
      ${cards(pending, "Không có memory candidate đang chờ.")}
    </section>
    <section class="candidate-section">
      <h2>owner-controlled review <span>${tyReview.length}</span></h2>
      ${cards(tyReview, "Không có candidate bị chặn để owner review.")}
    </section>
    <section class="candidate-section">
      <h2>Held observations <span>${held.length}</span></h2>
      ${cards(held, "Chưa có low-signal observation được giữ nền.", true)}
    </section>`;
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.onclick = async () => {
      await fetch(`/api/candidates/${encodeURIComponent(button.dataset.id)}/${button.dataset.action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: "Reviewed in LML dashboard" }),
      });
      candidatesLoaded = false;
      await renderCandidates();
      await load($("#search").value.trim());
    };
  });
  candidatesLoaded = true;
}

async function inspect(id) {
  selectedId = id;
  const res = await fetch(`/api/node?id=${encodeURIComponent(id)}`);
  const data = await res.json();
  const n = data.node;
  const active = state.nodes.find((node) => node.id === id)?.activation || {};
  $("#inspector").innerHTML = `<h2>${esc(n.title)}</h2>
    <div class="meta">${esc(n.kind)} · priority ${n.priority} · confidence ${n.confidence}<br>${esc(n.source_type)} · ${esc(n.source_ref)}</div>
    ${active.score ? `<div class="reason-box"><strong>Activation ${active.score.toFixed(2)}</strong>${(active.reasons || []).map((r) => `<span>${esc(r)}</span>`).join("")}</div>` : ""}
    <p>${esc(n.summary)}</p>
    <pre>${esc(n.content)}</pre>
    <div class="chips">${n.tags.map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>
    <h3>Connections</h3>
    ${data.neighbors.map((x) => `<div class="kind" data-node="${esc(x.id)}"><span>${esc(x.edge.relation)} → ${esc(x.title)}</span><b>${x.edge.weight}</b></div>`).join("")}`;
  document.querySelectorAll("[data-node]").forEach((el) => (el.onclick = () => inspect(el.dataset.node)));
  renderCards();
  renderGraph();
}

function makeSimulation(nodes, edges, svg) {
  const w = svg.clientWidth || 800;
  const h = svg.clientHeight || 420;
  const cx = w / 2;
  const cy = h / 2;
  const activeIds = new Set(nodes.filter(isActivated).map((n) => n.id));
  const activationOrder = [...nodes]
    .sort((a, b) => (b.activation?.score || 0) - (a.activation?.score || 0))
    .map((n) => n.id);
  const activationRank = new Map(activationOrder.map((id, index) => [id, index]));
  const nodeMap = new Map(nodes.map((n, i) => {
    const old = sim?.nodeMap?.get(n.id);
    const rank = activationRank.get(n.id) ?? i;
    const hasCue = activeIds.size > 0;
    const innerCount = Math.min(6, nodes.length);
    const inner = hasCue && rank < innerCount;
    const ringIndex = inner ? rank : hasCue ? rank - innerCount : i;
    const ringCount = inner ? innerCount : Math.max(1, nodes.length - (hasCue ? innerCount : 0));
    const angle = -Math.PI / 2 + (ringIndex / ringCount) * Math.PI * 2 + (inner ? 0 : Math.PI / Math.max(2, ringCount));
    const radiusX = hasCue ? (inner ? w * 0.17 : w * 0.32) : w * 0.3;
    const radiusY = hasCue ? (inner ? h * 0.23 : h * 0.36) : h * 0.34;
    const homeX = cx + Math.cos(angle) * radiusX;
    const homeY = cy + Math.sin(angle) * radiusY;
    const node = {
      ...n,
      x: old?.x ?? homeX,
      y: old?.y ?? homeY,
      homeX,
      homeY,
      vx: old?.vx ?? 0,
      vy: old?.vy ?? 0,
      r: 8 + n.priority / 20 + (isActivated(n) ? Math.min(7, n.activation.score * 1.45) : 0),
      active: activeIds.size === 0 || activeIds.has(n.id),
      labelRank: rank,
    };
    return [n.id, node];
  }));
  const links = edges.map((e) => ({ ...e, source: nodeMap.get(e.src_id), target: nodeMap.get(e.dst_id) })).filter((e) => e.source && e.target);
  return { nodes: [...nodeMap.values()], links, nodeMap, w, h, cx, cy, scale: sim?.scale ?? 1, panX: sim?.panX ?? 0, panY: sim?.panY ?? 0 };
}

function stepSimulation() {
  if (!sim || frozen) return;
  const activeCount = sim.nodes.filter(isActivated).length;
  const rotate = activeCount ? 0.00045 : 0.00075;
  for (const link of sim.links) {
    const dx = link.target.x - link.source.x;
    const dy = link.target.y - link.source.y;
    const dist = Math.hypot(dx, dy) || 1;
    const target = 118 + (1 - link.weight) * 34;
    const force = (dist - target) * 0.0028 * link.weight;
    const fx = (dx / dist) * force;
    const fy = (dy / dist) * force;
    link.source.vx += fx;
    link.source.vy += fy;
    link.target.vx -= fx;
    link.target.vy -= fy;
  }
  for (let i = 0; i < sim.nodes.length; i++) {
    const a = sim.nodes[i];
    for (let j = i + 1; j < sim.nodes.length; j++) {
      const b = sim.nodes[j];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 1;
      const min = a.r + b.r + 20;
      const push = Math.min(2.8, 1800 / (dist * dist));
      if (dist < min || push > 0.02) {
        const f = dist < min ? (min - dist) * 0.018 : push * 0.018;
        const fx = (dx / dist) * f;
        const fy = (dy / dist) * f;
        a.vx -= fx;
        a.vy -= fy;
        b.vx += fx;
        b.vy += fy;
      }
    }
  }
  for (const n of sim.nodes) {
    const focus = activeCount ? (n.active ? 0.016 : 0.004) : 0.011;
    const targetX = activeCount
      ? n.active
        ? n.homeX
        : sim.cx + Math.sign(n.homeX - sim.cx) * sim.w * 0.38
      : n.homeX;
    const targetY = activeCount
      ? n.active
        ? n.homeY
        : sim.cy + Math.sign(n.homeY - sim.cy) * sim.h * 0.32
      : n.homeY;
    const rx = n.x - sim.cx;
    const ry = n.y - sim.cy;
    n.vx += (targetX - n.x) * focus + -ry * rotate;
    n.vy += (targetY - n.y) * focus + rx * rotate;
    n.vx *= 0.88;
    n.vy *= 0.88;
    if (n.fx == null) n.x += n.vx;
    if (n.fy == null) n.y += n.vy;
  }
}

function renderGraph() {
  const version = ++graphVersion;
  const svg = $("#graph");
  svg.innerHTML = "";
  const nodes = filteredNodes().slice(0, 60);
  const ids = new Set(nodes.map((n) => n.id));
  const edges = state.edges.filter((e) => ids.has(e.src_id) && ids.has(e.dst_id));
  sim = makeSimulation(nodes, edges, svg);

  const NS = "http://www.w3.org/2000/svg";
  const viewport = document.createElementNS(NS, "g");
  svg.appendChild(viewport);
  const edgeLayer = document.createElementNS(NS, "g");
  const nodeLayer = document.createElementNS(NS, "g");
  viewport.append(edgeLayer, nodeLayer);

  const edgeEls = sim.links.map((e) => {
    const line = document.createElementNS(NS, "line");
    line.setAttribute("class", "edge");
    line.setAttribute("stroke-width", String(0.7 + e.weight * 1.4));
    const title = document.createElementNS(NS, "title");
    title.textContent = e.relation;
    line.appendChild(title);
    edgeLayer.appendChild(line);
    return [e, line];
  });
  const nodeEls = sim.nodes.map((n) => {
    const g = document.createElementNS(NS, "g");
    g.setAttribute(
      "class",
      `graph-node ${n.active ? "active-cluster" : "dimmed"} ${n.labelRank < 3 ? "labelled" : ""} ${selectedId === n.id ? "selected" : ""}`
    );
    g.dataset.id = n.id;
    const title = document.createElementNS(NS, "title");
    title.textContent = `${n.title} · ${n.kind} · priority ${n.priority}`;
    const c = document.createElementNS(NS, "circle");
    c.setAttribute("r", n.r);
    c.setAttribute("fill", palette[n.kind] || "#aaa5b4");
    c.setAttribute("class", "node");
    const t = document.createElementNS(NS, "text");
    const labelOnRight = n.homeX >= sim.cx;
    t.setAttribute("x", labelOnRight ? -(n.r + 6) : n.r + 6);
    t.setAttribute("y", 4);
    t.setAttribute("text-anchor", labelOnRight ? "end" : "start");
    t.setAttribute("class", "node-label");
    t.textContent = n.title.length > 30 ? n.title.slice(0, 28) + "..." : n.title;
    g.append(title, c, t);
    g.onclick = () => inspect(n.id);
    enableDrag(g, n, svg);
    nodeLayer.appendChild(g);
    return [n, g];
  });

  enableZoom(svg, viewport);
  function paint() {
    if (version !== graphVersion) return;
    stepSimulation();
    viewport.setAttribute("transform", `translate(${sim.panX},${sim.panY}) scale(${sim.scale})`);
    for (const [e, line] of edgeEls) {
      line.setAttribute("x1", e.source.x);
      line.setAttribute("y1", e.source.y);
      line.setAttribute("x2", e.target.x);
      line.setAttribute("y2", e.target.y);
    }
    for (const [n, g] of nodeEls) {
      g.setAttribute("transform", `translate(${n.x},${n.y})`);
    }
    requestAnimationFrame(paint);
  }
  requestAnimationFrame(paint);
  setView(activeView);
}

function pointerPoint(evt) {
  const rect = $("#graph").getBoundingClientRect();
  return { x: (evt.clientX - rect.left - sim.panX) / sim.scale, y: (evt.clientY - rect.top - sim.panY) / sim.scale };
}

function enableDrag(el, node, svg) {
  el.addEventListener("pointerdown", (evt) => {
    evt.preventDefault();
    el.setPointerCapture(evt.pointerId);
    const p = pointerPoint(evt);
    node.fx = p.x;
    node.fy = p.y;
  });
  el.addEventListener("pointermove", (evt) => {
    if (node.fx == null) return;
    const p = pointerPoint(evt);
    node.x = node.fx = p.x;
    node.y = node.fy = p.y;
    node.vx = 0;
    node.vy = 0;
  });
  el.addEventListener("pointerup", () => {
    node.fx = null;
    node.fy = null;
  });
}

function enableZoom(svg) {
  let panning = false;
  let last = null;
  svg.onwheel = (evt) => {
    evt.preventDefault();
    const factor = evt.deltaY > 0 ? 0.92 : 1.08;
    sim.scale = Math.max(0.45, Math.min(2.3, sim.scale * factor));
  };
  svg.onpointerdown = (evt) => {
    if (evt.target.closest(".graph-node")) return;
    panning = true;
    last = { x: evt.clientX, y: evt.clientY };
    svg.setPointerCapture(evt.pointerId);
  };
  svg.onpointermove = (evt) => {
    if (!panning || !last) return;
    sim.panX += evt.clientX - last.x;
    sim.panY += evt.clientY - last.y;
    last = { x: evt.clientX, y: evt.clientY };
  };
  svg.onpointerup = () => {
    panning = false;
    last = null;
  };
}

function focusCluster() {
  if (!sim) return;
  for (const n of sim.nodes) {
    if (n.active) {
      n.vx += (sim.cx - n.x) * 0.08;
      n.vy += (sim.cy - n.y) * 0.08;
    }
  }
}

function resetView() {
  if (!sim) return;
  sim.scale = 1;
  sim.panX = 0;
  sim.panY = 0;
  renderGraph();
}

function setView(view) {
  activeView = view;
  document.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#graph").hidden = view !== "graph";
  $("#timelineView").hidden = view !== "timeline";
  $("#packetView").hidden = view !== "packet";
  $("#candidatesView").hidden = view !== "candidates";
  $("#graph").style.display = view === "graph" ? "block" : "none";
  $("#timelineView").style.display = view === "timeline" ? "block" : "none";
  $("#packetView").style.display = view === "packet" ? "block" : "none";
  $("#candidatesView").style.display = view === "candidates" ? "block" : "none";
  if (view === "packet") renderPacket();
  if (view === "candidates" && !candidatesLoaded) renderCandidates();
}

$("#searchForm").addEventListener("submit", (e) => {
  e.preventDefault();
  packetLoaded = false;
  load($("#search").value.trim());
});
$("#freezeBtn").onclick = () => {
  frozen = !frozen;
  $("#freezeBtn").textContent = frozen ? "Resume" : "Freeze";
};
$("#focusBtn").onclick = focusCluster;
$("#resetBtn").onclick = resetView;
document.querySelectorAll("[data-view]").forEach((b) => (b.onclick = () => setView(b.dataset.view)));
window.addEventListener("resize", () => renderGraph());
load();
