/* FEC Playground — Reed–Solomon erasure coding and the PI-AFEC scheduler */
(function () {
  const { h, esc, fmt } = SW;
  let ui = {}, lost = new Set(), last = null;

  async function encode() {
    const k = ui.k.get(), r = ui.r.get();
    [...lost].forEach((i) => i >= k + r && lost.delete(i));
    try {
      last = await SW.api("/api/tools/rs", { body: { message: ui.msg.value || " ", k, r, lost: [...lost] } });
      renderRS();
    } catch (e) { SW.toast(e.message, "bad"); }
  }
  const encodeSoon = SW.debounce(encode, 180);

  function renderRS() {
    const d = last;
    ui.shards.innerHTML = "";
    d.shards.forEach((s) => {
      const el = h(`<div class="shard ${s.kind} ${s.lost ? "lost" : ""} ${s.rebuilt ? "rebuilt" : ""}" title="${esc(s.hex)}">
        <div class="s-i">${s.kind === "data" ? "D" : "P"}${s.kind === "data" ? s.index : s.index - d.k}</div>
        <div class="s-k">${s.kind === "data" ? "data" : "parity"}</div>
        <div class="s-t">${s.kind === "data" ? esc(s.text.replace(/\0/g, "·")) : esc(s.hex.slice(0, 11))}</div></div>`);
      el.onclick = () => { lost.has(s.index) ? lost.delete(s.index) : lost.add(s.index); encode(); };
      ui.shards.appendChild(el);
    });
    const ok = d.success;
    const need = d.k, have = d.received;
    ui.res.innerHTML = `
      <div class="row wrap" style="gap:8px">
        <span class="badge ${ok ? "ok" : "bad"}">${ok ? (d.missing_data.length ? "RECOVERED" : "NOTHING LOST") : "UNRECOVERABLE"}</span>
        <span class="small muted">received <b class="mono" style="color:var(--text)">${have}</b> of ${d.n} shards · need any <b class="mono" style="color:var(--text)">${need}</b></span>
      </div>
      <div class="progress mt-s" style="height:8px"><i style="width:${Math.min(100, (have / d.n) * 100)}%;background:${ok ? "var(--ok)" : "var(--bad)"}"></i></div>
      <div class="mt" style="font-size:12px" class="muted">Receiver's reconstruction:</div>
      <div class="code mt-s" style="padding-right:13px;color:${ok ? "#d1fae5" : "#fecaca"}">${ok ? esc(d.recovered) : `✕ ${d.missing_data.length} data shard${d.missing_data.length > 1 ? "s" : ""} lost and only ${have} of ${need} needed shards arrived — the frame must be concealed`}</div>
      <dl class="kv mt"><dt>Code</dt><dd>RS(${d.n}, ${d.k}) over GF(2⁸)</dd><dt>Shard length</dt><dd>${d.shard_len} B</dd><dt>Overhead</dt><dd>${fmt.pct(d.overhead_pct, 0)}</dd>
      <dt>Data shards rebuilt</dt><dd>${ok ? d.missing_data.length : 0}</dd><dt>Encode / decode</dt><dd>${fmt.n(d.encode_us, 0)} µs / ${fmt.n(d.decode_us, 0)} µs</dd></dl>`;
  }

  /* --- PI-AFEC scheduler --- */
  const PRESETS = {
    "Vowel onset": [12, 255, 210, 190],
    "Steady vowel": [180, 175, 182, 178],
    "Silence": [0, 3, 0, 5],
    "Fricative tail": [120, 105, 60, 8],
  };
  async function plan() {
    try {
      const pis = ui.pis.map((s) => s.get());
      const d = await SW.api("/api/tools/fec/plan", { body: { pis, rho: ui.prho.get() / 100, burst: ui.pburst.get() } });
      renderPlan(pis, d);
    } catch (e) { SW.toast(e.message, "bad"); }
  }
  const planSoon = SW.debounce(plan, 150);

  function renderPlan(pis, d) {
    const W = 560, H = 230, k = pis.length, cw = 90, gap = 18, x0 = 24, base = 170;
    const col = (p) => (p < 30 ? "#475569" : p < 120 ? "#60a5fa" : p < 200 ? "#2dd4bf" : "#c084fc");
    let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-height:260px">`;
    svg += `<text x="${x0}" y="16" fill="#8193b2" font-size="11" font-weight="700" letter-spacing="1">DATA FRAMES (height = PI)</text>`;
    pis.forEach((p, i) => {
      const x = x0 + i * (cw + gap), bh = 12 + (p / 255) * 110;
      svg += `<rect x="${x}" y="${base - bh}" width="${cw}" height="${bh}" rx="8" fill="${col(p)}" fill-opacity=".85"/>`;
      svg += `<text x="${x + cw / 2}" y="${base - bh - 8}" text-anchor="middle" fill="#e7eef9" font-size="13" font-weight="700">${p}</text>`;
      svg += `<text x="${x + cw / 2}" y="${base + 18}" text-anchor="middle" fill="#8193b2" font-size="11">frame ${i} · dmg ${d.damage[i].toFixed(2)}</text>`;
    });
    d.anchors.forEach(([idx, delay], j) => {
      const x = x0 + idx * (cw + gap) + cw / 2, y = base + 34 + j * 10;
      const tx = Math.min(W - 20, x + delay * (cw + gap) * 0.55);
      svg += `<path d="M${x} ${base + 26} Q ${(x + tx) / 2} ${y + 22} ${tx} ${base + 26}" stroke="#c084fc" stroke-width="2.2" fill="none" marker-end="url(#ah)"/>`;
      svg += `<text x="${(x + tx) / 2}" y="${y + 24}" text-anchor="middle" fill="#d8b4fe" font-size="11" font-weight="600">replica +${delay} frame${delay > 1 ? "s" : ""}</text>`;
    });
    const px = x0 + k * (cw + gap) - gap + 22;
    svg += `<line x1="${px - 11}" y1="30" x2="${px - 11}" y2="${base}" stroke="#2a3a58" stroke-dasharray="4 4"/>`;
    for (let j = 0; j < d.r; j++) svg += `<rect x="${px + j * 26}" y="${base - 40}" width="22" height="40" rx="5" fill="#f59e0b" fill-opacity=".85"/><text x="${px + j * 26 + 11}" y="${base - 15}" text-anchor="middle" fill="#1f1300" font-size="11" font-weight="800">P${j}</text>`;
    if (!d.r) svg += `<text x="${px}" y="${base - 15}" fill="#566684" font-size="12">no parity</text>`;
    svg += `<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#c084fc"/></marker></defs></svg>`;
    ui.planViz.innerHTML = svg;
    ui.planTxt.innerHTML = `<span class="badge ${d.burst_mode ? "warn" : "info"}">${d.burst_mode ? "BURST MODE" : "PARITY MODE"}</span>
      <span class="small muted">group mean importance <b class="mono" style="color:var(--text)">${d.mean_pi.toFixed(2)}</b> · ${d.r} parity · ${d.anchors.length} anchor${d.anchors.length === 1 ? "" : "s"} · overhead ${fmt.pct(d.overhead_ratio * 100, 0)}</span>`;
  }

  SW.page("fec", {
    title: "FEC Playground",
    group: "Protocol Toolkit",
    mount(el) {
      el.innerHTML = `
      <div class="grid g2">
        <div class="card">
          <div class="card-h"><h3>Reed–Solomon erasure code</h3><span class="sub">swara/fec.py · verified MDS over GF(2⁸)</span></div>
          <div class="card-b">
            <div class="field"><label>Payload</label><textarea rows="2" id="f-msg">SWARA: the same budget, spent where it is heard.</textarea></div>
            <div class="grid g2" id="f-kr"></div>
            <div class="row between mt-s"><div class="group-title" style="margin:0">Shards on the wire · click to drop</div>
              <div class="row"><button class="btn sm" id="f-rand">Random loss</button><button class="btn sm" id="f-burst">Burst</button><button class="btn sm ghost" id="f-clear">Clear</button></div></div>
            <div class="shards mt-s" id="f-shards"></div>
            <div class="divider"></div>
            <div id="f-res"></div>
          </div>
        </div>
        <div class="stack">
          <div class="card">
            <div class="card-h"><h3>PI-AFEC scheduler</h3><span class="sub">what one group of k = 4 frames earns</span></div>
            <div class="card-b">
              <div class="row wrap" id="f-presets" style="gap:6px;margin-bottom:12px"></div>
              <div class="grid g2" id="f-pis"></div>
              <div class="grid g2" id="f-pctl"></div>
              <div id="f-plantxt" class="row wrap" style="gap:8px"></div>
              <div id="f-plan" class="mt"></div>
            </div>
          </div>
          <div class="note small"><b>Why two mechanisms?</b> A systematic RS(k+r, k) group survives only if any k of its k+r packets arrive. When the mean loss burst approaches the group's own span, that fails almost every time the channel goes bad, so every parity byte is wasted. The scheduler then switches to <b>burst mode</b>: it sends time-displaced <b>anchor replicas</b> of the most important frames instead, timed to land after the burst but before the frame's playout deadline.</div>
        </div>
      </div>`;
      ui.msg = el.querySelector("#f-msg"); ui.shards = el.querySelector("#f-shards"); ui.res = el.querySelector("#f-res");
      ui.k = SW.slider({ label: "Data shards k", min: 1, max: 8, value: 4, onInput: encodeSoon });
      ui.r = SW.slider({ label: "Parity shards r", min: 0, max: 4, value: 2, onInput: encodeSoon });
      el.querySelector("#f-kr").append(ui.k.el, ui.r.el);
      ui.msg.addEventListener("input", encodeSoon);
      el.querySelector("#f-clear").onclick = () => { lost.clear(); encode(); };
      el.querySelector("#f-rand").onclick = () => { lost.clear(); const n = ui.k.get() + ui.r.get(); for (let i = 0; i < n; i++) if (Math.random() < 0.3) lost.add(i); encode(); };
      el.querySelector("#f-burst").onclick = () => { lost.clear(); const n = ui.k.get() + ui.r.get(), b = Math.min(n, ui.r.get() + 1), s = Math.floor(Math.random() * (n - b + 1)); for (let i = s; i < s + b; i++) lost.add(i); encode(); };
      lost.add(0); lost.add(2);
      encode();

      ui.pis = [12, 255, 210, 190].map((v, i) => SW.slider({ label: `Frame ${i} PI`, min: 0, max: 255, value: v, onInput: planSoon }));
      ui.pis.forEach((s) => el.querySelector("#f-pis").appendChild(s.el));
      ui.prho = SW.slider({ label: "Budget ρ", min: 0, max: 100, value: 30, fmt: (v) => v + "%", onInput: planSoon });
      ui.pburst = SW.slider({ label: "Mean burst", min: 1, max: 10, step: 0.5, value: 2, fmt: (v) => v.toFixed(1), onInput: planSoon });
      el.querySelector("#f-pctl").append(ui.prho.el, ui.pburst.el);
      Object.entries(PRESETS).forEach(([n, v]) => {
        const b = h(`<button class="chip" style="height:26px;font-size:11.5px">${n}</button>`);
        b.onclick = () => { v.forEach((x, i) => ui.pis[i].set(x)); plan(); };
        el.querySelector("#f-presets").appendChild(b);
      });
      ui.planViz = el.querySelector("#f-plan"); ui.planTxt = el.querySelector("#f-plantxt");
      plan();
    },
  });
})();
