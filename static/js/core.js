/* ==========================================================================
   SWARA Console — core runtime: router, API client, controls, charts, audio
   ========================================================================== */
(function () {
  "use strict";

  const SW = (window.SW = {
    pages: {},
    mounted: {},
    current: null,
    status: null,
    cache: {},
    clip: null,          // the operator's recorded/uploaded clip, shared by pages
  });

  /* ---------- DOM helpers ---------- */
  SW.$ = (s, r = document) => r.querySelector(s);
  SW.$$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  SW.h = (html) => {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  };
  SW.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  SW.debounce = (fn, ms = 250) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  SW.sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  SW.css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  /* ---------- formatting ---------- */
  const isNum = (v) => typeof v === "number" && isFinite(v);
  SW.fmt = {
    n: (v, d = 1) => (isNum(v) ? v.toFixed(d) : "—"),
    int: (v) => (isNum(v) ? Math.round(v).toLocaleString() : "—"),
    pct: (v, d = 1) => (isNum(v) ? v.toFixed(d) + "%" : "—"),
    ms: (v, d = 0) => (isNum(v) ? v.toFixed(d) + " ms" : "—"),
    bytes: (v) => (!isNum(v) ? "—" : v < 1024 ? v + " B" : v < 1048576 ? (v / 1024).toFixed(1) + " KB" : (v / 1048576).toFixed(2) + " MB"),
    dur: (s) => {
      if (!isNum(s)) return "—";
      s = Math.floor(s);
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
      return h ? `${h}h ${m}m` : m ? `${m}m ${x}s` : `${x}s`;
    },
    time: (ts) => new Date(ts * 1000).toLocaleTimeString(),
  };
  SW.mosColor = (m) => (m >= 4.0 ? "#34d399" : m >= 3.6 ? "#a3e635" : m >= 3.1 ? "#fbbf24" : m >= 2.6 ? "#fb923c" : "#f87171");
  SW.mosLabel = (m) => (m >= 4.3 ? "Excellent" : m >= 4.0 ? "Good" : m >= 3.6 ? "Fair" : m >= 3.1 ? "Poor" : "Bad");
  SW.armName = { swara: "SWARA", uniform: "Uniform FEC", none: "Unprotected", baseline: "Baseline", uniform_fec: "Uniform FEC" };
  SW.armColor = () => ({ swara: SW.css("--swara"), uniform: SW.css("--uniform"), uniform_fec: SW.css("--uniform"), none: SW.css("--baseline"), baseline: SW.css("--baseline") });

  /* ---------- API ---------- */
  SW.api = async (path, opts = {}) => {
    const init = { method: opts.method || (opts.body !== undefined || opts.raw ? "POST" : "GET"), headers: {} };
    if (opts.raw) { init.body = opts.raw; init.headers["Content-Type"] = "application/octet-stream"; }
    else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
    const res = await fetch(path, init);
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      let msg = data && data.detail ? data.detail : res.statusText;
      if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return data;
  };
  SW.results = async () => {
    if (!SW.cache.results) SW.cache.results = await SW.api("/experiments/results");
    return SW.cache.results;
  };

  /* ---------- toasts ---------- */
  SW.toast = (msg, kind = "info", ms = 4200) => {
    const icon = { ok: "✓", bad: "✕", warn: "!", info: "i" }[kind] || "i";
    const col = { ok: "var(--ok)", bad: "var(--bad)", warn: "var(--warn)", info: "var(--info)" }[kind];
    const el = SW.h(`<div class="toast ${kind}"><b style="color:${col}">${icon}</b><div>${SW.esc(msg)}</div></div>`);
    SW.$("#toasts").appendChild(el);
    setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, ms);
  };
  SW.copy = async (text) => {
    try { await navigator.clipboard.writeText(text); SW.toast("Copied to clipboard", "ok", 1600); }
    catch { SW.toast("Copy failed — select and copy manually", "warn"); }
  };
  SW.codeBlock = (text) => {
    const el = SW.h(`<div class="code"><button class="btn sm ghost copy" title="Copy">Copy</button></div>`);
    el.insertBefore(document.createTextNode(text), el.firstChild);
    el.querySelector(".copy").onclick = () => SW.copy(text);
    return el;
  };

  /* ---------- router ---------- */
  SW.page = (id, def) => { SW.pages[id] = def; };
  SW.go = (id) => { location.hash = "#/" + id; };
  function route() {
    const id = (location.hash.replace(/^#\/?/, "").split("?")[0]) || "overview";
    const def = SW.pages[id] || SW.pages.overview;
    const pid = SW.pages[id] ? id : "overview";
    if (SW.current && SW.pages[SW.current] && SW.pages[SW.current].hide) SW.pages[SW.current].hide();
    SW.$$(".page").forEach((p) => p.classList.remove("active"));
    let el = SW.mounted[pid];
    if (!el) {
      el = SW.h(`<section class="page" id="page-${pid}"></section>`);
      SW.$("#content").appendChild(el);
      SW.mounted[pid] = el;
      try { def.mount(el); } catch (e) { console.error(e); el.innerHTML = `<div class="note warn">Failed to render: ${SW.esc(e.message)}</div>`; }
    }
    el.classList.add("active");
    SW.current = pid;
    SW.$("#title").textContent = def.title;
    SW.$("#crumb").textContent = def.group || "Console";
    document.title = `${def.title} · SWARA Console`;
    SW.$$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === pid));
    SW.$("#sidebar").classList.remove("open");
    SW.$("#content").scrollTop = 0;
    if (def.show) def.show(el);
  }

  /* ---------- status polling ---------- */
  async function pollStatus() {
    try {
      const s = await SW.api("/api/status");
      SW.status = s;
      const r = s.relay;
      const rc = SW.$("#chip-relay");
      rc.querySelector(".dot").className = "dot " + (r.running ? "ok" : r.error ? "bad" : "");
      rc.querySelector("span:last-child").textContent = r.running
        ? `Relay udp:${r.port} · ${r.online} online${r.calls ? ` · ${r.calls} call${r.calls > 1 ? "s" : ""}` : ""}`
        : r.error ? "Relay error" : "Relay stopped";
      SW.$("#foot-relay .dot").className = "dot " + (r.running ? "ok" : "bad");
      const ec = SW.$("#chip-engine");
      const busy = s.lab.busy || s.experiments.running;
      ec.querySelector(".dot").className = "dot " + (busy ? "warn pulse" : "ok");
      ec.querySelector("span:last-child").textContent = s.lab.busy
        ? (s.lab.kind === "bench" ? "Benchmark running" : `Call · ${s.lab.phase}`)
        : s.experiments.running ? "Experiments running" : "Engine idle";
      SW.$("#foot-engine .dot").className = "dot " + (busy ? "warn pulse" : "ok");
      SW.$("#nav-live").classList.toggle("hidden", !(s.lab.busy && s.lab.kind === "call"));
      SW.$("#foot-uptime").textContent = "up " + SW.fmt.dur(s.uptime_s);
      document.dispatchEvent(new CustomEvent("sw:status", { detail: s }));
    } catch (e) {
      SW.$("#chip-relay .dot").className = "dot bad";
      SW.$("#chip-relay span:last-child").textContent = "API unreachable";
    }
  }

  /* ---------- controls ---------- */
  // Range slider with live value read-out. Returns {el, input, get, set}.
  SW.slider = ({ label, min, max, step = 1, value, fmt = (v) => v, hint, onInput, onChange }) => {
    const el = SW.h(`<div class="field"><label>${label}<span class="val"></span></label><input type="range" min="${min}" max="${max}" step="${step}" value="${value}">${hint ? `<div class="hint">${hint}</div>` : ""}</div>`);
    const input = el.querySelector("input"), val = el.querySelector(".val");
    const paint = () => {
      const v = parseFloat(input.value);
      val.textContent = fmt(v);
      input.style.setProperty("--p", ((v - min) / (max - min)) * 100 + "%");
    };
    input.addEventListener("input", () => { paint(); onInput && onInput(parseFloat(input.value)); });
    input.addEventListener("change", () => onChange && onChange(parseFloat(input.value)));
    paint();
    return { el, input, get: () => parseFloat(input.value), set: (v) => { input.value = v; paint(); }, disable: (d) => (input.disabled = d) };
  };
  // Segmented control. options: [[value, label, disabled?], ...]
  SW.seg = (options, value, onChange) => {
    const el = SW.h(`<div class="seg"></div>`);
    let cur = value;
    options.forEach(([v, lab, dis]) => {
      const b = SW.h(`<button type="button" data-v="${v}">${lab}</button>`);
      if (dis) b.disabled = true;
      b.onclick = () => { if (b.disabled) return; api.set(v); onChange && onChange(v); };
      el.appendChild(b);
    });
    const api = {
      el,
      get: () => cur,
      set: (v) => { cur = v; SW.$$("button", el).forEach((b) => b.classList.toggle("on", b.dataset.v === String(v))); },
      enable: (v, on) => { const b = el.querySelector(`[data-v="${v}"]`); if (b) b.disabled = !on; },
      disable: (d) => SW.$$("button", el).forEach((b) => (b.disabled = d)),
    };
    api.set(value);
    return api;
  };
  SW.toggle = (label, checked, onChange) => {
    const el = SW.h(`<label class="check"><input type="checkbox" ${checked ? "checked" : ""}><span class="tog"></span><span>${label}</span></label>`);
    const i = el.querySelector("input");
    i.onchange = () => onChange && onChange(i.checked);
    return { el, get: () => i.checked, set: (v) => (i.checked = v) };
  };
  SW.field = (label, control, hint) => {
    const el = SW.h(`<div class="field"><label>${label}</label>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`);
    el.insertBefore(control.el || control, el.querySelector(".hint"));
    return el;
  };
  SW.kpi = (label, color) => {
    const el = SW.h(`<div class="kpi" style="--kc:${color || "transparent"}"><div class="k-label">${label}</div><div class="k-value">—</div><div class="k-sub">&nbsp;</div></div>`);
    return { el, set: (v, sub) => { el.querySelector(".k-value").innerHTML = v; if (sub !== undefined) el.querySelector(".k-sub").innerHTML = sub || "&nbsp;"; } };
  };
  SW.empty = (title, text, icon) => `<div class="empty">${icon || '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>'}<h4>${title}</h4><div class="small">${text || ""}</div></div>`;

  /* ---------- MOS gauge (SVG) ---------- */
  SW.gauge = (size = 170) => {
    const r = size / 2 - 14, cx = size / 2, cy = size / 2 + 6;
    const arc = (a0, a1) => {
      const p = (a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
      const [x0, y0] = p(a0), [x1, y1] = p(a1);
      return `M${x0} ${y0} A${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${x1} ${y1}`;
    };
    const A0 = Math.PI * 0.85, A1 = Math.PI * 2.15;
    const el = SW.h(`<div class="gauge"><svg width="${size}" height="${size * 0.78}" viewBox="0 0 ${size} ${size * 0.78}">
      <defs><linearGradient id="gg${size}" x1="0" x2="1"><stop offset="0" stop-color="#f87171"/><stop offset=".45" stop-color="#fbbf24"/><stop offset=".75" stop-color="#a3e635"/><stop offset="1" stop-color="#34d399"/></linearGradient></defs>
      <path d="${arc(A0, A1)}" stroke="#1b2740" stroke-width="11" fill="none" stroke-linecap="round"/>
      <path class="g-arc" d="${arc(A0, A1)}" stroke="url(#gg${size})" stroke-width="11" fill="none" stroke-linecap="round" stroke-dasharray="0 9999" style="opacity:0"/>
      <circle class="g-dot" r="6" fill="#fff" stroke="#0b1220" stroke-width="3" cx="-20" cy="-20" style="opacity:0"/>
    </svg><div class="g-val"><div class="g-num">—</div><div class="g-lab muted">MOS</div></div></div>`);
    const path = el.querySelector(".g-arc"), dot = el.querySelector(".g-dot");
    const len = r * (A1 - A0);
    return {
      el,
      set(m, label) {
        if (!isNum(m)) { el.querySelector(".g-num").textContent = "—"; path.style.opacity = 0; dot.style.opacity = 0; return; }
        path.style.opacity = 1; dot.style.opacity = 1;
        const f = Math.max(0, Math.min(1, (m - 1) / 3.5));
        path.setAttribute("stroke-dasharray", `${len * f} 9999`);
        const a = A0 + (A1 - A0) * f;
        dot.setAttribute("cx", cx + r * Math.cos(a)); dot.setAttribute("cy", cy + r * Math.sin(a));
        const n = el.querySelector(".g-num"); n.textContent = m.toFixed(2); n.style.color = SW.mosColor(m);
        el.querySelector(".g-lab").textContent = label || "MOS · " + SW.mosLabel(m);
      },
    };
  };

  /* ---------- charts ---------- */
  SW.chartsInit = () => {
    if (!window.Chart) return;
    const C = window.Chart;
    C.defaults.color = "#8193b2";
    C.defaults.font.family = getComputedStyle(document.body).fontFamily;
    C.defaults.font.size = 11.5;
    C.defaults.borderColor = "rgba(40, 56, 88, .45)";
    C.defaults.animation.duration = 350;
    C.defaults.plugins.legend.labels.boxWidth = 10;
    C.defaults.plugins.legend.labels.boxHeight = 10;
    C.defaults.plugins.legend.labels.usePointStyle = true;
    C.defaults.plugins.legend.labels.color = "#b8c5dc";
    C.defaults.plugins.tooltip.backgroundColor = "#0b1324";
    C.defaults.plugins.tooltip.borderColor = "#2a3a58";
    C.defaults.plugins.tooltip.borderWidth = 1;
    C.defaults.plugins.tooltip.padding = 10;
    C.defaults.plugins.tooltip.titleColor = "#e7eef9";
    C.defaults.plugins.tooltip.bodyColor = "#b8c5dc";
    C.defaults.elements.line.borderWidth = 2.2;
    C.defaults.elements.point.radius = 0;
    C.defaults.elements.point.hoverRadius = 5;
    C.defaults.maintainAspectRatio = false;
  };
  SW.chart = (canvas, config) => {
    if (canvas._chart) canvas._chart.destroy();
    const c = new Chart(canvas, config);
    canvas._chart = c;
    return c;
  };
  SW.axis = (title, extra = {}) => ({ title: { display: !!title, text: title, color: "#8193b2", font: { weight: 600 } }, grid: { color: "rgba(40,56,88,.35)" }, ...extra });
  SW.alpha = (hex, a) => {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };

  /* ---------- audio ---------- */
  SW.b64ToInt16 = (b64) => {
    const bin = atob(b64), n = bin.length >> 1, out = new Int16Array(n);
    for (let i = 0; i < n; i++) { const lo = bin.charCodeAt(2 * i), hi = bin.charCodeAt(2 * i + 1); let v = lo | (hi << 8); if (v & 0x8000) v -= 0x10000; out[i] = v; }
    return out;
  };
  SW.wavBlob = (int16, rate = 16000) => {
    const buf = new ArrayBuffer(44 + int16.length * 2), v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, "RIFF"); v.setUint32(4, 36 + int16.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, int16.length * 2, true);
    new Int16Array(buf, 44).set(int16);
    return new Blob([buf], { type: "audio/wav" });
  };
  // One AudioContext for the whole console (created on a user gesture).
  SW.audioContext = () => (SW._actx || (SW._actx = new (window.AudioContext || window.webkitAudioContext)()));
  // Plays a stream of 16 kHz int16 chunks gaplessly, for "listen live" and the softphone.
  SW.PcmPlayer = class {
    constructor() { this.ctx = null; this.t = 0; this.on = false; this.gain = null; this.level = 0; }
    async enable() {
      if (!this.ctx) { this.ctx = SW.audioContext(); this.gain = this.ctx.createGain(); this.gain.connect(this.ctx.destination); }
      await this.ctx.resume(); this.on = true; this.t = 0;
    }
    disable() { this.on = false; this.level = 0; }
    push(int16) {
      if (!this.on || !this.ctx || !int16.length) return;
      let acc = 0; const n = Math.min(320, int16.length);
      for (let i = 0; i < n; i++) { const v = int16[i] / 32768; acc += v * v; }
      this.level = Math.sqrt(acc / n);
      const b = this.ctx.createBuffer(1, int16.length, 16000), ch = b.getChannelData(0);
      for (let i = 0; i < int16.length; i++) ch[i] = int16[i] / 32768;
      const s = this.ctx.createBufferSource(); s.buffer = b; s.connect(this.gain);
      const now = this.ctx.currentTime;
      if (this.t < now + 0.04) this.t = now + 0.25;          // re-prime after an underrun
      s.start(this.t); this.t += b.duration;
    }
  };
  // Convert any decodable audio (file or recording) to 16 kHz mono int16.
  SW.toPcm16k = async (audioBuffer) => {
    const len = Math.ceil(audioBuffer.duration * 16000);
    const off = new OfflineAudioContext(1, len, 16000);
    const src = off.createBufferSource(); src.buffer = audioBuffer; src.connect(off.destination); src.start();
    const out = await off.startRendering();
    const f = out.getChannelData(0), pcm = new Int16Array(f.length);
    let peak = 0; for (let i = 0; i < f.length; i++) peak = Math.max(peak, Math.abs(f[i]));
    const g = peak > 0 && peak < 0.5 ? 0.5 / peak : 1;     // gentle normalisation for quiet mics
    for (let i = 0; i < f.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, Math.round(f[i] * g * 32767)));
    return pcm;
  };
  SW.decodeFile = async (file) => {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    try { return await SW.toPcm16k(await ctx.decodeAudioData(await file.arrayBuffer())); }
    finally { ctx.close(); }
  };
  // Streams the microphone as 16 kHz int16 PCM chunks (for the softphone).
  // Resampling is done here with linear interpolation so the server receives
  // exactly the rate the codec wants, whatever the device runs at.
  SW.MicStream = class {
    constructor() { this.muted = false; this.sending = false; this.level = 0; }
    async start(onChunk) {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
      this.ctx = SW.audioContext();
      await this.ctx.resume();
      this.src = this.ctx.createMediaStreamSource(this.stream);
      this.node = this.ctx.createScriptProcessor(1024, 1, 1);
      this.sink = this.ctx.createGain(); this.sink.gain.value = 0;          // the processor only runs when connected
      this.ratio = this.ctx.sampleRate / 16000; this.carry = 0; this.pos = 1;
      this.node.onaudioprocess = (e) => {
        const d = e.inputBuffer.getChannelData(0), N = d.length;
        let s = 0; for (let i = 0; i < N; i++) s += d[i] * d[i];
        this.level = this.muted ? 0 : Math.sqrt(s / N);
        const x = (i) => (i <= 0 ? this.carry : d[i - 1]);           // x[0] = last sample of the previous block
        const out = [];
        let p = this.pos;
        while (p <= N) { const i0 = Math.floor(p), f = p - i0, a = x(i0), b = i0 + 1 <= N ? x(i0 + 1) : a; out.push(a + (b - a) * f); p += this.ratio; }
        this.pos = p - N; this.carry = d[N - 1];
        if (!this.sending) return;
        const pcm = new Int16Array(out.length);
        if (!this.muted) for (let i = 0; i < out.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, Math.round(out[i] * 32767)));
        onChunk(pcm);
      };
      this.src.connect(this.node); this.node.connect(this.sink); this.sink.connect(this.ctx.destination);
    }
    stop() {
      this.sending = false;
      try { this.node && this.node.disconnect(); this.src && this.src.disconnect(); this.sink && this.sink.disconnect(); } catch {}
      try { this.stream && this.stream.getTracks().forEach((t) => t.stop()); } catch {}
    }
  };
  SW.Recorder = class {
    async start(onLevel) {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      const src = this.ctx.createMediaStreamSource(this.stream);
      this.node = this.ctx.createScriptProcessor(4096, 1, 1);
      this.chunks = [];
      this.node.onaudioprocess = (e) => {
        const d = e.inputBuffer.getChannelData(0); this.chunks.push(new Float32Array(d));
        if (onLevel) { let s = 0; for (let i = 0; i < d.length; i++) s += d[i] * d[i]; onLevel(Math.min(1, Math.sqrt(s / d.length) * 6)); }
      };
      src.connect(this.node); this.node.connect(this.ctx.destination);
      this.t0 = performance.now();
    }
    elapsed() { return (performance.now() - this.t0) / 1000; }
    async stop() {
      this.node.disconnect(); this.stream.getTracks().forEach((t) => t.stop());
      const n = this.chunks.reduce((a, c) => a + c.length, 0);
      const buf = this.ctx.createBuffer(1, Math.max(1, n), this.ctx.sampleRate);
      const ch = buf.getChannelData(0); let o = 0; this.chunks.forEach((c) => { ch.set(c, o); o += c.length; });
      const pcm = await SW.toPcm16k(buf); this.ctx.close(); return pcm;
    }
  };

  /* Clip widget: record from the browser mic or upload a file; the clip is
     uploaded once and shared by the Live Lab, Benchmark and Analyzer pages. */
  SW.clipWidget = (onReady) => {
    const el = SW.h(`<div class="stack" style="gap:10px">
      <div class="row"><button class="btn rec-btn" type="button"><svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6"/></svg><span>Record from microphone</span></button></div>
      <div class="meter hidden"><i></i></div>
      <label class="btn sm ghost" style="justify-self:start;cursor:pointer"><input type="file" accept="audio/*" class="hidden">Or upload an audio file…</label>
      <div class="clip-info small muted"></div>
    </div>`);
    const btn = el.querySelector(".rec-btn"), meter = el.querySelector(".meter"), info = el.querySelector(".clip-info");
    let rec = null, timer = null;
    const render = () => {
      if (!SW.clip) { info.innerHTML = "No clip yet. Max 20 s; speak naturally with pauses."; return; }
      const url = URL.createObjectURL(SW.wavBlob(SW.clip.pcm));
      info.innerHTML = `<div class="row" style="margin-bottom:6px"><span class="badge ok">Clip ready</span><span class="mono">${SW.clip.seconds.toFixed(1)} s · 16 kHz</span><span class="dim">${SW.esc(SW.clip.name)}</span></div><audio controls src="${url}"></audio>`;
    };
    const finish = async (pcm, name) => {
      if (pcm.length < 16000 * 0.5) { SW.toast("Clip is too short (min 0.5 s)", "warn"); return; }
      pcm = pcm.slice(0, 16000 * 20);
      try {
        const up = await SW.api("/api/audio/upload", { raw: pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength) });
        SW.clip = { pcm, uploadId: up.upload_id, seconds: up.seconds, name };
        document.dispatchEvent(new CustomEvent("sw:clip"));
        SW.toast(`Clip uploaded (${up.seconds.toFixed(1)} s)`, "ok");
        onReady && onReady(SW.clip);
      } catch (e) { SW.toast("Upload failed: " + e.message, "bad"); }
    };
    btn.onclick = async () => {
      if (rec) {
        clearInterval(timer); btn.classList.remove("recording"); meter.classList.add("hidden");
        btn.querySelector("span").textContent = "Record from microphone";
        const r = rec; rec = null; await finish(await r.stop(), "microphone recording"); return;
      }
      try {
        rec = new SW.Recorder();
        await rec.start((l) => (meter.firstElementChild.style.width = l * 100 + "%"));
        btn.classList.add("recording"); meter.classList.remove("hidden");
        timer = setInterval(() => {
          const t = rec ? rec.elapsed() : 0;
          btn.querySelector("span").textContent = `Recording… ${t.toFixed(1)} s — click to stop`;
          if (t >= 20) btn.click();
        }, 100);
      } catch (e) { rec = null; SW.toast("Microphone unavailable: " + e.message, "bad"); }
    };
    el.querySelector("input[type=file]").onchange = async (ev) => {
      const f = ev.target.files[0]; if (!f) return;
      try { await finish(await SW.decodeFile(f), f.name); } catch (e) { SW.toast("Could not decode that file: " + e.message, "bad"); }
      ev.target.value = "";
    };
    document.addEventListener("sw:clip", render);
    render();
    return el;
  };

  /* Shared network-impairment controls (Live Lab, Benchmark). */
  SW.PRESETS = [
    { id: "clean", title: "Clean", loss: 0, burst: 1, jitter: 0, delay: 0 },
    { id: "good_wifi", title: "Good Wi-Fi", loss: 0.01, burst: 1.5, jitter: 6, delay: 20 },
    { id: "busy_wifi", title: "Busy Wi-Fi", loss: 0.05, burst: 3, jitter: 14, delay: 35 },
    { id: "congested_4g", title: "Congested 4G", loss: 0.10, burst: 4, jitter: 25, delay: 60 },
    { id: "hostel_wifi", title: "Hostel Wi-Fi", loss: 0.15, burst: 8, jitter: 30, delay: 45 },
    { id: "poor_3g", title: "Poor 3G", loss: 0.20, burst: 6, jitter: 45, delay: 110 },
  ];
  SW.netControls = (init, onChange) => {
    const el = SW.h(`<div><div class="group-title">Network conditions</div><div class="row wrap presets" style="gap:6px;margin-bottom:14px"></div></div>`);
    const pr = el.querySelector(".presets");
    const fire = () => onChange && onChange(api.get());
    const s = {
      loss: SW.slider({ label: "Packet loss", min: 0, max: 40, step: 1, value: init.loss * 100, fmt: (v) => v + "%", onInput: () => { mark(); fire(); } }),
      burst: SW.slider({ label: "Mean loss burst", min: 1, max: 12, step: 0.5, value: init.burst, fmt: (v) => v.toFixed(1) + " pkts", onInput: () => { mark(); fire(); }, hint: "Gilbert–Elliott: how many packets each fade wipes out" }),
      jitter: SW.slider({ label: "Jitter", min: 0, max: 80, step: 1, value: init.jitter, fmt: (v) => v + " ms", onInput: () => { mark(); fire(); } }),
      extra_delay: SW.slider({ label: "Added one-way delay", min: 0, max: 300, step: 5, value: init.extra_delay || 0, fmt: (v) => v + " ms", onInput: () => { mark(); fire(); } }),
    };
    const btns = SW.PRESETS.map((p) => {
      const b = SW.h(`<button type="button" class="chip" style="height:26px;font-size:11.5px">${p.title}</button>`);
      b.onclick = () => { s.loss.set(p.loss * 100); s.burst.set(p.burst); s.jitter.set(p.jitter); s.extra_delay.set(p.delay); mark(); fire(); };
      pr.appendChild(b); return [p, b];
    });
    const mark = () => {
      const v = api.get();
      btns.forEach(([p, b]) => b.classList.toggle("on", Math.abs(p.loss - v.loss) < 1e-6 && p.burst === v.burst && p.jitter === v.jitter && p.delay === v.extra_delay));
    };
    Object.values(s).forEach((x) => el.appendChild(x.el));
    const api = {
      el,
      get: () => ({ loss: s.loss.get() / 100, burst: s.burst.get(), jitter: s.jitter.get(), extra_delay: s.extra_delay.get() }),
      set: (v) => { if (v.loss !== undefined) s.loss.set(v.loss * 100); if (v.burst !== undefined) s.burst.set(v.burst); if (v.jitter !== undefined) s.jitter.set(v.jitter); if (v.extra_delay !== undefined) s.extra_delay.set(v.extra_delay); mark(); },
      describe: () => { const v = api.get(); return v.loss === 0 && v.jitter === 0 && v.extra_delay === 0 ? "clean network" : `${(v.loss * 100).toFixed(0)}% loss · burst ${v.burst} · ${v.jitter} ms jitter${v.extra_delay ? ` · +${v.extra_delay} ms` : ""}`; },
    };
    mark();
    return api;
  };

  /* ---------- boot ---------- */
  SW.boot = () => {
    SW.chartsInit();
    SW.$("#menu-btn").onclick = () => SW.$("#sidebar").classList.toggle("open");
    window.addEventListener("hashchange", route);
    route();
    pollStatus();
    setInterval(pollStatus, 2500);
  };
})();
