/* Overview — the system at a glance */
(function () {
  const { h, esc, fmt } = SW;

  const SENDER = [
    ["01", "Capture", "16 kHz mono, 20 ms frames from mic, WAV or synthetic speech", "audio_io.py", "lab"],
    ["02", "ADPCM codec", "IMA-ADPCM with per-frame state reset: one loss cannot cascade", "codec.py", "packet"],
    ["03", "PI analyser", "Energy, voicing, spectral flux, onset → importance 0–255", "perceptual.py", "perceptual"],
    ["04", "PI-AFEC", "Reed–Solomon parity + perceptual anchor replicas", "fec.py", "fec"],
    ["05", "AEAD seal", "ChaCha20-Poly1305; header authenticated as AAD", "auth.py", "handshake"],
    ["06", "UDP + DSCP", "EF for onsets, AF41 / best-effort for silence", "net.py", "packet"],
  ];
  const RECEIVER = [
    ["07", "AEAD open", "Authenticate, unwrap 16-bit seq, 64-bit replay window", "auth.py", "handshake"],
    ["08", "Repair", "Rebuild lost frames from parity or replicas", "session.py", "fec"],
    ["09", "Estimator", "Loss, burst length, RFC 3550 jitter, delay trend", "receiver.py", "sim"],
    ["10", "NAVIK", "Joint codec · redundancy · depth optimiser", "controller.py", "quality"],
    ["11", "Jitter buffer", "R-factor-optimal playout depth", "receiver.py", "research"],
    ["12", "Decode + PLC", "Conceal only what cannot be repaired", "codec.py", "lab"],
  ];
  const DECISIONS = [
    ["Per-frame codec state reset", "Standard ADPCM carries predictor state across frames, so one lost packet corrupts everything after it. SWARA resets per frame and sends a 3-byte preamble — slightly less efficient, vastly more robust."],
    ["Playout depth bounds protection", "Parity is emitted only when a group closes, and a replica must beat its own frame's deadline. Paying for redundancy the buffer is too shallow to use is waste, so depth and protection are decided together."],
    ["Congestion is not the same as loss", "Naive AIMD backs off on any loss — right on a wired path, wrong on wireless. NAVIK backs off only when loss comes with a rising one-way-delay trend."],
    ["The relay cannot hear the call", "Media keys derive from a call secret delivered to each party under its own session key; the server forwards datagrams verbatim and sees only a connection id and a length."],
    ["Time-based channel model", "Impairment advances on a wall clock, not per packet. Per-packet stepping made three arms configured for 15% loss measure 9.5%, 12.3% and 12.5% — an instrument artefact."],
  ];
  const LIMITS = [
    "Simulation MOS figures come from the ITU-T G.107 model, not human listeners.",
    "The research corpus is generated speech, not recorded human speech.",
    "Live calls use real UDP, but impairment is injected locally rather than met in the wild.",
    "At mean burst ≥ 10 packets every repair mechanism fails and redundancy becomes wasted bandwidth.",
    "SPARK composes well-analysed primitives but has no formal security proof.",
  ];

  function node([n, name, desc, file, page]) {
    const el = h(`<div class="pipe-node"><div class="pn-step">${n}</div><div class="pn-name">${name}</div><div class="pn-desc">${desc}</div><div class="pn-file">swara/${file}</div></div>`);
    el.onclick = () => SW.go(page);
    return el;
  }

  // Animated hero art: a scrolling stream of frames, height = perceptual
  // importance, with occasional losses being repaired (blue/violet) or concealed.
  function heroArt(canvas) {
    const ctx = canvas.getContext("2d");
    let frames = [], t = 0, raf = 0, phase = 0;
    const gen = () => {
      phase += 1;
      const spurt = Math.sin(phase / 23) + Math.sin(phase / 7.3) * 0.4;
      const speech = spurt > -0.2;
      const pi = speech ? Math.min(1, 0.45 + Math.random() * 0.5 + (Math.random() < 0.08 ? 0.3 : 0)) : Math.random() * 0.08;
      let fate = 0;
      const r = Math.random();
      if (r < 0.07) fate = pi > 0.55 ? (Math.random() < 0.6 ? 1 : 2) : 3;
      return { pi, fate };
    };
    const draw = () => {
      const w = (canvas.width = canvas.clientWidth * devicePixelRatio), hgt = (canvas.height = canvas.clientHeight * devicePixelRatio);
      const bw = 7 * devicePixelRatio, gap = 3 * devicePixelRatio, n = Math.ceil(w / (bw + gap)) + 1;
      t += 0.35;
      while (frames.length < n + 2) frames.push(gen());
      if (t >= 1) { t = 0; frames.shift(); frames.push(gen()); }
      ctx.clearRect(0, 0, w, hgt);
      const cols = ["#2dd4bf", "#60a5fa", "#c084fc", "#fb7185"];
      for (let i = 0; i < n; i++) {
        const f = frames[i]; if (!f) continue;
        const x = i * (bw + gap) - t * (bw + gap);
        const fh = Math.max(3, f.pi * hgt * 0.86);
        ctx.fillStyle = f.fate === 0 ? SW.alpha("#2dd4bf", 0.25 + f.pi * 0.6) : cols[f.fate];
        ctx.beginPath();
        ctx.roundRect ? ctx.roundRect(x, (hgt - fh) / 2, bw, fh, 3) : ctx.rect(x, (hgt - fh) / 2, bw, fh);
        ctx.fill();
      }
      raf = requestAnimationFrame(draw);
    };
    return { start: () => { if (!raf) raf = requestAnimationFrame(draw); }, stop: () => { cancelAnimationFrame(raf); raf = 0; } };
  }

  let art;
  SW.page("overview", {
    title: "Overview",
    group: "Console",
    mount(el) {
      el.innerHTML = `
      <div class="hero">
        <div>
          <span class="badge accent">Real UDP · Real ADPCM · Real X25519 / ChaCha20-Poly1305</span>
          <h1 style="margin-top:14px">Voice over a hostile network,<br><span class="grad">protected where it is heard.</span></h1>
          <p class="lead">SWARA is a working voice-chat system over UDP. It scores every 20 ms of speech by perceptual importance and spends its redundancy budget on the frames a listener would actually miss — with mutual password authentication that never sends the password and a relay that cannot hear the call.</p>
          <div class="cta">
            <a class="btn primary lg" href="#/lab"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>Start a live call</a>
            <a class="btn lg" href="#/bench">Run the A/B benchmark</a>
            <a class="btn lg ghost" href="#/research">See the evaluation →</a>
          </div>
        </div>
        <div class="card" style="background:rgba(5,9,18,.55);padding:16px 16px 12px">
          <div class="row between small"><span class="muted">Frame stream · height = perceptual importance</span><span class="badge live">SIM</span></div>
          <canvas class="wave-art" id="hero-art"></canvas>
          <div class="fate-legend mt-s"><span><i class="swatch" style="background:#2dd4bf"></i>delivered</span><span><i class="swatch" style="background:#60a5fa"></i>rebuilt from parity</span><span><i class="swatch" style="background:#c084fc"></i>anchor replica</span><span><i class="swatch" style="background:#fb7185"></i>concealed</span></div>
        </div>
      </div>

      <div class="section-title"><h2>Headline results</h2><span class="sub">from <code>experiments/results.json</code> — the offline evaluation</span></div>
      <div class="grid g6" id="ov-kpis"></div>

      <div class="section-title"><h2>The media pipeline</h2><span class="sub">every stage is a real module — click one to explore it</span></div>
      <div class="card"><div class="card-b">
        <div class="pipe-lane">Sender · alice</div>
        <div class="pipeline" id="pipe-s"></div>
        <div class="wire mt" id="wire"><span class="lbl">UDP → <b>blind relay</b> (server.py) forwards ciphertext verbatim · holds no media keys → UDP</span></div>
        <div class="pipe-lane mt">Receiver · bob</div>
        <div class="pipeline" id="pipe-r"></div>
      </div></div>

      <div class="grid g2 mt-l">
        <div class="card">
          <div class="card-h"><h3>Quality under loss</h3><span class="sub">MOS vs channel loss, equal redundancy budget</span><div class="right"><a class="btn sm ghost" href="#/research">Details</a></div></div>
          <div class="card-b"><div class="chart-box"><canvas id="ov-loss"></canvas></div></div>
        </div>
        <div class="card">
          <div class="card-h"><h3>Where the bits go</h3><span class="sub">perceptual importance by speech class</span></div>
          <div class="card-b"><div class="chart-box"><canvas id="ov-pi"></canvas></div></div>
        </div>
      </div>

      <div class="grid g2 mt-l">
        <div>
          <div class="section-title" style="margin-top:0"><h2>Design decisions</h2><span class="sub">the choices worth defending</span></div>
          <div class="decision-list" id="ov-dec"></div>
        </div>
        <div class="stack">
          <div>
            <div class="section-title" style="margin-top:0"><h2>Hosted relay</h2><span class="sub">real CLI clients can connect right now</span></div>
            <div class="card"><div class="card-b" id="ov-relay"></div></div>
          </div>
          <div class="card"><div class="card-h"><h3>Honest limitations</h3></div><div class="card-b"><ul id="ov-lim" style="margin:0;padding-left:18px;color:var(--text-2);display:grid;gap:7px;font-size:13px"></ul></div></div>
        </div>
      </div>`;

      SENDER.forEach((n) => el.querySelector("#pipe-s").appendChild(node(n)));
      RECEIVER.forEach((n) => el.querySelector("#pipe-r").appendChild(node(n)));
      const wire = el.querySelector("#wire");
      for (let i = 0; i < 7; i++) {
        const p = h(`<i class="pkt"></i>`);
        p.style.animationDelay = `${-i * 0.46}s`;
        if (i === 3) p.style.background = "#f59e0b";
        wire.appendChild(p);
      }
      DECISIONS.forEach(([t, d], i) => el.querySelector("#ov-dec").appendChild(h(`<div class="decision"><div class="num">${i + 1}</div><div><h4>${t}</h4><p>${d}</p></div></div>`)));
      LIMITS.forEach((l) => el.querySelector("#ov-lim").appendChild(h(`<li>${l}</li>`)));
      art = heroArt(el.querySelector("#hero-art"));
      this.load(el);
      document.addEventListener("sw:status", () => this.relay(el));
    },
    async load(el) {
      let R;
      try { R = await SW.results(); } catch (e) {
        el.querySelector("#ov-kpis").innerHTML = `<div class="note warn" style="grid-column:1/-1">Research results are not available yet (${esc(e.message)}). Regenerate them from Research Results.</div>`;
        return;
      }
      const ls = R.loss_sweep, i12 = ls.losses.findIndex((x) => Math.abs(x - 0.12) < 1e-9);
      const hs = R.handshake, ov = R.overlay, pd = R.pi_distribution;
      const k = el.querySelector("#ov-kpis");
      const tiles = [
        ["MOS at 12% loss", `${fmt.n(ls.swara.mos[i12], 2)}`, `vs ${fmt.n(ls.baseline.mos[i12], 2)} unprotected · +${fmt.n(ls.swara.mos[i12] - ls.baseline.mos[i12], 2)}`, "var(--swara)"],
        ["Bandwidth at equal quality", `−${fmt.n(R.overhead.equal_quality_saving_pct, 1)}<small>%</small>`, "redundancy saved vs uniform FEC", "var(--uniform)"],
        ["Authentication", `${hs.spark_rtts}<small>RTT</small> · ${hs.spark_bytes}<small>B</small>`, `DTLS-SRTP: ${hs.dtls_srtp_rtts} RTT · ${hs.dtls_srtp_bytes} B`, "var(--info)"],
        ["Amplification factor", `${fmt.n(hs.amplification_factor, 2)}<small>×</small>`, "RETRY is smaller than HELLO", "var(--ok)"],
        ["Silence: frames vs importance", `${fmt.n(pd.silence_frame_share * 100, 0)}<small>%</small> → ${fmt.n(pd.silence_pi_share * 100, 1)}<small>%</small>`, "of frames · of perceptual weight", "var(--replica)"],
        ["SETU relay paths", `${fmt.n(ov.improved_pct, 0)}<small>%</small>`, `of pairs improved · median −${fmt.n(ov.median_gain_pct, 0)}% cost`, "var(--accent-2)"],
      ];
      tiles.forEach(([l, v, s, c]) => { const t = SW.kpi(l, c); t.set(v, s); k.appendChild(t.el); });

      const col = SW.armColor();
      SW.chart(el.querySelector("#ov-loss"), {
        type: "line",
        data: {
          labels: ls.losses.map((x) => (x * 100).toFixed(0) + "%"),
          datasets: [["swara", "SWARA (PI-AFEC)"], ["uniform_fec", "Uniform FEC"], ["baseline", "Unprotected"]].map(([s, lab]) => ({
            label: lab, data: ls[s].mos, borderColor: col[s], backgroundColor: SW.alpha(col[s], 0.12), pointRadius: 3, pointBackgroundColor: col[s], tension: 0.3, fill: s === "swara" ? "+1" : false,
          })),
        },
        options: { interaction: { mode: "index", intersect: false }, scales: { y: SW.axis("MOS", { min: 2, max: 4.5 }), x: SW.axis("channel packet loss") }, plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y.toFixed(2)}` } } } },
      });
      const classes = ["silence", "plosive", "unvoiced", "voiced", "onset"];
      SW.chart(el.querySelector("#ov-pi"), {
        type: "bar",
        data: {
          labels: classes.map((c) => c[0].toUpperCase() + c.slice(1)),
          datasets: [
            { label: "mean PI score", data: classes.map((c) => pd[c].mean), backgroundColor: ["#475569", "#f59e0b", "#60a5fa", "#2dd4bf", "#c084fc"], borderRadius: 6, yAxisID: "y" },
            { label: "frames analysed", data: classes.map((c) => pd[c].n), type: "line", borderColor: "#e7eef9", backgroundColor: "#e7eef9", pointRadius: 4, borderDash: [4, 4], yAxisID: "y2" },
          ],
        },
        options: { scales: { y: SW.axis("perceptual importance (0–255)", { min: 0, max: 255 }), y2: SW.axis("frames", { position: "right", grid: { display: false } }), x: { grid: { display: false } } } },
      });
      this.relay(el);
    },
    relay(el) {
      const s = SW.status, box = el.querySelector("#ov-relay");
      if (!box || !s) return;
      const r = s.relay;
      const host = location.hostname === "localhost" ? "127.0.0.1" : location.hostname;
      box.innerHTML = `<div class="row between"><div class="row"><span class="badge ${r.running ? "ok" : "bad"}">${r.running ? "RUNNING" : "STOPPED"}</span><span class="mono">udp://${r.host}:${r.port}</span></div><a class="btn sm" href="#/relay">Manage</a></div>
        <div class="grid g2 mt"><div class="kpi"><div class="k-label">Users online</div><div class="k-value">${r.online}</div></div><div class="kpi"><div class="k-label">Calls relayed now</div><div class="k-value">${r.calls}</div></div></div>
        <div class="small muted mt">Connect two terminals to this relay:</div>`;
      box.appendChild(SW.codeBlock(`python client.py --user bob --password secret2 --server ${host}:${r.port} --wait\npython client.py --user alice --password secret1 --server ${host}:${r.port} --call bob`));
    },
    show() { art && art.start(); },
    hide() { art && art.stop(); },
  });
})();
