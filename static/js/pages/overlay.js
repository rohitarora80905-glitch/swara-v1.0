/* SETU Overlay — distance-vector relay selection over a measured mesh */
(function () {
  const { h, esc, fmt } = SW;
  let ui = {}, D = null, sel = null;

  async function run() {
    try {
      D = await SW.api("/api/tools/overlay", { body: { nodes: ui.n.get(), bad_frac: ui.bad.get() / 100, seed: parseInt(ui.seed.value || "4", 10) } });
      const best = [...D.pairs].sort((a, b) => b.gain_pct - a.gain_pct)[0];
      sel = best ? best.pair : null;
      render();
    } catch (e) { SW.toast(e.message, "bad"); }
  }
  const runSoon = SW.debounce(run, 200);

  function render() {
    const W = 640, H = 520, cx = W / 2, cy = H / 2, R = 200;
    const pos = {}; D.nodes.forEach((n) => (pos[n.id] = [cx + n.x * R, cy + n.y * R]));
    const pair = D.pairs.find((p) => p.pair === sel);
    const [sa, sb] = pair ? pair.pair.split("->") : [null, null];
    const onPath = new Set();
    if (pair) for (let i = 0; i + 1 < pair.path.length; i++) onPath.add([pair.path[i], pair.path[i + 1]].sort().join("|"));
    let s = `<svg class="graph" viewBox="0 0 ${W} ${H}" width="100%" style="max-height:560px"><defs><filter id="glow"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>`;
    D.links.forEach((l) => {
      const [x1, y1] = pos[l.a], [x2, y2] = pos[l.b];
      const key = [l.a, l.b].sort().join("|");
      const direct = pair && ((l.a === sa && l.b === sb) || (l.a === sb && l.b === sa));
      if (onPath.has(key)) return;
      s += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${direct ? "#fb7185" : l.congested ? "rgba(251,113,133,.35)" : "rgba(100,116,139,.28)"}" stroke-width="${direct ? 2.5 : 1.2}" ${l.congested || direct ? 'stroke-dasharray="6 5"' : ""}><title>${l.a} — ${l.b}: ${l.owd.toFixed(0)} ms, ±${l.jitter.toFixed(0)} ms, ${(l.loss * 100).toFixed(1)}% loss → cost ${l.cost.toFixed(0)}</title></line>`;
    });
    D.links.forEach((l) => {
      const key = [l.a, l.b].sort().join("|");
      if (!onPath.has(key)) return;
      const [x1, y1] = pos[l.a], [x2, y2] = pos[l.b];
      s += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#2dd4bf" stroke-width="5" stroke-linecap="round" filter="url(#glow)"/>`;
      s += `<text x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 8}" text-anchor="middle" style="fill:#5eead4;font-size:11px">${l.cost.toFixed(0)}</text>`;
    });
    D.nodes.forEach((n) => {
      const [x, y] = pos[n.id], on = n.id === sa || n.id === sb, relay = pair && pair.path.includes(n.id) && !on;
      const lx = cx + n.x * (R + 34), ly = cy + n.y * (R + 34);
      s += `<g class="node ${on ? "sel" : ""}"><circle cx="${x}" cy="${y}" r="${on ? 13 : 10}" ${relay ? 'style="fill:#0e3b38;stroke:#5eead4;stroke-width:3"' : ""}/><text x="${lx}" y="${ly + 4}" text-anchor="middle">${esc(n.id)}${relay ? " (relay)" : ""}</text></g>`;
    });
    s += `</svg>`;
    ui.graph.innerHTML = s;
    ui.kpi.innerHTML = [
      ["Pairs improved by a relay", `${D.improved}<small>/ ${D.total_pairs}</small>`, `${fmt.pct((100 * D.improved) / Math.max(1, D.total_pairs), 0)} of pairs`, "var(--accent)"],
      ["Mean cost reduction", fmt.pct(D.mean_gain, 1), "on improved pairs", "var(--ok)"],
      ["Converged in", `${D.rounds}<small>rounds</small>`, `${fmt.n(D.converge_ms, 2)} ms of CPU`, "var(--info)"],
      ["Links measured", D.links.length, `${D.links.filter((l) => l.congested).length} through a congested exchange`, "var(--conceal)"],
    ].map(([l, v, s2, c]) => `<div class="kpi" style="--kc:${c}"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${s2}</div></div>`).join("");
    ui.detail.innerHTML = pair ? `<div class="row wrap" style="gap:8px"><b>${esc(sa)} → ${esc(sb)}</b>${pair.hops > 1 ? `<span class="badge ok">relayed · ${pair.hops} hops</span>` : '<span class="badge">direct is best</span>'}</div>
      <dl class="kv mt-s"><dt>Direct path cost</dt><dd>${pair.direct_cost.toFixed(1)}</dd><dt>SETU path cost</dt><dd>${pair.setu_cost.toFixed(1)}</dd><dt>Route</dt><dd>${pair.path.map(esc).join(" → ")}</dd><dt>Improvement</dt><dd style="color:${pair.gain_pct > 1 ? "var(--ok)" : "inherit"}">${fmt.pct(pair.gain_pct, 1)}</dd></dl>` : "";
    const rows = [...D.pairs].sort((a, b) => b.gain_pct - a.gain_pct);
    ui.table.innerHTML = rows.map((p) => `<tr data-p="${esc(p.pair)}" class="${p.pair === sel ? "sel" : ""}"><td>${esc(p.pair.replace("->", " → "))}</td><td class="num">${p.direct_cost.toFixed(0)}</td><td class="num">${p.setu_cost.toFixed(0)}</td><td class="num">${p.hops}</td><td class="num" style="color:${p.gain_pct > 1 ? "var(--ok)" : "var(--dim)"}">${fmt.pct(p.gain_pct, 1)}</td></tr>`).join("");
    SW.$$("tr[data-p]", ui.table).forEach((tr) => (tr.onclick = () => { sel = tr.dataset.p; render(); }));
  }

  SW.page("overlay", {
    title: "SETU Overlay",
    group: "Protocol Toolkit",
    mount(el) {
      el.innerHTML = `
      <div class="split">
        <div class="stack">
          <div class="card"><div class="card-h"><h3>Topology</h3></div><div class="card-b" id="o-cfg"></div></div>
          <div class="card"><div class="card-h"><h3>Link cost</h3></div><div class="card-b small">
            <div class="code" style="padding-right:13px">cost = 1.0 · owd_ms
     + 2.0 · jitter_ms
     + 45 · (−10·log₁₀(1 − loss))
relay hop penalty = 12</div>
            <div class="muted mt-s">Jitter is weighted above raw delay because it forces a deeper playout buffer. Split horizon with poisoned reverse prevents count-to-infinity.</div></div></div>
        </div>
        <div class="stack">
          <div class="grid g4" id="o-kpi"></div>
          <div class="grid" style="grid-template-columns:minmax(0,1.4fr) minmax(0,1fr)">
            <div class="card"><div class="card-h"><h3>Overlay mesh</h3><div class="right legend"><span><i class="swatch" style="background:#2dd4bf"></i>SETU route</span><span><i class="swatch" style="background:#fb7185"></i>direct / congested</span></div></div><div class="card-b" id="o-graph"></div></div>
            <div class="stack">
              <div class="card"><div class="card-b" id="o-detail"></div></div>
              <div class="card"><div class="card-h"><h3>All pairs</h3><span class="sub">click to trace</span></div><div class="table-wrap" style="max-height:340px;overflow-y:auto"><table class="t click"><thead><tr><th>Pair</th><th class="num">Direct</th><th class="num">SETU</th><th class="num">Hops</th><th class="num">Gain</th></tr></thead><tbody id="o-table"></tbody></table></div></div>
            </div>
          </div>
        </div>
      </div>`;
      const cfg = el.querySelector("#o-cfg");
      ui.n = SW.slider({ label: "Relay nodes", min: 3, max: 12, value: 7, onInput: runSoon });
      ui.bad = SW.slider({ label: "Links through a congested exchange", min: 0, max: 80, value: 28, fmt: (v) => v + "%", onInput: runSoon });
      cfg.append(ui.n.el, ui.bad.el);
      const sd = h(`<div class="field"><label>Seed</label><div class="row"><input type="number" value="4"><button class="btn sm">New topology</button></div></div>`);
      ui.seed = sd.querySelector("input"); ui.seed.oninput = runSoon;
      sd.querySelector("button").onclick = () => { ui.seed.value = Math.floor(Math.random() * 9999); run(); };
      cfg.appendChild(sd);
      ui.graph = el.querySelector("#o-graph"); ui.kpi = el.querySelector("#o-kpi"); ui.detail = el.querySelector("#o-detail"); ui.table = el.querySelector("#o-table");
      run();
    },
  });
})();
