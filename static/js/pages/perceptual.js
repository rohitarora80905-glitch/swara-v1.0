/* Perceptual Analyzer — what swara/perceptual.py thinks each 20 ms is worth */
(function () {
  const { h, esc, fmt } = SW;
  const LCOL = { silence: "#475569", unvoiced: "#60a5fa", voiced: "#2dd4bf", onset: "#c084fc", plosive: "#f59e0b" };
  let ui = {}, D = null, audio = null, raf = 0, hover = -1;

  function size(c) {
    const w = c.clientWidth * devicePixelRatio, hh = c.clientHeight * devicePixelRatio;
    if (c.width !== w || c.height !== hh) { c.width = w; c.height = hh; }
    return [w, hh];
  }
  function playFrac() { return audio && audio.duration ? audio.currentTime / audio.duration : -1; }

  function draw() {
    if (!D) return;
    const F = D.frames, n = F.length;
    // waveform
    let [w, H] = size(ui.wave); let ctx = ui.wave.getContext("2d"); const mid = H / 2, bw = w / n;
    ctx.clearRect(0, 0, w, H);
    const peak = Math.max(0.05, ...F.map((f) => Math.max(Math.abs(f.min), Math.abs(f.max))));
    F.forEach((f, i) => {
      const a = (f.max / peak) * (mid - 4), b = (f.min / peak) * (mid - 4);
      ctx.fillStyle = SW.alpha(LCOL[f.label] || "#94a3b8", 0.35 + (f.pi / 255) * 0.65);
      ctx.fillRect(i * bw, mid - a, Math.max(1, bw - 0.5), Math.max(1, a - b));
    });
    // PI line
    ctx.beginPath(); ctx.strokeStyle = "rgba(255,255,255,.75)"; ctx.lineWidth = 1.3 * devicePixelRatio;
    F.forEach((f, i) => { const y = H - 3 - (f.pi / 255) * (H - 8); i ? ctx.lineTo(i * bw + bw / 2, y) : ctx.moveTo(bw / 2, y); });
    ctx.stroke();
    const pf = playFrac();
    const marker = (c, x, col) => { const cx = c.getContext("2d"); cx.fillStyle = col; cx.fillRect(x, 0, 2 * devicePixelRatio, c.height); };
    if (hover >= 0) { ctx.fillStyle = "rgba(255,255,255,.08)"; ctx.fillRect(hover * bw, 0, Math.max(2, bw), H); }
    if (pf >= 0) marker(ui.wave, pf * w, "#fbbf24");

    // truth + plan strips
    [w, H] = size(ui.plan); ctx = ui.plan.getContext("2d"); ctx.clearRect(0, 0, w, H);
    const row = H / 3;
    F.forEach((f, i) => {
      if (f.truth) { ctx.fillStyle = LCOL[f.truth] || "#94a3b8"; ctx.fillRect(i * bw, 2, Math.max(1, bw), row - 4); }
      ctx.fillStyle = LCOL[f.label]; ctx.fillRect(i * bw, row + 2, Math.max(1, bw), row - 4);
    });
    D.groups.forEach((g) => {
      const x0 = g.start * bw, gw = bw * ui.k.get();
      if (g.r > 0) {
        const pw = Math.min(gw / Math.max(1, g.r), 8 * devicePixelRatio);
        for (let j = 0; j < g.r; j++) { ctx.fillStyle = g.burst_mode ? "#fb923c" : "#f59e0b"; ctx.fillRect(x0 + j * (pw + 1), 2 * row + 4, Math.max(1, pw - 1), row - 8); }
      }
      g.anchors.forEach(([idx]) => { ctx.fillStyle = "#c084fc"; ctx.beginPath(); ctx.arc((g.start + idx + 0.5) * bw, 2 * row + row / 2, Math.max(2, Math.min(4 * devicePixelRatio, bw)), 0, 7); ctx.fill(); });
    });
    if (pf >= 0) marker(ui.plan, pf * w, "#fbbf24");
  }

  function inspect(i) {
    if (!D || i < 0 || i >= D.frames.length) { ui.insp.innerHTML = `<div class="small muted">Hover the waveform to inspect a frame.</div>`; return; }
    const f = D.frames[i], k = ui.k.get(), g = D.groups[Math.floor(i / k)];
    ui.insp.innerHTML = `<div class="row between"><b>Frame ${i}</b><span class="mono small muted">${(i * 0.02).toFixed(2)} s</span></div>
      <div class="row mt-s"><span class="badge" style="color:${LCOL[f.label]}">${f.label}</span>${f.truth ? `<span class="small muted">truth: ${f.truth}</span>` : ""}</div>
      <div class="mt-s" style="font-size:30px;font-weight:800;color:${LCOL[f.label]}">${f.pi}<small class="muted" style="font-size:12px;font-weight:600"> / 255 PI</small></div>
      <dl class="kv mt-s"><dt>Energy</dt><dd>${f.energy_db} dB</dd><dt>Voicing</dt><dd>${f.voicing}</dd><dt>Spectral flux</dt><dd>${f.flux}</dd><dt>Onset</dt><dd>${f.onset}</dd><dt>Zero-crossing</dt><dd>${f.zcr}</dd>
      <dt>Loss damage weight</dt><dd>${f.damage}</dd>${g ? `<dt>Group plan</dt><dd>r=${g.r}${g.anchors.length ? `, ${g.anchors.length} anchor` : ""}${g.burst_mode ? " · burst" : ""}</dd>` : ""}</dl>`;
  }

  function render() {
    const S = D.stats, n = D.frames.length;
    const sil = S.silence || { share: 0, pi_share: 0 };
    ui.kp.sec.set(`${fmt.n(D.seconds, 1)}<small>s</small>`, `${n} frames of 20 ms`);
    ui.kp.sil.set(`${fmt.n(sil.share * 100, 0)}<small>%</small> → ${fmt.n(sil.pi_share * 100, 1)}<small>%</small>`, "silence: frames → importance");
    const sp = D.frames.filter((f) => f.label !== "silence");
    ui.kp.pi.set(fmt.n(sp.reduce((a, f) => a + f.pi, 0) / Math.max(1, sp.length), 0), "mean PI of speech frames");
    ui.kp.par.set(fmt.int(D.parity_total), `parity packets for ${D.groups.length} groups`);
    ui.kp.anc.set(fmt.int(D.anchors_total), "anchor replicas planned");
    ui.kp.ovh.set(fmt.pct(D.realised_overhead * 100, 1), "realised overhead");
    ui.stats.innerHTML = ["onset", "voiced", "unvoiced", "silence"].filter((l) => S[l]).map((l) => `<tr><td><i class="swatch" style="background:${LCOL[l]}"></i> ${l}</td><td class="num">${S[l].n}</td><td class="num">${fmt.pct(S[l].share * 100)}</td><td class="num">${fmt.n(S[l].mean_pi, 0)}</td><td class="num">${fmt.pct(S[l].pi_share * 100)}</td></tr>`).join("");
    SW.chart(ui.hist, {
      type: "bar",
      data: { labels: Array.from({ length: 16 }, (_, i) => `${i * 16}`), datasets: ["onset", "voiced", "unvoiced", "silence"].map((l) => ({ label: l, backgroundColor: LCOL[l], data: Array.from({ length: 16 }, (_, b) => D.frames.filter((f) => f.label === l && Math.min(15, Math.floor(f.pi / 16)) === b).length) })) },
      options: { scales: { x: SW.axis("PI bucket", { stacked: true, grid: { display: false } }), y: SW.axis("frames", { stacked: true }) }, plugins: { legend: { position: "top", align: "end" } } },
    });
    if (audio) { audio.pause(); URL.revokeObjectURL(audio.src); }
    audio = new Audio(URL.createObjectURL(SW.wavBlob(SW.b64ToInt16(D.audio))));
    audio.onended = () => { ui.play.innerHTML = "▶ Play"; cancelAnimationFrame(raf); draw(); };
    ui.play.disabled = false; ui.play.innerHTML = "▶ Play";
    ui.out.classList.remove("hidden"); ui.intro.classList.add("hidden");
    draw(); inspect(-1);
  }

  async function analyze() {
    ui.go.disabled = true; ui.go.textContent = "Analysing…";
    try {
      const fec = `k=${ui.k.get()}&rho=${ui.rho.get() / 100}&burst=${ui.burst.get()}`;
      if (ui.src.get() === "clip") {
        if (!SW.clip) { SW.toast("Record or upload a clip first", "warn"); return; }
        const pcm = SW.clip.pcm.slice(0, 16000 * 20);
        D = await SW.api(`/api/tools/perceptual/pcm?${fec}`, { raw: pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength) });
      } else {
        D = await SW.api("/api/tools/perceptual", { body: { seconds: ui.secs.get(), seed: parseInt(ui.seed.value || "7", 10), k: ui.k.get(), rho: ui.rho.get() / 100, burst: ui.burst.get() } });
      }
      render();
    } catch (e) { SW.toast(e.message, "bad"); }
    finally { ui.go.disabled = false; ui.go.textContent = "Analyse"; }
  }

  SW.page("perceptual", {
    title: "Perceptual Analyzer",
    group: "Protocol Toolkit",
    mount(el) {
      el.innerHTML = `
      <div class="split">
        <div class="stack">
          <div class="card"><div class="card-h"><h3>Input</h3></div><div class="card-b" id="p-cfg"></div></div>
          <div class="note small">The analyser is <b>causal</b>: it uses only the current frame and state from the past, because a real encoder cannot look ahead without adding delay. Each frame's score travels in the authenticated-but-unencrypted <b>PI octet</b> of the header, so relays and DiffServ routers can act on it without reading the speech.</div>
        </div>
        <div class="stack">
          <div class="card" id="p-intro"><div class="card-b">${SW.empty("Score speech by audibility", "Analyse generated speech, or record your own voice and see how SWARA would protect it.", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2 13a2 2 0 0 0 2-2V7a2 2 0 0 1 4 0v13a2 2 0 0 0 4 0V4a2 2 0 0 1 4 0v13a2 2 0 0 0 4 0v-4a2 2 0 0 1 2-2"/></svg>')}</div></div>
          <div class="stack hidden" id="p-out">
            <div class="grid g6" id="p-kpis"></div>
            <div class="grid" style="grid-template-columns:minmax(0,1fr) 260px">
              <div class="card">
                <div class="card-h"><h3>Waveform coloured by class, white line = PI</h3><div class="right"><button class="btn sm" id="p-play" disabled>▶ Play</button></div></div>
                <div class="card-b">
                  <canvas class="strip" id="p-wave" style="height:190px;cursor:crosshair"></canvas>
                  <canvas class="strip mt-s" id="p-plan" style="height:66px"></canvas>
                  <div class="row wrap small muted mt-s" style="gap:14px">
                    ${Object.entries(LCOL).filter(([k]) => k !== "plosive").map(([k, c]) => `<span><i class="swatch" style="background:${c}"></i> ${k}</span>`).join("")}
                    <span><i class="swatch" style="background:#f59e0b"></i> parity packet</span><span><i class="swatch" style="background:#c084fc;border-radius:50%"></i> anchor replica</span>
                    <span class="dim">rows: ground truth · analyser label · FEC plan</span>
                  </div>
                </div>
              </div>
              <div class="card"><div class="card-h"><h3>Frame inspector</h3></div><div class="card-b" id="p-insp"></div></div>
            </div>
            <div class="grid g2">
              <div class="card"><div class="card-h"><h3>PI distribution</h3></div><div class="card-b"><div class="chart-box"><canvas id="p-hist"></canvas></div></div></div>
              <div class="card"><div class="card-h"><h3>By class</h3><span class="sub">share of frames vs share of perceptual weight</span></div>
                <div class="table-wrap"><table class="t"><thead><tr><th>Class</th><th class="num">Frames</th><th class="num">Share</th><th class="num">Mean PI</th><th class="num">PI share</th></tr></thead><tbody id="p-stats"></tbody></table></div></div>
            </div>
          </div>
        </div>
      </div>`;
      const cfg = el.querySelector("#p-cfg");
      const synth = h(`<div></div>`), clip = h(`<div class="hidden" style="margin-bottom:14px"></div>`);
      ui.src = SW.seg([["synthetic", "Synthetic speech"], ["clip", "My recording"]], "synthetic", (v) => { synth.classList.toggle("hidden", v !== "synthetic"); clip.classList.toggle("hidden", v !== "clip"); });
      cfg.appendChild(SW.field("Source", ui.src));
      ui.secs = SW.slider({ label: "Length", min: 2, max: 20, value: 8, fmt: (v) => v + " s" });
      synth.appendChild(ui.secs.el);
      const sd = h(`<div class="field"><label>Speaker seed</label><input type="number" value="7"></div>`); ui.seed = sd.querySelector("input"); synth.appendChild(sd);
      clip.appendChild(SW.clipWidget());
      cfg.appendChild(synth); cfg.appendChild(clip);
      cfg.appendChild(h(`<div class="divider"></div>`));
      cfg.appendChild(h(`<div class="group-title">FEC scheduler</div>`));
      ui.k = SW.slider({ label: "Group size k", min: 2, max: 12, value: 4, fmt: (v) => v + " frames" });
      ui.rho = SW.slider({ label: "Redundancy budget ρ", min: 0, max: 100, value: 30, fmt: (v) => v + "%" });
      ui.burst = SW.slider({ label: "Expected mean burst", min: 1, max: 10, step: 0.5, value: 2, fmt: (v) => v.toFixed(1), hint: "≥ group span switches the scheduler to burst mode (anchors instead of parity)" });
      [ui.k, ui.rho, ui.burst].forEach((s) => cfg.appendChild(s.el));
      ui.go = h(`<button class="btn primary lg block mt">Analyse</button>`); ui.go.onclick = analyze; cfg.appendChild(ui.go);

      ui.wave = el.querySelector("#p-wave"); ui.plan = el.querySelector("#p-plan"); ui.insp = el.querySelector("#p-insp");
      ui.hist = el.querySelector("#p-hist"); ui.stats = el.querySelector("#p-stats"); ui.out = el.querySelector("#p-out"); ui.intro = el.querySelector("#p-intro");
      ui.play = el.querySelector("#p-play");
      const kp = el.querySelector("#p-kpis");
      ui.kp = { sec: SW.kpi("Analysed", "var(--accent)"), sil: SW.kpi("Silence", "#475569"), pi: SW.kpi("Speech importance", "var(--swara)"), par: SW.kpi("Parity planned", "var(--uniform)"), anc: SW.kpi("Anchors planned", "var(--replica)"), ovh: SW.kpi("Overhead", "var(--info)") };
      Object.values(ui.kp).forEach((k) => kp.appendChild(k.el));

      ui.wave.addEventListener("mousemove", (e) => {
        if (!D) return;
        const r = ui.wave.getBoundingClientRect();
        hover = Math.floor(((e.clientX - r.left) / r.width) * D.frames.length);
        inspect(hover); draw();
      });
      ui.wave.addEventListener("mouseleave", () => { hover = -1; draw(); });
      ui.wave.addEventListener("click", (e) => {
        if (!audio || !audio.duration) return;
        const r = ui.wave.getBoundingClientRect(); audio.currentTime = ((e.clientX - r.left) / r.width) * audio.duration;
        if (audio.paused) ui.play.click(); else draw();
      });
      ui.play.onclick = () => {
        if (!audio) return;
        if (audio.paused) {
          audio.play(); ui.play.innerHTML = "❚❚ Pause";
          const loop = () => { draw(); if (!audio.paused) raf = requestAnimationFrame(loop); };
          loop();
        } else { audio.pause(); ui.play.innerHTML = "▶ Play"; cancelAnimationFrame(raf); }
      };
      window.addEventListener("resize", SW.debounce(draw, 100));
      analyze();
    },
    show() { setTimeout(draw, 30); },
    hide() { if (audio && !audio.paused) { audio.pause(); ui.play.innerHTML = "▶ Play"; } },
  });
})();
