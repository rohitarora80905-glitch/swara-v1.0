/* Network Simulator — the offline evaluation engine (swara/engine.py) on demand */
(function () {
  const { h, esc, fmt } = SW;
  const SYS = ["swara", "uniform_fec", "baseline"];
  let ui = {}, profiles = [], busy = false;

  function params() {
    return {
      loss: ui.s.loss.get() / 100, burst: ui.s.burst.get(), delay: ui.s.delay.get(), jitter: ui.s.jitter.get(),
      capacity: ui.s.cap.get(), reorder: ui.s.reorder.get() / 100, duration: ui.s.dur.get(),
      seed: parseInt(ui.seed.value || "3", 10), mobile: ui.mobile.get(),
    };
  }
  function applyProfile(p) {
    ui.s.loss.set(p.loss * 100); ui.s.burst.set(p.burst); ui.s.delay.set(p.delay); ui.s.jitter.set(p.jitter);
    ui.s.cap.set(Math.min(3000, p.capacity)); ui.s.reorder.set(p.reorder * 100);
    SW.$$("button.chip", ui.prof).forEach((b) => b.classList.toggle("on", b.dataset.id === p.id));
  }

  async function run() {
    if (busy) return; busy = true;
    ui.go.disabled = true; ui.go.textContent = "Simulating…";
    try {
      const d = await SW.api("/api/sim/compare", { body: params() });
      render(d);
    } catch (e) { SW.toast(e.message, "bad"); }
    finally { busy = false; ui.go.disabled = false; ui.go.innerHTML = "Run simulation"; }
  }

  function render(d) {
    const S = d.systems, col = SW.armColor();
    ui.meta.innerHTML = `<span class="badge info">${esc(d.channel)}</span><span class="muted small">simulated ${params().duration}s × 3 systems in ${fmt.n(d.elapsed_ms, 0)} ms</span>`;
    ui.cards.innerHTML = "";
    SYS.forEach((k) => {
      const r = S[k];
      const card = h(`<div class="card" style="border-top:3px solid ${col[k]}">
        <div class="card-h"><h3 style="color:${col[k]}">${SW.armName[k]}</h3><span class="sub">${esc(r.codec)}</span><div class="right"><span class="badge" style="color:${SW.mosColor(r.mos)}">${r.quality}</span></div></div>
        <div class="card-b"><div class="g" style="display:grid;justify-items:center"></div>
          <dl class="kv mt">
            <dt>R-factor</dt><dd>${fmt.n(r.r_factor, 1)}</dd>
            <dt>Channel loss</dt><dd>${fmt.pct(r.channel_loss_pct, 2)}</dd>
            <dt>Residual loss</dt><dd>${fmt.pct(r.residual_loss_pct, 2)}</dd>
            <dt>Perceptual loss</dt><dd>${fmt.pct(r.perceptual_loss_pct, 2)}</dd>
            <dt>Mean PI of lost frames</dt><dd>${fmt.n(r.lost_pi_mean, 0)} <span class="dim">/ delivered ${fmt.n(r.delivered_pi_mean, 0)}</span></dd>
            <dt>Recovered (FEC / anchor)</dt><dd>${r.frames_recovered_fec} / ${r.frames_recovered_anchor}</dd>
            <dt>Redundancy</dt><dd>${fmt.pct(r.redundancy_pct, 1)}</dd>
            <dt>Wire rate</dt><dd>${fmt.n(r.wire_kbps, 1)} kbit/s</dd>
            <dt>Mouth-to-ear delay</dt><dd>${fmt.ms(r.mouth_to_ear_ms)}</dd>
            <dt>Playout delay</dt><dd>${fmt.ms(r.playout_delay_ms)}</dd>
          </dl></div></div>`);
      const g = SW.gauge(170); g.set(r.mos); card.querySelector(".g").appendChild(g.el);
      ui.cards.appendChild(card);
    });
    SW.chart(ui.cmp, {
      type: "bar",
      data: {
        labels: ["Channel loss", "Residual loss", "Perceptual loss", "Redundancy ÷ 10"],
        datasets: SYS.map((k) => ({ label: SW.armName[k], data: [S[k].channel_loss_pct, S[k].residual_loss_pct, S[k].perceptual_loss_pct, S[k].redundancy_pct / 10], backgroundColor: SW.alpha(col[k], 0.85), borderRadius: 5 })),
      },
      options: { scales: { y: SW.axis("%", { beginAtZero: true }), x: { grid: { display: false } } }, plugins: { legend: { position: "top", align: "end" } } },
    });
  }

  async function sweep() {
    ui.sw.disabled = true; ui.sw.textContent = "Sweeping 9 loss levels…";
    try {
      const d = await SW.api("/api/sim/sweep", { body: { ...params(), duration: Math.min(15, params().duration) } });
      const col = SW.armColor();
      ui.sweepBox.classList.remove("hidden");
      SW.chart(ui.sweepC, {
        type: "line",
        data: { labels: d.losses.map((x) => (x * 100).toFixed(0) + "%"), datasets: SYS.map((k) => ({ label: SW.armName[k], data: d[k].mos, borderColor: col[k], backgroundColor: col[k], pointRadius: 3.5, tension: 0.3 })) },
        options: { interaction: { mode: "index", intersect: false }, scales: { y: SW.axis("MOS", { min: 1, max: 4.5 }), x: SW.axis("channel loss (burst, delay & jitter as configured)") } },
      });
      ui.sweepMeta.textContent = `${fmt.n(d.elapsed_ms / 1000, 1)} s · ${d.losses.length * 3} simulated calls`;
    } catch (e) { SW.toast(e.message, "bad"); }
    finally { ui.sw.disabled = false; ui.sw.textContent = "Sweep packet loss 0–30%"; }
  }

  SW.page("sim", {
    title: "Network Simulator",
    group: "Analysis",
    mount(el) {
      el.innerHTML = `
      <div class="split">
        <div class="stack">
          <div class="card"><div class="card-h"><h3>Channel model</h3><span class="sub">Gilbert–Elliott + log-normal delay</span></div><div class="card-b" id="s-cfg"></div></div>
          <div class="note small">This page drives <code>swara/engine.py</code> — the same simulator that produced the paper's figures. It runs far faster than real time, so you can explore conditions a live call would take minutes to cover. All three systems get the <b>same redundancy budget</b>; only placement differs.</div>
        </div>
        <div class="stack">
          <div class="row between"><div class="row" id="s-meta"></div><button class="btn" id="s-sweep">Sweep packet loss 0–30%</button></div>
          <div class="grid g3" id="s-cards">${SW.empty("Choose a profile and run", "Results for SWARA, uniform FEC and the unprotected baseline appear here.")}</div>
          <div class="card"><div class="card-h"><h3>Loss accounting</h3><span class="sub">raw vs perceptually weighted — SWARA concedes silence to save speech</span></div><div class="card-b"><div class="chart-box"><canvas id="s-cmp"></canvas></div></div></div>
          <div class="card hidden" id="s-sweepbox"><div class="card-h"><h3>MOS vs packet loss</h3><span class="sub" id="s-sweepmeta"></span></div><div class="card-b"><div class="chart-box lg"><canvas id="s-sweepc"></canvas></div></div></div>
        </div>
      </div>`;
      const cfg = el.querySelector("#s-cfg");
      ui.prof = h(`<div class="row wrap" style="gap:6px;margin-bottom:14px"></div>`);
      cfg.appendChild(h(`<div class="group-title">Profiles</div>`)); cfg.appendChild(ui.prof);
      ui.s = {
        loss: SW.slider({ label: "Packet loss", min: 0, max: 40, value: 10, fmt: (v) => v + "%" }),
        burst: SW.slider({ label: "Mean loss burst", min: 1, max: 12, step: 0.5, value: 4, fmt: (v) => v.toFixed(1) }),
        delay: SW.slider({ label: "Base one-way delay", min: 0, max: 400, step: 5, value: 60, fmt: (v) => v + " ms" }),
        jitter: SW.slider({ label: "Jitter scale", min: 0, max: 80, value: 25, fmt: (v) => v + " ms" }),
        cap: SW.slider({ label: "Bottleneck capacity", min: 100, max: 3000, step: 50, value: 400, fmt: (v) => v + " kbit/s" }),
        reorder: SW.slider({ label: "Reordering", min: 0, max: 10, step: 0.5, value: 3, fmt: (v) => v + "%" }),
        dur: SW.slider({ label: "Call length", min: 5, max: 60, value: 20, fmt: (v) => v + " s" }),
      };
      Object.values(ui.s).forEach((s) => { cfg.appendChild(s.el); s.input.addEventListener("input", () => SW.$$("button.chip", ui.prof).forEach((b) => b.classList.remove("on"))); });
      ui.mobile = SW.toggle("Mobile handset (E-model advantage factor A = 10)", false);
      cfg.appendChild(ui.mobile.el);
      const adv = h(`<div class="field mt"><label>Seed</label><input type="number" value="3"></div>`);
      ui.seed = adv.querySelector("input"); cfg.appendChild(adv);
      ui.go = h(`<button class="btn primary lg block mt">Run simulation</button>`);
      ui.go.onclick = run; cfg.appendChild(ui.go);
      ui.cards = el.querySelector("#s-cards"); ui.meta = el.querySelector("#s-meta"); ui.cmp = el.querySelector("#s-cmp");
      ui.sw = el.querySelector("#s-sweep"); ui.sw.onclick = sweep;
      ui.sweepBox = el.querySelector("#s-sweepbox"); ui.sweepC = el.querySelector("#s-sweepc"); ui.sweepMeta = el.querySelector("#s-sweepmeta");
      SW.api("/api/sim/profiles").then((d) => {
        profiles = d.profiles;
        profiles.forEach((p) => {
          const b = h(`<button class="chip" style="height:26px;font-size:11.5px" data-id="${p.id}" title="${esc(p.label)}">${esc(p.title)}</button>`);
          b.onclick = () => { applyProfile(p); run(); };
          ui.prof.appendChild(b);
        });
        const def = profiles.find((p) => p.id === "congested_4g");
        if (def) { applyProfile(def); run(); }
      });
    },
  });
})();
