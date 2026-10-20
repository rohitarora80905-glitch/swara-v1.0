/* Live Call Lab — a real, instrumented SWARA call streamed to the browser */
(function () {
  const { h, esc, fmt } = SW;
  const FATE_COL = ["#2dd4bf", "#60a5fa", "#c084fc", "#fb7185"];
  const FATE_NAME = ["delivered", "rebuilt (FEC)", "anchor replica", "concealed"];
  const STEPS = [["relay", "Relay"], ["auth", "Authenticate"], ["setup", "Call setup"], ["media", "Media"], ["done", "Complete"]];
  const ORDER = { queued: -1, relay: 0, auth: 1, setup: 2, media: 3, drain: 3, done: 4, error: 5 };

  const st = {
    run: null, es: null, frames: [], sentEnv: [], recvEnv: [], recvRem: new Int16Array(0),
    tel: [], counts: [0, 0, 0, 0], params: null, t0: 0, timer: 0, player: new SW.PcmPlayer(), history: [],
  };
  let ui = {};

  /* ---------- drawing ---------- */
  function sizeCanvas(c) {
    const w = c.clientWidth * devicePixelRatio, hh = c.clientHeight * devicePixelRatio;
    if (c.width !== w || c.height !== hh) { c.width = w; c.height = hh; }
    return [w, hh];
  }
  function drawFrames() {
    const c = ui.strip; if (!c || !c.clientWidth) return;
    const [w, H] = sizeCanvas(c), ctx = c.getContext("2d"), dpr = devicePixelRatio;
    ctx.clearRect(0, 0, w, H);
    const done = st.run && st.run.done;
    const F = st.frames;
    let bw, gap, start;
    if (done && F.length) { gap = 0; bw = w / F.length; start = 0; }
    else { bw = 5 * dpr; gap = 2 * dpr; start = Math.max(0, F.length - Math.floor(w / (bw + gap))); }
    // grid lines at PI 64/128/192
    ctx.strokeStyle = "rgba(60,80,120,.25)"; ctx.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach((f) => { ctx.beginPath(); ctx.moveTo(0, H - f * (H - 6)); ctx.lineTo(w, H - f * (H - 6)); ctx.stroke(); });
    for (let i = start; i < F.length; i++) {
      const [, pi, fate] = F[i];
      const x = (i - start) * (bw + gap);
      const fh = Math.max(2 * dpr, (pi / 255) * (H - 6));
      if (fate === 3) { ctx.fillStyle = "rgba(251,113,133,.13)"; ctx.fillRect(x, 0, Math.max(1, bw), H); }
      ctx.fillStyle = fate === 0 ? SW.alpha("#2dd4bf", 0.28 + (pi / 255) * 0.6) : FATE_COL[fate];
      ctx.fillRect(x, H - fh, Math.max(1, bw - (done ? 0 : 0)), fh);
    }
    if (!F.length) {
      ctx.fillStyle = "#566684"; ctx.font = `${12 * dpr}px Inter, sans-serif`; ctx.textAlign = "center";
      ctx.fillText("Frames appear here as the receiver plays them out", w / 2, H / 2);
    }
    drawWave(start, bw, gap, done);
  }
  function drawWave(start, bw, gap, done) {
    const c = ui.wave; if (!c || !c.clientWidth) return;
    const [w, H] = sizeCanvas(c), ctx = c.getContext("2d"), mid = H / 2;
    ctx.clearRect(0, 0, w, H);
    ctx.fillStyle = "rgba(60,80,120,.35)"; ctx.fillRect(0, mid, w, 1);
    const first = st.frames.length ? st.frames[0][0] : 0;
    const n = done ? Math.max(st.frames.length, 1) : Math.floor(w / (bw + gap));
    const scale = (v) => Math.min(1, Math.sqrt(v) * 2.2) * (mid - 3);
    for (let i = 0; i < n; i++) {
      const fi = start + i, x = i * (bw + gap);
      const s = st.sentEnv[first + fi], r = st.recvEnv[fi];
      if (s !== undefined) { const a = scale(s); ctx.fillStyle = "rgba(148,163,184,.55)"; ctx.fillRect(x, mid - a, Math.max(1, bw), a); }
      if (r !== undefined) { const a = scale(r); const fate = st.frames[fi] ? st.frames[fi][2] : 0; ctx.fillStyle = fate === 3 ? "#fb7185" : fate ? FATE_COL[fate] : "#5eead4"; ctx.fillRect(x, mid + 1, Math.max(1, bw), a); }
    }
  }

  /* ---------- telemetry ---------- */
  function setStepper(phase, timings = {}) {
    const cur = ORDER[phase] ?? -1;
    SW.$$(".step", ui.stepper).forEach((s, i) => {
      s.className = "step" + (phase === "error" && i === Math.max(0, st.lastOrder) ? " err" : i < cur || phase === "done" ? " done" : i === cur ? " cur" : "");
    });
    if (phase !== "error") st.lastOrder = cur;
    const tm = SW.$$(".step .tm", ui.stepper);
    tm[1].textContent = timings.auth_alice_ms ? `${Math.round(timings.auth_alice_ms)} / ${Math.round(timings.auth_bob_ms)} ms` : "";
    tm[2].textContent = timings.setup_ms ? `${Math.round(timings.setup_ms)} ms` : "";
  }
  function updateKpis(s) {
    const k = ui.k;
    k.sent.set(fmt.int(s.tx_frames), `${fmt.int(s.tx_dgrams)} datagrams on the wire`);
    const draining = st.run && (st.run.phase === "drain" || st.run.phase === "done");
    if (!draining) k.parity.set(fmt.int(s.tx_parity), `ρ = ${(s.rho * 100).toFixed(1)}% redundancy`);
    k.replicas.set(fmt.int(s.tx_replicas), "perceptual anchors");
    k.fec.set(fmt.int(st.counts[1]), `${fmt.int(s.fec)} parity reconstructions`);
    k.rep.set(fmt.int(st.counts[2]), `${fmt.int(s.replica)} replicas received`);
    k.conc.set(fmt.int(s.concealed), `of ${fmt.int(s.played)} frames played`);
    k.after.set(fmt.pct(s.loss_after, 1), `channel dropped ${fmt.int(s.drops)} datagrams`);
    if (!draining) k.rate.set(`${fmt.n(s.kbps, 0)}<small>kbit/s</small>`, s.codec ? `NAVIK: ${s.codec}` : "warming up");
    ui.gauge.set(s.mos > 0 ? s.mos : NaN);
    ui.jb.textContent = `${fmt.n(s.depth, 0)} ms`;
    ui.mloss.textContent = fmt.pct(s.net_loss, 1);
    ui.mburst.textContent = `${fmt.n(s.burst, 1)} pkts`;
    ui.mjit.textContent = `${fmt.n(s.jitter, 1)} ms`;
    ui.auth.textContent = s.auth_fail;
  }
  function pushCharts(list) {
    if (!list.length) return;
    const add = (ch, idx, key, f = (v) => v) => list.forEach((s) => ch.data.datasets[idx].data.push({ x: s.t, y: f(s[key]) }));
    add(ui.cNet, 0, "net_loss"); add(ui.cNet, 1, "jitter");
    add(ui.cAdapt, 0, "depth"); add(ui.cAdapt, 1, "rho", (v) => v * 100);
    add(ui.cQual, 0, "mos"); add(ui.cQual, 1, "loss_after");
    [ui.cNet, ui.cAdapt, ui.cQual].forEach((c) => c.update("none"));
  }
  function resetCharts() {
    [ui.cNet, ui.cAdapt, ui.cQual].forEach((c) => { c.data.datasets.forEach((d) => (d.data = [])); c.update("none"); });
  }
  function log(evs) {
    evs.forEach((e) => {
      const ln = h(`<div class="ln ${e.level}"><span class="ts">+${e.t.toFixed(1)}s</span><span>${esc(e.msg)}</span></div>`);
      ui.log.appendChild(ln);
    });
    if (evs.length) ui.log.scrollTop = ui.log.scrollHeight;
  }
  function legend() {
    ui.legend.innerHTML = FATE_NAME.map((n, i) => `<span><i class="swatch" style="background:${FATE_COL[i]}"></i>${n} <b>${st.counts[i]}</b></span>`).join("") +
      `<span class="dim">· bar height = perceptual importance of that frame</span>`;
  }

  function onPayload(p) {
    if (p.sent_env) st.sentEnv = p.sent_env;
    if (p.frames && p.frames.length) { p.frames.forEach((f) => { st.frames.push(f); st.counts[f[2]]++; }); legend(); }
    if (p.audio) {
      const pcm = SW.b64ToInt16(p.audio);
      st.player.push(pcm);
      const all = new Int16Array(st.recvRem.length + pcm.length); all.set(st.recvRem); all.set(pcm, st.recvRem.length);
      let o = 0;
      for (; o + 320 <= all.length; o += 320) { let s = 0; for (let i = 0; i < 320; i++) { const v = all[o + i] / 32768; s += v * v; } st.recvEnv.push(s / 320); }
      st.recvRem = all.slice(o);
    }
    if (p.phase) {
      st.run.phase = p.phase;
      setStepper(p.phase, (p.result || {}));
      ui.phase.textContent = { relay: "Starting relay", auth: "Authenticating", setup: "Setting up call", media: "In call", drain: "Draining buffer", done: "Call complete", error: "Failed" }[p.phase] || p.phase;
    }
    if (p.telemetry && p.telemetry.length) { st.tel.push(...p.telemetry); updateKpis(p.telemetry[p.telemetry.length - 1]); pushCharts(p.telemetry); }
    if (p.events) log(p.events);
    if (p.result !== undefined || p.error) finish(p.result, p.error);
    drawFrames();
  }

  function connect(id, withAudio = true) {
    if (st.es) st.es.close();
    const es = new EventSource(`/api/lab/${id}/stream?audio=${withAudio ? 1 : 0}`);
    st.es = es;
    es.addEventListener("hello", (e) => {
      const d = JSON.parse(e.data);
      st.run = { id: d.id, phase: d.phase, done: false, params: d.params };
      ui.callid.textContent = d.id;
    });
    es.onmessage = (e) => onPayload(JSON.parse(e.data));
    es.addEventListener("end", () => { es.close(); st.es = null; });
    es.onerror = () => {
      es.close(); st.es = null;
      if (!st.run || st.run.done) return;
      // Reconnect only if the server still knows this call (it may have restarted).
      setTimeout(async () => {
        if (!st.run || st.run.done) return;
        try {
          const d = await SW.api(`/api/lab/${id}`);
          if (d.done) { finish(d.result, d.error); return; }
          clearView(); connect(id, false);         // the server replays from the start, so rebuild
        } catch {
          st.run.done = true; setRunning(false);
          ui.phase.textContent = "Disconnected";
          SW.toast("Lost this call — the server was restarted. Place a new call.", "warn");
        }
      }, 1200);
    };
  }

  function clearView() {
    st.frames = []; st.sentEnv = []; st.recvEnv = []; st.recvRem = new Int16Array(0); st.tel = []; st.counts = [0, 0, 0, 0];
    ui.log.innerHTML = ""; resetCharts(); legend(); drawFrames();
  }

  function setRunning(on) {
    ui.start.classList.toggle("hidden", on);
    ui.stop.classList.toggle("hidden", !on);
    ui.src.disable(on); ui.prot.disable(on); ui.mode.disable(on); ui.dur.disable(on);
    ui.liveBadge.classList.toggle("hidden", !on);
    if (!on) refreshMicAvailability();
    clearInterval(st.timer);
    if (on) { st.t0 = Date.now(); st.timer = setInterval(() => (ui.elapsed.textContent = ((Date.now() - st.t0) / 1000).toFixed(1) + " s"), 100); }
  }

  async function start() {
    const net = ui.net.get();
    const body = { ...net, seconds: ui.dur.get(), protection: ui.prot.get(), mode: ui.mode.get(), source: ui.src.get(), seed: parseInt(ui.seed.value || "7", 10) };
    if (body.source === "upload") {
      if (!SW.clip) { SW.toast("Record or upload a clip first", "warn"); return; }
      body.upload_id = SW.clip.uploadId;
    }
    try {
      const d = await SW.api("/api/lab/call", { body });
      attach(d);
    } catch (e) { SW.toast(e.message, "bad"); }
  }
  function attach(d) {
    st.run = { id: d.id, phase: d.phase, done: false, params: d.params };
    st.params = d.params;
    ui.result.classList.add("hidden");
    ui.callid.textContent = d.id;
    ui.title.textContent = `alice → bob · ${SW.armName[d.params.protection]} · ${d.params.mode}`;
    clearView();
    setStepper(d.phase);
    setRunning(true);
    connect(d.id, true);
  }

  function finish(res, err) {
    if (!st.run || st.run.done) return;
    st.run.done = true;
    setRunning(false);
    ui.elapsed.textContent = ((Date.now() - st.t0) / 1000).toFixed(1) + " s";
    drawFrames();
    if (err || !res) { ui.phase.textContent = "Failed"; SW.toast("Call failed: " + (err || "unknown error"), "bad"); setStepper("error"); return; }
    setStepper("done", res);
    ui.gauge.set(res.mos, `MOS · ${res.quality} · final`);
    ui.k.rate.set(`${fmt.n(res.tx_kbps, 0)}<small>kbit/s</small>`, "call average");
    ui.k.parity.set(fmt.int(res.parity), `${fmt.pct((100 * res.parity) / Math.max(1, res.sent_frames), 1)} of frames sent`);
    renderResult(res);
    st.history.unshift({ id: st.run.id, p: st.params, r: res });
    renderHistory();
  }

  function renderResult(r) {
    const id = st.run.id, box = ui.result;
    box.classList.remove("hidden");
    const qcol = SW.mosColor(r.mos);
    box.innerHTML = `
      <div class="card-h"><h3>Call report</h3><span class="sub">${esc(SW.armName[r.protect])} · ${fmt.n(r.seconds, 1)} s · call ${id}</span>
        <div class="right"><button class="btn sm" data-a="bench">Benchmark this network →</button><button class="btn sm primary" data-a="again">Run again</button></div></div>
      <div class="card-b">
        <div class="grid g3">
          <div class="kpi" style="--kc:${qcol}"><div class="k-label">Est. MOS (E-model)</div><div class="k-value" style="color:${qcol}">${fmt.n(r.mos, 2)}</div><div class="k-sub">${r.quality} · R = ${fmt.n(r.r, 1)}</div></div>
          <div class="kpi" style="--kc:var(--replica)"><div class="k-label">Perceptual dmg</div><div class="k-value">${fmt.pct(r.perceptual_pct)}</div><div class="k-sub">loss weighted by audibility</div></div>
          <div class="kpi" style="--kc:var(--conceal)"><div class="k-label">Unrecoverable</div><div class="k-value">${fmt.pct(r.loss_after_repair)}</div><div class="k-sub">channel dropped ${fmt.pct(r.channel_loss_pct)}</div></div>
          <div class="kpi" style="--kc:var(--fec)"><div class="k-label">Correlation</div><div class="k-value">${fmt.n(r.corr, 3)}</div><div class="k-sub">SNR ${fmt.n(r.snr_db, 1)} dB</div></div>
          <div class="kpi" style="--kc:var(--uniform)"><div class="k-label">Wire rate</div><div class="k-value">${fmt.n(r.tx_kbps, 1)}<small>kbit/s</small></div><div class="k-sub">${fmt.int(r.parity)} parity · ${fmt.int(r.replicas)} replicas</div></div>
          <div class="kpi" style="--kc:var(--ok)"><div class="k-label">Auth failures</div><div class="k-value">${fmt.int(r.auth_fail)}</div><div class="k-sub">handshake ${fmt.ms(r.auth_alice_ms)} · setup ${fmt.ms(r.setup_ms)}</div></div>
        </div>
        <div class="grid g2 mt">
          <div class="audio-card"><div class="ac-h"><i class="swatch" style="background:#94a3b8"></i>What alice said <span class="dim small">(sent)</span><a class="btn sm ghost" style="margin-left:auto" href="/api/lab/${id}/audio/sent.wav" download>Download</a></div><audio controls preload="none" src="/api/lab/${id}/audio/sent.wav"></audio></div>
          <div class="audio-card"><div class="ac-h"><i class="swatch" style="background:#5eead4"></i>What bob heard <span class="dim small">(received)</span><a class="btn sm ghost" style="margin-left:auto" href="/api/lab/${id}/audio/received.wav" download>Download</a></div><audio controls preload="none" src="/api/lab/${id}/audio/received.wav"></audio></div>
        </div>
      </div>`;
    box.querySelector('[data-a="again"]').onclick = start;
    box.querySelector('[data-a="bench"]').onclick = () => { SW.benchPreset = { ...ui.net.get(), seconds: ui.dur.get(), source: ui.src.get() }; SW.go("bench"); };
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function renderHistory() {
    if (!st.history.length) return;
    ui.hist.classList.remove("hidden");
    ui.histBody.innerHTML = st.history.map(({ id, p, r }) => `<tr>
      <td class="mono">${id}</td><td><span class="badge" style="color:${SW.armColor()[p.protection]}">${SW.armName[p.protection]}</span></td>
      <td>${(p.loss * 100).toFixed(0)}% · b${p.burst} · ${p.jitter} ms</td><td>${p.source}</td>
      <td class="num">${fmt.pct(r.perceptual_pct)}</td><td class="num">${fmt.pct(r.loss_after_repair)}</td><td class="num">${fmt.n(r.tx_kbps, 0)}</td>
      <td class="num" style="color:${SW.mosColor(r.mos)}">${fmt.n(r.mos, 2)}</td>
      <td><a class="btn sm ghost" href="/api/lab/${id}/audio/received.wav" target="_blank">▶ heard</a></td></tr>`).join("");
  }

  async function refreshMicAvailability() {
    const s = SW.status || (await SW.api("/api/status").catch(() => null));
    if (s && ui.src) ui.src.enable("mic", !!s.audio_backends.live);
  }

  function lineChart(canvas, sets, y1, y2) {
    return SW.chart(canvas, {
      type: "line",
      data: { datasets: sets.map(([label, color, axis]) => ({ label, borderColor: color, backgroundColor: SW.alpha(color, 0.1), data: [], yAxisID: axis, tension: 0.25, fill: axis === "y", borderWidth: 2 })) },
      options: { animation: false, parsing: false, interaction: { mode: "nearest", intersect: false, axis: "x" },
        scales: { x: SW.axis("seconds", { type: "linear", min: 0, ticks: { maxTicksLimit: 8 } }), y: SW.axis(y1, { beginAtZero: true }), y2: SW.axis(y2, { position: "right", beginAtZero: true, grid: { display: false } }) },
        plugins: { legend: { position: "top", align: "end" } } },
    });
  }

  SW.page("lab", {
    title: "Live Call Lab",
    group: "Console",
    mount(el) {
      el.innerHTML = `
      <div class="split">
        <div class="stack">
          <div class="card">
            <div class="card-h"><h3>Call setup</h3><span class="badge live hidden" id="lb-live">LIVE</span></div>
            <div class="card-b" id="lab-cfg"></div>
          </div>
          <div class="note small">Each call starts a private relay, authenticates <b>alice</b> and <b>bob</b> with SPARK, and carries real ChaCha20-Poly1305 media over UDP sockets on the loopback interface. Impairment is injected at the sending socket — and can be changed <b>while the call runs</b>.</div>
        </div>
        <div class="stack">
          <div class="card">
            <div class="card-h"><h3 id="lab-title">No call yet</h3><span class="badge mono" id="lab-id">—</span>
              <div class="right"><span class="muted small" id="lab-phase">Idle</span><span class="badge mono" id="lab-el">0.0 s</span>
              <button class="btn sm" id="lab-listen" title="Stream bob's received audio to this browser"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>Listen live</button></div></div>
            <div class="card-b"><div class="stepper" id="lab-steps">${STEPS.map(([, n], i) => `<div class="step"><div class="bub">${i + 1}</div><div class="nm">${n}</div><div class="tm"></div></div>`).join("")}</div></div>
          </div>

          <div class="grid" style="grid-template-columns:230px minmax(0,1fr)">
            <div class="card"><div class="card-b" style="display:grid;justify-items:center;gap:8px">
              <div id="lab-gauge"></div>
              <div class="kv" style="width:100%"><dt>Jitter buffer</dt><dd id="lab-jb">—</dd><dt>Measured loss</dt><dd id="lab-mloss">—</dd><dt>Loss burst</dt><dd id="lab-mburst">—</dd><dt>Jitter</dt><dd id="lab-mjit">—</dd><dt>Auth failures</dt><dd id="lab-auth">—</dd></div>
            </div></div>
            <div class="grid g4" id="lab-kpis"></div>
          </div>

          <div class="card">
            <div class="card-h"><h3>Frame fate map</h3><span class="sub">every 20 ms frame alice → bob, classified at playout</span></div>
            <div class="card-b">
              <canvas class="strip" id="lab-strip" style="height:150px"></canvas>
              <div class="row between mt-s"><div class="fate-legend" id="lab-legend"></div></div>
              <div class="row mt small muted" style="gap:16px"><span><i class="swatch" style="background:#94a3b8"></i> sent waveform (above)</span><span><i class="swatch" style="background:#5eead4"></i> received waveform (below)</span></div>
              <canvas class="strip mt-s" id="lab-wave" style="height:96px"></canvas>
            </div>
          </div>

          <div class="grid g3">
            <div class="card"><div class="card-h"><h3>Network as measured</h3></div><div class="card-b"><div class="chart-box sm"><canvas id="c-net"></canvas></div></div></div>
            <div class="card"><div class="card-h"><h3>Adaptation</h3></div><div class="card-b"><div class="chart-box sm"><canvas id="c-adapt"></canvas></div></div></div>
            <div class="card"><div class="card-h"><h3>Quality</h3></div><div class="card-b"><div class="chart-box sm"><canvas id="c-qual"></canvas></div></div></div>
          </div>

          <div class="card hidden glow" id="lab-result"></div>

          <div class="card"><div class="card-h"><h3>Call log</h3><span class="sub">signalling and control events</span></div><div class="card-b"><div class="console" id="lab-log"></div></div></div>

          <div class="card hidden" id="lab-hist"><div class="card-h"><h3>This session</h3><span class="sub">calls placed from this console</span></div>
            <div class="table-wrap"><table class="t"><thead><tr><th>Call</th><th>Protection</th><th>Network</th><th>Source</th><th class="num">Perceptual dmg</th><th class="num">Unrecoverable</th><th class="num">kbit/s</th><th class="num">MOS</th><th></th></tr></thead><tbody></tbody></table></div></div>
        </div>
      </div>`;

      const cfg = el.querySelector("#lab-cfg");
      ui = {
        strip: el.querySelector("#lab-strip"), wave: el.querySelector("#lab-wave"), stepper: el.querySelector("#lab-steps"),
        log: el.querySelector("#lab-log"), legend: el.querySelector("#lab-legend"), result: el.querySelector("#lab-result"),
        phase: el.querySelector("#lab-phase"), elapsed: el.querySelector("#lab-el"), callid: el.querySelector("#lab-id"), title: el.querySelector("#lab-title"),
        jb: el.querySelector("#lab-jb"), auth: el.querySelector("#lab-auth"),
        mloss: el.querySelector("#lab-mloss"), mburst: el.querySelector("#lab-mburst"), mjit: el.querySelector("#lab-mjit"),
        hist: el.querySelector("#lab-hist"), histBody: el.querySelector("#lab-hist tbody"), liveBadge: el.querySelector("#lb-live"),
      };

      // --- config controls
      const clipBox = h(`<div class="hidden" style="margin:-4px 0 14px"></div>`);
      clipBox.appendChild(SW.clipWidget());
      ui.src = SW.seg([["synthetic", "Synthetic speech"], ["upload", "My recording"], ["mic", "Server mic"]], "synthetic", (v) => {
        clipBox.classList.toggle("hidden", v !== "upload");
        micNote.classList.toggle("hidden", v !== "mic");
      });
      const micNote = h(`<div class="note warn small hidden" style="margin:-4px 0 14px">Captures the microphone of the machine running uvicorn via <code>sounddevice</code>. Use headphones if you enable <b>Listen live</b>.</div>`);
      cfg.appendChild(SW.field("Audio source (alice's voice)", ui.src));
      cfg.appendChild(clipBox); cfg.appendChild(micNote);
      ui.dur = SW.slider({ label: "Call length", min: 3, max: 60, step: 1, value: 15, fmt: (v) => v + " s" });
      cfg.appendChild(ui.dur.el);
      ui.prot = SW.seg([["swara", "SWARA"], ["uniform", "Uniform FEC"], ["none", "None"]], "swara");
      cfg.appendChild(SW.field("Loss protection", ui.prot, "SWARA places parity and replicas by perceptual importance; uniform spreads the same budget evenly."));
      ui.mode = SW.seg([["wideband", "Wideband 16 kHz"], ["narrowband", "Narrowband 8 kHz"]], "wideband");
      cfg.appendChild(SW.field("Codec mode", ui.mode));
      cfg.appendChild(h(`<div class="divider"></div>`));
      const pushImp = SW.debounce(async (v) => {
        if (!st.run || st.run.done) return;
        try { await SW.api(`/api/lab/${st.run.id}/impairment`, { body: v }); } catch (e) { SW.toast(e.message, "bad"); }
      }, 220);
      ui.net = SW.netControls({ loss: 0.10, burst: 3, jitter: 10, extra_delay: 0 }, pushImp);
      cfg.appendChild(ui.net.el);
      const adv = h(`<details class="mt-s"><summary class="small muted" style="cursor:pointer">Advanced</summary><div class="field mt-s"><label>Random seed <span class="hint" style="margin-left:auto">same seed → same channel realisation</span></label><input type="number" value="7" min="0" max="99999"></div></details>`);
      ui.seed = adv.querySelector("input");
      cfg.appendChild(adv);
      cfg.appendChild(h(`<div class="divider"></div>`));
      ui.start = h(`<button class="btn primary lg block"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>Place call</button>`);
      ui.stop = h(`<button class="btn danger lg block hidden"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>Hang up</button>`);
      ui.start.onclick = start;
      ui.stop.onclick = async () => { if (st.run) { try { await SW.api(`/api/lab/${st.run.id}/stop`, { method: "POST" }); } catch (e) { SW.toast(e.message, "bad"); } } };
      cfg.appendChild(ui.start); cfg.appendChild(ui.stop);
      cfg.appendChild(h(`<div class="hint small dim mt-s center">Network sliders stay live during the call.</div>`));

      // --- listen live
      const lb = el.querySelector("#lab-listen");
      lb.onclick = async () => {
        if (st.player.on) { st.player.disable(); lb.classList.remove("on"); }
        else { await st.player.enable(); lb.classList.add("on"); SW.toast("Streaming bob's received audio to this browser", "info", 2500); }
      };

      // --- KPIs & gauge
      ui.gauge = SW.gauge(190); el.querySelector("#lab-gauge").appendChild(ui.gauge.el);
      const kp = el.querySelector("#lab-kpis");
      ui.k = {
        sent: SW.kpi("Frames sent", "var(--accent)"), parity: SW.kpi("Parity packets", "var(--uniform)"),
        replicas: SW.kpi("Anchor replicas", "var(--replica)"), rate: SW.kpi("Wire rate", "var(--accent-2)"),
        fec: SW.kpi("Rebuilt by FEC", "var(--fec)"), rep: SW.kpi("Saved by replica", "var(--replica)"),
        conc: SW.kpi("Frames concealed", "var(--conceal)"), after: SW.kpi("Loss after repair", "var(--bad)"),
      };
      Object.values(ui.k).forEach((k) => kp.appendChild(k.el));

      ui.cNet = lineChart(el.querySelector("#c-net"), [["loss %", "#fb7185", "y"], ["jitter ms", "#fbbf24", "y2"]], "loss %", "jitter ms");
      ui.cAdapt = lineChart(el.querySelector("#c-adapt"), [["buffer depth ms", "#60a5fa", "y"], ["FEC ρ %", "#f59e0b", "y2"]], "ms", "ρ %");
      ui.cQual = lineChart(el.querySelector("#c-qual"), [["MOS estimate", "#2dd4bf", "y"], ["loss after repair %", "#fb7185", "y2"]], "MOS", "%");
      ui.cQual.options.scales.y.max = 4.5; ui.cQual.options.scales.y.min = 1;

      legend(); drawFrames();
      window.addEventListener("resize", SW.debounce(drawFrames, 100));
      refreshMicAvailability();
    },
    async show() {
      setTimeout(drawFrames, 30);
      if (SW.labPreset) { ui.net.set(SW.labPreset); SW.labPreset = null; }
      if (st.run && !st.run.done) return;
      try {
        const a = await SW.api("/api/lab/active");
        if (a.busy && a.run && a.run.kind === "call" && (!st.run || st.run.id !== a.run.id)) { attach(a.run); SW.toast("Attached to the call in progress", "info"); }
      } catch {}
    },
  });
})();
