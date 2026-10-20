/* Quality & NAVIK — the ITU-T G.107 E-model and the joint controller */
(function () {
  const { h, esc, fmt } = SW;
  let ui = {};

  async function emodel() {
    try {
      const d = await SW.api("/api/tools/emodel", { body: { codec: ui.codec.value, delay_ms: ui.delay.get(), loss_pct: ui.loss.get(), burst: ui.burst.get(), mobile: ui.mobile.get() } });
      ui.gauge.set(d.mos);
      const b = d.breakdown;
      ui.r.innerHTML = `R = <b style="color:${SW.mosColor(d.mos)}">${d.r.toFixed(1)}</b> · ${d.label}`;
      const terms = [["R₀ base signal-to-noise", b.R0, "#2dd4bf"], ["− Is simultaneous impairment", -b.Is, "#94a3b8"], ["− Id delay impairment", -b.Id, "#fbbf24"], ["− Ie,eff equipment + loss", -b.Ie_eff, "#fb7185"], ["+ A advantage factor", b.A, "#60a5fa"]];
      ui.terms.innerHTML = terms.map(([n, v, c]) => `<div class="row" style="gap:10px;margin-bottom:7px"><span class="small" style="width:190px;color:var(--text-2)">${n}</span>
        <div style="flex:1;height:10px;background:#16213a;border-radius:5px;overflow:hidden"><i style="display:block;height:100%;width:${Math.min(100, (Math.abs(v) / 93.2) * 100)}%;background:${c};border-radius:5px"></i></div>
        <span class="mono small" style="width:56px;text-align:right">${v >= 0 ? "+" : ""}${v.toFixed(1)}</span></div>`).join("") +
        `<div class="small dim mt-s">Codec ${esc(ui.codec.value)}: Ie = ${d.codec_params.ie}, Bpl = ${d.codec_params.bpl}, ${d.codec_params.kbps} kbit/s · burst ratio ${b.burst_ratio.toFixed(2)}</div>`;
      SW.chart(ui.cLoss, { type: "line", data: { labels: d.curve_loss.x, datasets: [{ label: "MOS", data: d.curve_loss.y, borderColor: "#2dd4bf", backgroundColor: SW.alpha("#2dd4bf", 0.12), fill: true, tension: 0.25, pointRadius: d.curve_loss.x.map((x) => (Math.abs(x - ui.loss.get()) < 0.26 ? 6 : 0)), pointBackgroundColor: "#fff" }] },
        options: { plugins: { legend: { display: false } }, scales: { x: SW.axis("packet loss %", { ticks: { maxTicksLimit: 8 } }), y: SW.axis("MOS", { min: 1, max: 4.5 }) } } });
      SW.chart(ui.cDelay, { type: "line", data: { labels: d.curve_delay.x, datasets: [{ label: "MOS", data: d.curve_delay.y, borderColor: "#fbbf24", backgroundColor: SW.alpha("#fbbf24", 0.12), fill: true, tension: 0.25, pointRadius: d.curve_delay.x.map((x) => (Math.abs(x - ui.delay.get()) < 5.1 ? 6 : 0)), pointBackgroundColor: "#fff" }] },
        options: { plugins: { legend: { display: false } }, scales: { x: SW.axis("mouth-to-ear delay ms", { ticks: { maxTicksLimit: 8 } }), y: SW.axis("MOS", { min: 1, max: 4.5 }) } } });
      SW.chart(ui.cCodec, { type: "bar", data: { labels: d.codecs.map((c) => c.codec), datasets: [{ label: "MOS", data: d.codecs.map((c) => c.mos), backgroundColor: d.codecs.map((c) => (c.codec === ui.codec.value ? "#2dd4bf" : SW.alpha(SW.mosColor(c.mos), 0.55))), borderRadius: 5 }] },
        options: { plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: (c) => `${d.codecs[c.dataIndex].kbps} kbit/s` } } }, scales: { y: SW.axis("MOS at these conditions", { min: 1, max: 4.5 }), x: { grid: { display: false } } } } });
    } catch (e) { SW.toast(e.message, "bad"); }
  }

  async function navik() {
    try {
      const d = await SW.api("/api/tools/navik", { body: { loss_pct: ui.n.loss.get(), burst: ui.n.burst.get(), jitter_ms: ui.n.jit.get(), owd_ms: ui.n.owd.get(), depth_ms: ui.n.depth.get(), capacity_kbps: ui.n.cap.get(), pi_afec: ui.n.pi.get(), mobile: false } });
      const x = d.decision;
      const tile = (l, v, s, c) => `<div class="kpi" style="--kc:${c}"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${s}</div></div>`;
      ui.dec.innerHTML = tile("Codec", esc(x.codec), `${x.bitrate_kbps} kbit/s media`, "var(--accent)") +
        tile("FEC redundancy ρ", fmt.pct(x.rho * 100, 1), `of media bitrate`, "var(--uniform)") +
        tile("Offered load", `${fmt.n(x.offered_kbps, 1)}<small>kbit/s</small>`, `budget ${fmt.n(ui.n.cap.get() * 0.85, 0)} kbit/s (85%)`, "var(--info)") +
        tile("Predicted MOS", `<span style="color:${SW.mosColor(x.predicted_mos)}">${fmt.n(x.predicted_mos, 2)}</span>`, `R = ${fmt.n(x.predicted_r, 1)}`, SW.mosColor(x.predicted_mos));
      ui.dscp.innerHTML = Object.entries(d.dscp).map(([k, v]) => `<tr><td>${k}</td><td class="num mono">${v}</td><td>${v === 46 ? '<span class="badge ok">EF</span>' : v === 34 ? '<span class="badge info">AF41</span>' : '<span class="badge">best effort</span>'}</td></tr>`).join("");
      const S = d.sweep;
      SW.chart(ui.cNav, {
        type: "line",
        data: { labels: S.loss.map((l) => l + "%"), datasets: [
          { label: "chosen ρ %", data: S.rho.map((r) => r * 100), borderColor: "#f59e0b", backgroundColor: SW.alpha("#f59e0b", 0.15), fill: true, stepped: true, yAxisID: "y" },
          { label: "codec kbit/s", data: S.kbps, borderColor: "#60a5fa", stepped: true, yAxisID: "y", borderDash: [5, 4] },
          { label: "predicted MOS", data: S.mos, borderColor: "#2dd4bf", yAxisID: "y2", tension: 0.3 }] },
        options: { interaction: { mode: "index", intersect: false }, plugins: { tooltip: { callbacks: { afterTitle: (it) => `codec ${S.codec[it[0].dataIndex]}` } } },
          scales: { x: SW.axis("measured packet loss"), y: SW.axis("ρ % / kbit/s", { beginAtZero: true }), y2: SW.axis("MOS", { position: "right", min: 1, max: 4.5, grid: { display: false } }) } },
      });
    } catch (e) { SW.toast(e.message, "bad"); }
  }

  SW.page("quality", {
    title: "Quality & NAVIK",
    group: "Protocol Toolkit",
    mount(el) {
      el.innerHTML = `
      <div class="section-title" style="margin-top:0"><h2>E-model calculator</h2><span class="sub">ITU-T G.107 · swara/emodel.py · R = R₀ − Is − Id − Ie,eff + A</span></div>
      <div class="split">
        <div class="card"><div class="card-b" id="q-cfg"></div></div>
        <div class="stack">
          <div class="grid" style="grid-template-columns:260px minmax(0,1fr)">
            <div class="card"><div class="card-b" style="display:grid;justify-items:center;gap:6px"><div id="q-gauge"></div><div class="small" id="q-r"></div></div></div>
            <div class="card"><div class="card-h"><h3>Where the rating goes</h3></div><div class="card-b" id="q-terms"></div></div>
          </div>
          <div class="grid g3">
            <div class="card"><div class="card-h"><h3>MOS vs loss</h3></div><div class="card-b"><div class="chart-box sm"><canvas id="q-loss"></canvas></div></div></div>
            <div class="card"><div class="card-h"><h3>MOS vs delay</h3></div><div class="card-b"><div class="chart-box sm"><canvas id="q-delay"></canvas></div></div></div>
            <div class="card"><div class="card-h"><h3>Every codec here</h3></div><div class="card-b"><div class="chart-box sm"><canvas id="q-codec"></canvas></div></div></div>
          </div>
        </div>
      </div>

      <div class="section-title"><h2>NAVIK controller</h2><span class="sub">swara/controller.py · picks codec, redundancy and DSCP jointly to maximise predicted R</span></div>
      <div class="split">
        <div class="card"><div class="card-b" id="q-ncfg"></div></div>
        <div class="stack">
          <div class="grid g4" id="q-dec"></div>
          <div class="grid" style="grid-template-columns:minmax(0,1.7fr) minmax(0,1fr)">
            <div class="card"><div class="card-h"><h3>Decision across loss rates</h3><span class="sub">other inputs held fixed</span></div><div class="card-b"><div class="chart-box"><canvas id="q-nav"></canvas></div></div></div>
            <div class="stack">
              <div class="card"><div class="card-h"><h3>Per-packet DiffServ marking</h3></div><table class="t"><thead><tr><th>Frame</th><th class="num">DSCP</th><th>Class</th></tr></thead><tbody id="q-dscp"></tbody></table></div>
              <div class="note small"><b>Cross-layer, without trust.</b> A router applying WRED drops the lowest-precedence class first. Marking onsets EF and silence best-effort makes the network's own drop decision perceptually informed — without the router reading the speech.</div>
            </div>
          </div>
        </div>
      </div>`;
      const cfg = el.querySelector("#q-cfg");
      const sel = h(`<div class="field"><label>Codec</label><select>${["G.711", "G.729", "opus-6k", "opus-8k", "opus-12k", "opus-16k", "opus-20k", "opus-24k", "opus-32k"].map((c) => `<option ${c === "opus-24k" ? "selected" : ""}>${c}</option>`).join("")}</select></div>`);
      ui.codec = sel.querySelector("select"); cfg.appendChild(sel);
      const go = SW.debounce(emodel, 120);
      ui.codec.onchange = go;
      ui.delay = SW.slider({ label: "Mouth-to-ear delay", min: 0, max: 500, step: 5, value: 150, fmt: (v) => v + " ms", onInput: go, hint: "Id rises sharply past ~177 ms" });
      ui.loss = SW.slider({ label: "Packet loss (after repair)", min: 0, max: 30, step: 0.5, value: 3, fmt: (v) => v + "%", onInput: go });
      ui.burst = SW.slider({ label: "Mean loss burst", min: 1, max: 10, step: 0.5, value: 1, fmt: (v) => v.toFixed(1), onInput: go, hint: "Bursty loss hurts more than random loss at the same rate" });
      [ui.delay, ui.loss, ui.burst].forEach((s) => cfg.appendChild(s.el));
      ui.mobile = SW.toggle("Mobile user (A = 10)", false, go); cfg.appendChild(ui.mobile.el);
      ui.gauge = SW.gauge(210); el.querySelector("#q-gauge").appendChild(ui.gauge.el);
      ui.r = el.querySelector("#q-r"); ui.terms = el.querySelector("#q-terms");
      ui.cLoss = el.querySelector("#q-loss"); ui.cDelay = el.querySelector("#q-delay"); ui.cCodec = el.querySelector("#q-codec");

      const ncfg = el.querySelector("#q-ncfg"), ngo = SW.debounce(navik, 120);
      ui.n = {
        loss: SW.slider({ label: "Measured loss", min: 0, max: 30, step: 0.5, value: 8, fmt: (v) => v + "%", onInput: ngo }),
        burst: SW.slider({ label: "Mean burst", min: 1, max: 10, step: 0.5, value: 3, fmt: (v) => v.toFixed(1), onInput: ngo }),
        jit: SW.slider({ label: "Jitter", min: 0, max: 80, value: 12, fmt: (v) => v + " ms", onInput: ngo }),
        owd: SW.slider({ label: "One-way delay", min: 0, max: 300, step: 5, value: 40, fmt: (v) => v + " ms", onInput: ngo }),
        depth: SW.slider({ label: "Jitter-buffer depth", min: 20, max: 300, step: 10, value: 80, fmt: (v) => v + " ms", onInput: ngo }),
        cap: SW.slider({ label: "Path capacity estimate", min: 10, max: 200, step: 2, value: 60, fmt: (v) => v + " kbit/s", onInput: ngo, hint: "Lower it to watch NAVIK trade codec bitrate for redundancy" }),
      };
      Object.values(ui.n).forEach((s) => ncfg.appendChild(s.el));
      ui.n.pi = SW.toggle("Perceptual placement (PI-AFEC)", true, ngo); ncfg.appendChild(ui.n.pi.el);
      ui.dec = el.querySelector("#q-dec"); ui.dscp = el.querySelector("#q-dscp"); ui.cNav = el.querySelector("#q-nav");
      emodel(); navik();
    },
  });
})();
