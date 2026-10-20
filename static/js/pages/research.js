/* Research Results — interactive versions of the paper's evaluation */
(function () {
  const { h, esc, fmt } = SW;
  const TABS = [
    ["loss", "Loss sweep", "fig1_loss_sweep.png"],
    ["burst", "Burst sweep", "fig2_burst_sweep.png"],
    ["overhead", "Overhead efficiency", "fig3_overhead_efficiency.png"],
    ["pi", "PI distribution", "fig4_pi_distribution.png"],
    ["jb", "Jitter buffer", "fig5_jitter_buffer.png"],
    ["hs", "Handshake", "fig6_handshake.png"],
    ["overlay", "SETU overlay", "fig7_overlay.png"],
    ["profiles", "Network profiles", "fig8_profiles.png"],
  ];
  const SYS = [["swara", "SWARA (PI-AFEC)"], ["uniform_fec", "Uniform FEC"], ["baseline", "Unprotected"]];
  let ui = {}, R = null, tab = "loss", figStamp = Date.now(), jobPoll = 0, jobSeen = 0;

  const finding = (title, text) => `<div class="note ok"><b>${title}</b><br>${text}</div>`;

  function layout(main, side) {
    return `<div class="grid" style="grid-template-columns:minmax(0,1.6fr) minmax(0,1fr)">${main}<div class="stack">${side}</div></div>`;
  }
  function figLink(file) {
    return `<div class="card"><div class="card-h"><h3>Paper figure</h3><span class="sub mono">${file}</span></div><div class="card-b tight"><div class="fig" data-fig="${file}"><img src="/figures/${file}?t=${figStamp}" alt="${file}" loading="lazy"></div></div></div>`;
  }

  function renderTab() {
    SW.$$(".tabs button", ui.root).forEach((b) => b.classList.toggle("on", b.dataset.t === tab));
    const box = ui.body, col = SW.armColor();
    if (!R) { box.innerHTML = SW.empty("No results yet", "Regenerate the experiments below."); return; }
    const fig = TABS.find((t) => t[0] === tab)[2];

    if (tab === "loss") {
      const L = R.loss_sweep, i12 = L.losses.indexOf(0.12), i30 = L.losses.length - 1;
      box.innerHTML = layout(`<div class="stack"><div class="card"><div class="card-h"><h3>MOS vs channel loss</h3><span class="sub">mean burst 3, 40 ms ± 14 ms, 5 seeds, shaded = ±1 s.e.</span></div><div class="card-b"><div class="chart-box lg"><canvas id="r1"></canvas></div></div></div>
        <div class="card"><div class="card-h"><h3>Perceptually weighted loss</h3></div><div class="card-b"><div class="chart-box"><canvas id="r2"></canvas></div></div></div></div>`,
        finding("Equal budget, better placement", `At 12% loss SWARA scores <b>${fmt.n(L.swara.mos[i12], 2)}</b> against ${fmt.n(L.uniform_fec.mos[i12], 2)} for uniform FEC and ${fmt.n(L.baseline.mos[i12], 2)} unprotected. Perceptual loss at 12% is ${fmt.pct(L.swara.perc[i12])} vs ${fmt.pct(L.baseline.perc[i12])}. By 30% every scheme converges (${fmt.n(L.swara.mos[i30], 2)} / ${fmt.n(L.baseline.mos[i30], 2)}): redundancy cannot replace a working channel.`) + figLink(fig));
      const ds = [];
      SYS.forEach(([s, lab]) => {
        const m = L[s].mos, e = L[s].err;
        ds.push({ label: lab, data: m, borderColor: col[s], backgroundColor: col[s], pointRadius: 3.5, tension: 0.3, order: 1 });
        ds.push({ label: "_hi", data: m.map((v, i) => v + e[i]), borderWidth: 0, pointRadius: 0, fill: false, order: 2 });
        ds.push({ label: "_lo", data: m.map((v, i) => v - e[i]), borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: SW.alpha(col[s], 0.18), order: 2 });
      });
      SW.chart(SW.$("#r1", box), { type: "line", data: { labels: L.losses.map((x) => (x * 100).toFixed(0) + "%"), datasets: ds },
        options: { interaction: { mode: "index", intersect: false }, plugins: { legend: { labels: { filter: (i) => !i.text.startsWith("_") } }, tooltip: { filter: (i) => !i.dataset.label.startsWith("_") } },
          scales: { y: SW.axis("MOS", { min: 2, max: 4.4 }), x: SW.axis("channel packet loss") } } });
      SW.chart(SW.$("#r2", box), { type: "bar", data: { labels: L.losses.map((x) => (x * 100).toFixed(0) + "%"), datasets: SYS.map(([s, lab]) => ({ label: lab, data: L[s].perc, backgroundColor: SW.alpha(col[s], 0.85), borderRadius: 4 })) },
        options: { scales: { y: SW.axis("perceptual loss %", { beginAtZero: true }), x: { grid: { display: false } } } } });
    }

    if (tab === "burst") {
      const B = R.burst_sweep;
      box.innerHTML = layout(`<div class="stack"><div class="card"><div class="card-h"><h3>MOS vs mean burst length</h3><span class="sub">fixed loss rate, burstiness varied</span></div><div class="card-b"><div class="chart-box lg"><canvas id="r1"></canvas></div></div></div>
        <div class="card"><div class="card-h"><h3>Which mechanism does the recovering</h3><span class="sub">SWARA frames recovered per call</span></div><div class="card-b"><div class="chart-box"><canvas id="r2"></canvas></div></div></div></div>`,
        finding("Bursts defeat block codes; anchors take over", `With isolated losses Reed–Solomon does almost all the repair (${fmt.n(B.fec_recoveries[0], 1)} frames/call at burst 1). As bursts approach the group span, FEC recoveries collapse to ${fmt.n(B.fec_recoveries[B.fec_recoveries.length - 1], 1)} while time-displaced anchor replicas keep working — which is why the scheduler switches into burst mode.`) + figLink(fig));
      SW.chart(SW.$("#r1", box), { type: "line", data: { labels: B.bursts, datasets: SYS.map(([s, lab]) => ({ label: lab, data: B[s], borderColor: col[s], backgroundColor: col[s], pointRadius: 3.5, tension: 0.3 })) },
        options: { interaction: { mode: "index", intersect: false }, scales: { y: SW.axis("MOS"), x: SW.axis("mean burst length (packets)") } } });
      SW.chart(SW.$("#r2", box), { type: "bar", data: { labels: B.bursts, datasets: [
        { label: "Reed–Solomon parity", data: B.fec_recoveries, backgroundColor: "#60a5fa", borderRadius: 4 },
        { label: "Anchor replicas", data: B.anchor_recoveries, backgroundColor: "#c084fc", borderRadius: 4 }] },
        options: { scales: { y: SW.axis("frames recovered", { beginAtZero: true, stacked: true }), x: SW.axis("mean burst length", { stacked: true }) } } });
    }

    if (tab === "overhead") {
      const O = R.overhead;
      box.innerHTML = layout(`<div class="card"><div class="card-h"><h3>Quality bought per unit of redundancy</h3><span class="sub">MOS against redundancy actually transmitted</span></div><div class="card-b"><div class="chart-box xl"><canvas id="r1"></canvas></div></div></div>`,
        finding("Same quality, less bandwidth", `For equal quality SWARA needs <b>${fmt.n(O.equal_quality_saving_pct, 1)}% less</b> redundancy than uniform FEC. At ρ = 0.5 it transmits ${fmt.n(O.swara_cost[O.swara_cost.length - 1], 1)}% overhead versus ${fmt.n(O.uniform_cost[O.uniform_cost.length - 1], 1)}% — silence frames are simply not protected.`) +
        `<div class="card"><div class="card-b"><table class="t"><thead><tr><th>ρ budget</th><th class="num">SWARA cost</th><th class="num">Uniform cost</th></tr></thead><tbody>${O.rho.map((r, i) => `<tr><td>${r}</td><td class="num">${fmt.pct(O.swara_cost[i])}</td><td class="num">${fmt.pct(O.uniform_cost[i])}</td></tr>`).join("")}</tbody></table></div></div>` + figLink(fig));
      SW.chart(SW.$("#r1", box), { type: "scatter", data: { datasets: [
        { label: "SWARA", data: O.swara_cost.map((x, i) => ({ x, y: O.swara_mos[i] })), borderColor: col.swara, backgroundColor: col.swara, showLine: true, pointRadius: 5, tension: 0.2 },
        { label: "Uniform FEC", data: O.uniform_cost.map((x, i) => ({ x, y: O.uniform_mos[i] })), borderColor: col.uniform, backgroundColor: col.uniform, showLine: true, pointRadius: 5, tension: 0.2 }] },
        options: { scales: { x: SW.axis("redundancy transmitted (% of media)"), y: SW.axis("MOS") } } });
    }

    if (tab === "pi") {
      const P = R.pi_distribution, cls = ["silence", "plosive", "unvoiced", "voiced", "onset"], cc = ["#475569", "#f59e0b", "#60a5fa", "#2dd4bf", "#c084fc"];
      box.innerHTML = layout(`<div class="stack"><div class="card"><div class="card-h"><h3>Perceptual importance by speech class</h3><span class="sub">bar = mean, whisker = p10–p90</span></div><div class="card-b"><div class="chart-box lg"><canvas id="r1"></canvas></div></div></div></div>`,
        finding("Silence is common and cheap", `${fmt.n(P.silence_frame_share * 100, 0)}% of frames are silence but they carry only <b>${fmt.n(P.silence_pi_share * 100, 2)}%</b> of the total perceptual importance. Onsets score ${fmt.n(P.onset.mean, 0)} on average, voiced frames ${fmt.n(P.voiced.mean, 0)}. That asymmetry is the whole opportunity.`) +
        `<div class="card"><div class="card-h"><h3>Share of frames vs share of importance</h3></div><div class="card-b"><div class="chart-box sm"><canvas id="r2"></canvas></div></div></div>` + figLink(fig));
      SW.chart(SW.$("#r1", box), { type: "bar", data: { labels: cls.map((c) => `${c} (n=${P[c].n})`), datasets: [
        { label: "p10–p90", data: cls.map((c) => [P[c].p10, P[c].p90]), backgroundColor: cc.map((c) => SW.alpha(c, 0.22)), borderColor: cc, borderWidth: 1, borderRadius: 4, barPercentage: 0.35, grouped: false },
        { label: "mean PI", data: cls.map((c) => P[c].mean), backgroundColor: cc, borderRadius: 6, barPercentage: 0.7, grouped: false }] },
        options: { scales: { y: SW.axis("PI score (0–255)", { min: 0, max: 260 }), x: { grid: { display: false } } } } });
      const tot = cls.reduce((a, c) => a + P[c].n, 0), totPi = cls.reduce((a, c) => a + P[c].n * P[c].mean, 0);
      SW.chart(SW.$("#r2", box), { type: "bar", data: { labels: ["frames", "importance"], datasets: cls.map((c, i) => ({ label: c, data: [100 * P[c].n / tot, 100 * P[c].n * P[c].mean / totPi], backgroundColor: cc[i] })) },
        options: { indexAxis: "y", scales: { x: SW.axis("%", { stacked: true, max: 100 }), y: { stacked: true, grid: { display: false } } }, plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.x.toFixed(1)}%` } } } } });
    }

    if (tab === "jb") {
      const J = R.jitter_buffer;
      box.innerHTML = layout(`<div class="card"><div class="card-h"><h3>Choosing the playout depth</h3><span class="sub">too shallow loses late packets, too deep adds delay</span></div><div class="card-b"><div class="chart-box xl"><canvas id="r1"></canvas></div></div></div>`,
        finding("The optimiser lands on the measured optimum", `The R-factor-optimal buffer picks <b>${fmt.n(J.chosen_ms, 0)} ms</b>; sweeping fixed depths in simulation puts the measured MOS peak at <b>${J.measured_optimum_ms} ms</b>. At 20 ms MOS collapses to ${fmt.n(J.measured_mos[0], 2)}; at 180 ms delay costs it back down to ${fmt.n(J.measured_mos[J.measured_mos.length - 1], 2)}.`) + figLink(fig));
      SW.chart(SW.$("#r1", box), { type: "scatter", data: { datasets: [
        { label: "predicted R-factor (left)", data: J.curve.map(([x, y]) => ({ x, y })), borderColor: "#60a5fa", backgroundColor: "#60a5fa", showLine: true, pointRadius: 3, yAxisID: "y", tension: 0.3 },
        { label: "measured MOS (right)", data: J.measured_depths.map((x, i) => ({ x, y: J.measured_mos[i] })), borderColor: "#2dd4bf", backgroundColor: "#2dd4bf", showLine: true, pointRadius: 5, yAxisID: "y2", tension: 0.3 },
        { label: `chosen ${fmt.n(J.chosen_ms, 0)} ms`, data: [{ x: J.chosen_ms, y: 0 }, { x: J.chosen_ms, y: 100 }], borderColor: "#fbbf24", borderDash: [6, 5], showLine: true, pointRadius: 0, yAxisID: "y" }] },
        options: { scales: { x: SW.axis("buffer depth (ms)", { min: 0, max: 200 }), y: SW.axis("R-factor", { min: 0, max: 100 }), y2: SW.axis("MOS", { position: "right", min: 1, max: 4.5, grid: { display: false } }) } } });
    }

    if (tab === "hs") {
      const H = R.handshake;
      box.innerHTML = layout(`<div class="stack"><div class="grid g2"><div class="card"><div class="card-h"><h3>Round trips to first media</h3></div><div class="card-b"><div class="chart-box"><canvas id="r1"></canvas></div></div></div>
        <div class="card"><div class="card-h"><h3>Bytes on the wire</h3></div><div class="card-b"><div class="chart-box"><canvas id="r2"></canvas></div></div></div></div>
        <div class="card"><div class="card-h"><h3>Measured costs</h3><div class="right"><a class="btn sm" href="#/handshake">Run a handshake live →</a></div></div><div class="card-b"><dl class="kv">
          <dt>SPARK full handshake</dt><dd>${H.spark_rtts} RTT · ${H.spark_bytes} B · ${fmt.n(H.spark_cpu_ms, 1)} ms CPU</dd>
          <dt>Path migration (Wi-Fi → mobile)</dt><dd>${H.spark_resume_rtts} RTT · ${H.migration_bytes} B · ${fmt.n(H.migration_cpu_ms * 1000, 1)} µs</dd>
          <dt>HELLO / RETRY</dt><dd>${H.hello_bytes} B / ${H.retry_bytes} B → amplification ${fmt.n(H.amplification_factor, 3)}×</dd>
          <dt>Server record per user</dt><dd>${H.db_record_bytes} B (no password-equivalent)</dd>
          <dt>AEAD seal per packet</dt><dd>${fmt.n(H.seal_us_per_packet, 2)} µs = ${fmt.n(H.seal_pct_of_frame_budget, 3)}% of a 20 ms frame</dd>
          <dt>DTLS-SRTP reference</dt><dd>${H.dtls_srtp_rtts} RTT · ${H.dtls_srtp_bytes} B</dd></dl></div></div></div>`,
        finding("Authenticated in 2 round trips, migrated in 0", `SPARK completes mutual password authentication with forward secrecy in ${H.spark_rtts} RTTs and ${H.spark_bytes} bytes — about ${fmt.n(H.dtls_srtp_bytes / H.spark_bytes, 1)}× smaller than DTLS-SRTP. A network handover costs zero round trips of silence.`) + figLink(fig));
      const lbl = ["Plain UDP", "SPARK", "SPARK resume", "DTLS-SRTP"];
      const cc = ["#94a3b8", "#2dd4bf", "#5eead4", "#f59e0b"];
      SW.chart(SW.$("#r1", box), { type: "bar", data: { labels: lbl, datasets: [{ data: [H.plain_rtts, H.spark_rtts, H.spark_resume_rtts, H.dtls_srtp_rtts], backgroundColor: cc, borderRadius: 6 }] }, options: { plugins: { legend: { display: false } }, scales: { y: SW.axis("RTTs", { beginAtZero: true }), x: { grid: { display: false } } } } });
      SW.chart(SW.$("#r2", box), { type: "bar", data: { labels: lbl, datasets: [{ data: [H.plain_bytes, H.spark_bytes, H.migration_bytes, H.dtls_srtp_bytes], backgroundColor: cc, borderRadius: 6 }] }, options: { plugins: { legend: { display: false } }, scales: { y: SW.axis("bytes", { beginAtZero: true }), x: { grid: { display: false } } } } });
    }

    if (tab === "overlay") {
      const O = R.overlay, hops = Object.keys(O.hop_histogram).sort();
      box.innerHTML = layout(`<div class="card"><div class="card-h"><h3>Path length chosen by SETU</h3><span class="sub">${O.pairs} node pairs across 300 random 6-node overlays</span></div><div class="card-b"><div class="chart-box xl"><canvas id="r1"></canvas></div></div></div>`,
        finding("Relays help when the direct path is bad", `For <b>${fmt.n(O.improved_pct, 1)}%</b> of pairs a relayed path beats the direct one, with a mean cost reduction of ${fmt.n(O.mean_gain_pct, 1)}% (median ${fmt.n(O.median_gain_pct, 1)}%). The distance-vector protocol converges in ${O.convergence_rounds} rounds.`) +
        `<div class="card"><div class="card-b row between"><span class="muted">Explore an overlay interactively</span><a class="btn sm" href="#/overlay">SETU Overlay →</a></div></div>` + figLink(fig));
      SW.chart(SW.$("#r1", box), { type: "bar", data: { labels: hops.map((k) => `${k} hop${k > 1 ? "s" : ""}${k === "1" ? " (direct)" : ""}`), datasets: [{ data: hops.map((k) => O.hop_histogram[k]), backgroundColor: ["#475569", "#2dd4bf", "#22d3ee", "#60a5fa"], borderRadius: 6 }] },
        options: { plugins: { legend: { display: false } }, scales: { y: SW.axis("pairs", { beginAtZero: true }), x: { grid: { display: false } } } } });
    }

    if (tab === "profiles") {
      const P = R.profiles;
      box.innerHTML = `<div class="stack"><div class="card"><div class="card-h"><h3>MOS across realistic networks</h3></div><div class="card-b"><div class="chart-box lg"><canvas id="r1"></canvas></div></div></div>
        <div class="grid" style="grid-template-columns:minmax(0,1.6fr) minmax(0,1fr)"><div class="card"><div class="table-wrap"><table class="t"><thead><tr><th>Profile</th><th>Channel</th>${SYS.map(([s, l]) => `<th class="num" style="color:${col[s]}">${l.split(" ")[0]} MOS</th>`).join("")}<th class="num">SWARA resid.</th><th class="num">SWARA delay</th></tr></thead><tbody>
        ${P.map((p) => `<tr><td><b>${esc(p.profile.replace(/_/g, " "))}</b></td><td class="small muted">${esc(p.label)}</td>${SYS.map(([s]) => `<td class="num" style="color:${SW.mosColor(p[s].mos)}">${fmt.n(p[s].mos, 2)}</td>`).join("")}<td class="num">${fmt.pct(p.swara.resid)}</td><td class="num">${fmt.ms(p.swara.delay)}</td></tr>`).join("")}
        </tbody></table></div></div><div class="stack">${finding("Never worse, often much better", "On clean links SWARA matches the baseline (it spends nothing it doesn't need). On lossy links it leads — at the cost of a deeper playout buffer, the price of being able to use its redundancy.")}${figLink(fig)}</div></div></div>`;
      SW.chart(SW.$("#r1", box), { type: "bar", data: { labels: P.map((p) => p.profile.replace(/_/g, " ")), datasets: SYS.map(([s, lab]) => ({ label: lab, data: P.map((p) => p[s].mos), backgroundColor: SW.alpha(col[s], 0.88), borderRadius: 5 })) },
        options: { scales: { y: SW.axis("MOS", { min: 1, max: 4.5 }), x: { grid: { display: false } } } } });
    }

    SW.$$(".fig", box).forEach((f) => (f.onclick = () => lightbox(f.dataset.fig)));
  }

  function lightbox(file) {
    const lb = h(`<div class="lightbox"><img src="/figures/${file}?t=${figStamp}" alt="${file}"></div>`);
    lb.onclick = () => lb.remove();
    document.body.appendChild(lb);
  }

  async function loadFigures() {
    try {
      const d = await SW.api("/figures");
      ui.gallery.innerHTML = d.figures.map((f) => `<div class="fig" data-fig="${f}"><img src="/figures/${f}?t=${figStamp}" alt="${f}" loading="lazy"><div class="cap">${f.replace(/^fig\d_|\.png$/g, "").replace(/_/g, " ")}</div></div>`).join("") || SW.empty("No figures", "Regenerate the experiments to create them.");
      SW.$$(".fig", ui.gallery).forEach((f) => (f.onclick = () => lightbox(f.dataset.fig)));
    } catch {}
  }

  async function load() {
    try { SW.cache.results = null; R = await SW.results(); } catch { R = null; }
    renderTab();
  }

  function jobRender(s) {
    if (s.lines.length) {
      s.lines.forEach((ln) => ui.log.appendChild(h(`<div class="ln ${/^\[\d\]/.test(ln) ? "phase" : ""}"><span>${esc(ln)}</span></div>`)));
      ui.log.scrollTop = ui.log.scrollHeight;
    }
    jobSeen = s.count;
    const all = SW.$$(".ln", ui.log).map((x) => x.textContent).join("\n");
    const m = all.match(/\[(\d)\]/g);
    const stage = m ? Math.max(...m.map((x) => +x[1])) : 0;
    ui.prog.style.width = s.running ? Math.min(97, (stage / 8) * 100) + "%" : s.returncode === 0 ? "100%" : ui.prog.style.width;
    ui.jobState.innerHTML = s.running ? `<span class="badge live">RUNNING</span> <span class="mono small">${fmt.dur(s.elapsed_s)} · stage ${stage}/8</span>`
      : s.returncode === 0 ? `<span class="badge ok">COMPLETE</span> <span class="mono small">${fmt.dur(s.elapsed_s)}</span>`
      : s.returncode != null ? `<span class="badge bad">FAILED (${s.returncode})</span>` : `<span class="badge">IDLE</span>`;
    ui.run.disabled = s.running;
  }
  async function jobTick() {
    try {
      const s = await SW.api(`/api/experiments/job?since=${jobSeen}`);
      jobRender(s);
      if (!s.running) {
        clearInterval(jobPoll); jobPoll = 0;
        if (s.returncode === 0) { figStamp = Date.now(); await load(); loadFigures(); SW.toast("Experiments regenerated — charts and figures updated", "ok"); }
      }
    } catch { clearInterval(jobPoll); jobPoll = 0; }
  }

  SW.page("research", {
    title: "Research Results",
    group: "Analysis",
    mount(el) {
      el.innerHTML = `
        <div class="tabs">${TABS.map(([k, n]) => `<button data-t="${k}">${n}</button>`).join("")}</div>
        <div id="r-body"></div>
        <div class="section-title"><h2>Paper figures</h2><span class="sub">rendered by <code>experiments/run_all.py</code> — click to enlarge</span></div>
        <div class="fig-grid" id="r-gallery"></div>
        <div class="section-title"><h2>Regenerate the evaluation</h2><span class="sub">re-runs all eight experiments (≈ 3 minutes) and rewrites results.json and the figures</span></div>
        <div class="card"><div class="card-h"><div id="r-state"><span class="badge">IDLE</span></div><div class="right"><button class="btn primary" id="r-run">Run all experiments</button></div></div>
          <div class="card-b"><div class="progress"><i id="r-prog"></i></div><div class="console mt" id="r-log" style="height:180px"></div></div></div>`;
      ui.root = el; ui.body = el.querySelector("#r-body"); ui.gallery = el.querySelector("#r-gallery");
      ui.log = el.querySelector("#r-log"); ui.prog = el.querySelector("#r-prog"); ui.jobState = el.querySelector("#r-state"); ui.run = el.querySelector("#r-run");
      SW.$$(".tabs button", el).forEach((b) => (b.onclick = () => { tab = b.dataset.t; renderTab(); }));
      ui.run.onclick = async () => {
        try { ui.log.innerHTML = ""; jobSeen = 0; jobRender(await SW.api("/api/experiments/run", { method: "POST" })); if (!jobPoll) jobPoll = setInterval(jobTick, 1000); }
        catch (e) { SW.toast(e.message, "bad"); }
      };
      load(); loadFigures();
      SW.api("/api/experiments/job").then((s) => { jobRender(s); if (s.running && !jobPoll) jobPoll = setInterval(jobTick, 1000); });
    },
  });
})();
