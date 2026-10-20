/* A/B Benchmark — the same call under SWARA, uniform FEC and no protection */
(function () {
  const { h, esc, fmt } = SW;
  const ARMS = ["swara", "uniform", "none"];
  const DESC = {
    swara: "PI-AFEC: parity + anchor replicas placed by perceptual importance",
    uniform: "Conventional FEC: the same budget spread evenly across frames",
    none: "Plain UDP: no repair, concealment only",
  };
  let ui = {}, run = null, poll = 0;

  function armCard(a) {
    const c = SW.armColor()[a];
    const el = h(`<div class="card" style="border-top:3px solid ${c}">
      <div class="card-h"><h3 style="color:${c}">${SW.armName[a]}</h3><span class="badge right-badge">queued</span></div>
      <div class="card-b">
        <div class="small muted" style="min-height:36px">${DESC[a]}</div>
        <div class="progress mt-s"><i></i></div>
        <div class="grid g2 mt" style="gap:10px">
          <div><div class="tiny muted">Perceptual damage</div><div class="mono" style="font-size:20px;font-weight:700" data-k="perc">—</div></div>
          <div><div class="tiny muted">Unrecoverable</div><div class="mono" style="font-size:20px;font-weight:700" data-k="after">—</div></div>
          <div><div class="tiny muted">Wire rate</div><div class="mono" data-k="kbps">—</div></div>
          <div><div class="tiny muted">Rebuilt / concealed</div><div class="mono" data-k="rep">—</div></div>
        </div>
      </div></div>`);
    el.querySelector(".right-badge").style.marginLeft = "auto";
    return el;
  }

  function setArm(a, state, data, prog) {
    const el = ui.arms[a];
    const b = el.querySelector(".right-badge");
    b.className = "badge right-badge " + ({ running: "live", done: "ok", queued: "", error: "bad" }[state] || "");
    b.textContent = state === "running" ? "RUNNING" : state.toUpperCase();
    b.style.marginLeft = "auto";
    if (prog !== undefined) el.querySelector(".progress > i").style.width = Math.min(100, prog * 100) + "%";
    if (!data) return;
    const q = (k) => el.querySelector(`[data-k="${k}"]`);
    if (data.perceptual_pct !== undefined) {
      q("perc").textContent = fmt.pct(data.perceptual_pct); q("after").textContent = fmt.pct(data.loss_after_repair);
      q("kbps").textContent = fmt.n(data.tx_kbps, 1) + " kbit/s"; q("rep").textContent = `${fmt.n(data.fec_rec + data.rep_rec, 0)} / ${fmt.n(data.concealed, 0)}`;
    } else {
      q("perc").textContent = "…"; q("after").textContent = fmt.pct(data.loss_after);
      q("kbps").textContent = fmt.n(data.kbps, 0) + " kbit/s"; q("rep").textContent = `${data.fec + data.replica} / ${data.concealed}`;
    }
  }

  function reset() {
    ARMS.forEach((a) => {
      setArm(a, "queued", null, 0);
      SW.$$("[data-k]", ui.arms[a]).forEach((x) => (x.textContent = "—"));
    });
    ui.out.classList.add("hidden");
  }

  async function start() {
    const body = { ...ui.net.get(), seconds: ui.dur.get(), repeat: parseInt(ui.rep.get(), 10), source: ui.src.get(), seed: 7 };
    if (body.source === "upload") {
      if (!SW.clip) { SW.toast("Record or upload a clip first", "warn"); return; }
      body.upload_id = SW.clip.uploadId;
    }
    try {
      const d = await SW.api("/api/lab/bench", { body });
      track(d);
    } catch (e) { SW.toast(e.message, "bad"); }
  }

  function track(d) {
    run = d; reset();
    ui.start.classList.add("hidden"); ui.stop.classList.remove("hidden");
    const total = 3 * d.repeat * (d.params.seconds + 5);
    ui.eta.textContent = `≈ ${Math.round(total)} s for ${3 * d.repeat} calls`;
    clearInterval(poll);
    poll = setInterval(async () => {
      try { update(await SW.api(`/api/lab/${d.id}`)); } catch (e) { clearInterval(poll); }
    }, 500);
  }

  function update(d) {
    run = d;
    ARMS.forEach((a) => {
      if (d.results[a]) setArm(a, "done", d.results[a], 1);
      else if (d.live && d.live.arm === a) {
        const s = d.live.sample, runs = d.arms[a].runs;
        const prog = ((runs - 1) + (s ? Math.min(1, s.t / (d.params.seconds + 2)) : 0)) / d.repeat;
        setArm(a, "running", s, prog);
      }
    });
    if (d.done) {
      clearInterval(poll);
      ui.start.classList.remove("hidden"); ui.stop.classList.add("hidden"); ui.eta.textContent = "";
      if (d.error) { SW.toast("Benchmark stopped: " + d.error, "bad"); ARMS.forEach((a) => !d.results[a] && setArm(a, "error")); return; }
      render(d);
    }
  }

  function render(d) {
    const R = d.results, s = R.swara, u = R.uniform, n = R.none;
    ui.out.classList.remove("hidden");
    const dmgVsNone = 100 * (1 - s.perceptual_pct / Math.max(1e-9, n.perceptual_pct));
    const dmgVsUni = 100 * (1 - s.perceptual_pct / Math.max(1e-9, u.perceptual_pct));
    const bw = 100 * (1 - s.tx_kbps / Math.max(1e-9, u.tx_kbps));
    const p = d.params;
    ui.verdict.innerHTML = `<h3>${dmgVsNone > 0 ? `SWARA cut perceptual damage by ${fmt.n(dmgVsNone, 0)}% versus unprotected UDP` : "SWARA did not beat unprotected UDP on this run"}</h3>
      <p>${dmgVsUni >= 0 ? `${fmt.n(dmgVsUni, 0)}% less perceptual damage than uniform FEC` : `${fmt.n(-dmgVsUni, 0)}% more perceptual damage than uniform FEC`}, while using
      <b>${bw >= 0 ? fmt.n(bw, 0) + "% less" : fmt.n(-bw, 0) + "% more"}</b> bandwidth (${fmt.n(s.tx_kbps, 0)} vs ${fmt.n(u.tx_kbps, 0)} kbit/s).
      Raw unrecoverable frames: SWARA ${fmt.pct(s.loss_after_repair)}, uniform ${fmt.pct(u.loss_after_repair)}, none ${fmt.pct(n.loss_after_repair)}.</p>
      <p class="small dim">Network: ${(p.loss * 100).toFixed(0)}% loss, mean burst ${p.burst}, ${p.jitter} ms jitter · ${p.seconds} s per call · ${d.repeat} run${d.repeat > 1 ? "s" : ""} per arm${d.repeat < 2 || p.seconds < 25 ? " · short single runs are noisy; use 25 s+ and 2–3 repeats for a stable figure" : ""}.</p>`;

    const col = SW.armColor(), labels = ARMS.map((a) => SW.armName[a]);
    const bar = (cv, key, title, f = (v) => v, dec = 1) => SW.chart(cv, {
      type: "bar",
      data: { labels, datasets: [{ data: ARMS.map((a) => f(R[a][key])), backgroundColor: ARMS.map((a) => SW.alpha(col[a], 0.85)), borderRadius: 7, maxBarThickness: 64 }] },
      options: { plugins: { legend: { display: false }, title: { display: true, text: title, color: "#e7eef9", font: { weight: 600, size: 12.5 } }, tooltip: { callbacks: { label: (c) => c.parsed.y.toFixed(dec) } } }, scales: { y: SW.axis("", { beginAtZero: true }), x: { grid: { display: false } } } },
    });
    bar(ui.c1, "perceptual_pct", "Perceptual damage % (lower is better)");
    bar(ui.c2, "loss_after_repair", "Frames unrecoverable % (lower is better)");
    bar(ui.c3, "tx_kbps", "Wire rate kbit/s");
    bar(ui.c4, "snr_db", "Waveform SNR dB (higher is better)");

    const rows = [
      ["Perceptual damage", "perceptual_pct", (v) => fmt.pct(v), true],
      ["Frames unrecoverable", "loss_after_repair", (v) => fmt.pct(v), true],
      ["Channel loss (dropped datagrams)", "channel_loss_pct", (v) => fmt.pct(v)],
      ["Estimated MOS (E-model)", "mos", (v) => fmt.n(v, 2), false, true],
      ["Waveform correlation", "corr", (v) => fmt.n(v, 3), false, true],
      ["Waveform SNR", "snr_db", (v) => fmt.n(v, 1) + " dB", false, true],
      ["Wire rate", "tx_kbps", (v) => fmt.n(v, 1) + " kbit/s", true],
      ["Frames sent", "sent_frames", (v) => fmt.int(v)],
      ["Parity packets sent", "parity", (v) => fmt.int(v)],
      ["Anchor replicas sent", "replicas", (v) => fmt.int(v)],
      ["Frames rebuilt by FEC", "fec_rec", (v) => fmt.n(v, 1)],
      ["Replicas received", "rep_rec", (v) => fmt.n(v, 1)],
      ["Frames concealed", "concealed", (v) => fmt.n(v, 1), true],
      ["AEAD auth failures", "auth_fail", (v) => fmt.int(v)],
    ];
    ui.table.innerHTML = rows.map(([lab, k, f, lowBetter, highBetter]) => {
      const vals = ARMS.map((a) => R[a][k]);
      const best = lowBetter ? Math.min(...vals) : highBetter ? Math.max(...vals) : null;
      return `<tr><td>${lab}</td>${ARMS.map((a, i) => `<td class="num" style="${best !== null && vals[i] === best ? "color:var(--ok);font-weight:700" : ""}">${f(vals[i])}</td>`).join("")}</tr>`;
    }).join("");

    const first = (a) => d.arms[a].call_ids[0];
    ui.audio.innerHTML = `<div class="audio-card"><div class="ac-h"><i class="swatch" style="background:#94a3b8"></i>Original (what alice said)</div><audio controls preload="none" src="/api/lab/${first("swara")}/audio/sent.wav"></audio></div>` +
      ARMS.map((a) => `<div class="audio-card"><div class="ac-h"><i class="swatch" style="background:${col[a]}"></i>Heard with ${SW.armName[a]}<span class="dim small" style="margin-left:auto">${fmt.pct(R[a].perceptual_pct)} damage</span></div><audio controls preload="none" src="/api/lab/${first(a)}/audio/received.wav"></audio></div>`).join("");
    ui.out.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  SW.page("bench", {
    title: "A/B Benchmark",
    group: "Console",
    mount(el) {
      el.innerHTML = `
      <div class="split">
        <div class="stack">
          <div class="card"><div class="card-h"><h3>Benchmark setup</h3></div><div class="card-b" id="b-cfg"></div></div>
          <div class="note small"><b>Fairness.</b> The impairment injector keeps two independent random streams — one for the Gilbert–Elliott loss chain, one for jitter — and is reset to the same seed for every arm, so a scheme that sends more packets cannot perturb the loss pattern it is measured against. Every arm faces the <b>identical</b> sequence of good and bad periods.</div>
        </div>
        <div class="stack">
          <div class="grid g3" id="b-arms"></div>
          <div class="stack hidden" id="b-out">
            <div class="verdict" id="b-verdict"></div>
            <div class="grid g4">
              <div class="card"><div class="card-b"><div class="chart-box sm"><canvas id="b-c1"></canvas></div></div></div>
              <div class="card"><div class="card-b"><div class="chart-box sm"><canvas id="b-c2"></canvas></div></div></div>
              <div class="card"><div class="card-b"><div class="chart-box sm"><canvas id="b-c3"></canvas></div></div></div>
              <div class="card"><div class="card-b"><div class="chart-box sm"><canvas id="b-c4"></canvas></div></div></div>
            </div>
            <div class="grid g2">
              <div class="card"><div class="card-h"><h3>Like-for-like comparison</h3><span class="sub">best value per row in green</span></div>
                <div class="table-wrap"><table class="t"><thead><tr><th>Metric</th>${ARMS.map((a) => `<th class="num" style="color:${SW.armColor()[a]}">${SW.armName[a]}</th>`).join("")}</tr></thead><tbody id="b-table"></tbody></table></div></div>
              <div class="card"><div class="card-h"><h3>Listen and compare</h3><span class="sub">identical speech, identical channel</span></div><div class="card-b stack" style="gap:10px" id="b-audio"></div></div>
            </div>
          </div>
          <div class="card" id="b-intro"><div class="card-b">${SW.empty("Run the three-arm comparison", "The same call is placed three times — SWARA, uniform FEC and unprotected UDP — over the same impaired network. Each arm is a full live call with real sockets and cryptography.", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6"/><rect x="12" y="8" width="3" height="10"/><rect x="17" y="5" width="3" height="13"/></svg>')}</div></div>
        </div>
      </div>`;
      const cfg = el.querySelector("#b-cfg");
      const clipBox = h(`<div class="hidden" style="margin:-4px 0 14px"></div>`);
      clipBox.appendChild(SW.clipWidget());
      ui.src = SW.seg([["synthetic", "Synthetic speech"], ["upload", "My recording"]], "synthetic", (v) => clipBox.classList.toggle("hidden", v !== "upload"));
      cfg.appendChild(SW.field("Audio source", ui.src)); cfg.appendChild(clipBox);
      ui.clipBox = clipBox;
      ui.dur = SW.slider({ label: "Seconds per call", min: 5, max: 40, step: 1, value: 20, fmt: (v) => v + " s" });
      cfg.appendChild(ui.dur.el);
      ui.rep = SW.seg([["1", "1 run"], ["2", "2 runs"], ["3", "3 runs"]], "1");
      cfg.appendChild(SW.field("Repeats per arm (averaged)", ui.rep));
      cfg.appendChild(h(`<div class="divider"></div>`));
      ui.net = SW.netControls({ loss: 0.15, burst: 4, jitter: 12, extra_delay: 0 });
      cfg.appendChild(ui.net.el);
      cfg.appendChild(h(`<div class="divider"></div>`));
      ui.start = h(`<button class="btn primary lg block"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>Run benchmark</button>`);
      ui.stop = h(`<button class="btn danger lg block hidden">Cancel</button>`);
      ui.eta = h(`<div class="small muted center mt-s"></div>`);
      ui.start.onclick = () => { el.querySelector("#b-intro").classList.add("hidden"); start(); };
      ui.stop.onclick = async () => { if (run) await SW.api(`/api/lab/${run.id}/stop`, { method: "POST" }).catch(() => {}); };
      cfg.appendChild(ui.start); cfg.appendChild(ui.stop); cfg.appendChild(ui.eta);

      ui.arms = {};
      ARMS.forEach((a) => { ui.arms[a] = armCard(a); el.querySelector("#b-arms").appendChild(ui.arms[a]); });
      ui.out = el.querySelector("#b-out"); ui.verdict = el.querySelector("#b-verdict"); ui.table = el.querySelector("#b-table"); ui.audio = el.querySelector("#b-audio");
      ui.c1 = el.querySelector("#b-c1"); ui.c2 = el.querySelector("#b-c2"); ui.c3 = el.querySelector("#b-c3"); ui.c4 = el.querySelector("#b-c4");
      ui.intro = el.querySelector("#b-intro");
    },
    async show() {
      if (SW.benchPreset) {
        const p = SW.benchPreset; SW.benchPreset = null;
        ui.net.set(p); ui.dur.set(Math.max(5, Math.min(40, p.seconds)));
        if (p.source === "upload" && SW.clip) { ui.src.set("upload"); ui.clipBox.classList.remove("hidden"); }
        SW.toast("Network settings copied from the Live Call Lab", "info", 2500);
      }
      if (run && !run.done) return;
      try {
        const a = await SW.api("/api/lab/active");
        if (a.busy && a.run && a.run.kind === "bench") { ui.intro.classList.add("hidden"); track(a.run); }
      } catch {}
    },
  });
})();
