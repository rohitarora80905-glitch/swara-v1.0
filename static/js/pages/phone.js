/* Softphone — sign in to a relay and call a real person, with your microphone */
(function () {
  const { h, esc, fmt } = SW;
  const FATE_COL = ["#2dd4bf", "#60a5fa", "#c084fc", "#fb7185"];
  const FATE_NAME = ["delivered", "rebuilt (FEC)", "anchor replica", "concealed"];
  const STATE_TEXT = { idle: "Not signed in", connecting: "Signing in…", online: "Online — waiting for calls", calling: "Calling…", in_call: "In call" };

  let ui = {}, ws = null, mic = null, player = null, defaults = null, timer = 0, raf = 0, report = null;
  let st = fresh();
  function fresh() { return { state: "idle", user: null, cid: null, peer: null, online: [], callStart: 0, stats: null, muted: false, audio: "browser", ended: false }; }

  const send = (o) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };
  const log = (msg, level = "info", t) => {
    ui.log.appendChild(h(`<div class="ln ${level === "ok" ? "phase" : level}"><span class="ts">${t !== undefined ? "+" + t.toFixed(1) + "s" : new Date().toLocaleTimeString()}</span><span>${esc(msg)}</span></div>`));
    ui.log.scrollTop = ui.log.scrollHeight;
  };

  /* ---------- connection ---------- */
  async function signIn() {
    const user = ui.user.value.trim(), password = ui.pw.value, server = ui.relay.value.trim();
    if (!user || !password) { SW.toast("Enter a username and password", "warn"); return; }
    if (!server) { SW.toast("Enter the relay address (host:port)", "warn"); return; }
    st = fresh(); st.user = user; st.audio = ui.audio.get(); report = null;
    ui.log.innerHTML = ""; render();
    // Audio must be unlocked by a click: do it now, before any call arrives.
    try {
      player = player || new SW.PcmPlayer();
      await player.enable();
      if (st.audio === "browser") {
        mic = new SW.MicStream();
        await mic.start((pcm) => { if (ws && ws.readyState === 1 && ws.bufferedAmount < 65536) ws.send(pcm.buffer); });
        log("microphone ready (" + Math.round(mic.ctx.sampleRate / 1000) + " kHz device → 16 kHz)");
      }
    } catch (e) {
      SW.toast("Microphone not available: " + e.message + (location.protocol === "http:" && !["localhost", "127.0.0.1"].includes(location.hostname) ? " — browsers only allow the microphone on https:// or localhost. Use the HTTPS share link." : ""), "bad", 9000);
      if (mic) { mic.stop(); mic = null; }
      st = fresh(); render(); return;
    }
    st.state = "connecting"; render();
    const net = ui.net.get();
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/phone";
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => send({ op: "login", user, password, server, audio: st.audio, protection: ui.prot.get(), mode: ui.mode.get(), ...net });
    ws.onmessage = (e) => { if (e.data instanceof ArrayBuffer) onAudio(e.data); else onMsg(JSON.parse(e.data)); };
    ws.onerror = () => log("connection error", "error");
    ws.onclose = () => {
      const wasIn = st.state !== "idle";
      ws = null; stopCallUi();
      if (mic) { mic.stop(); mic = null; }
      if (wasIn && !st.ended) { log("disconnected from the server", "warn"); SW.toast("Disconnected from the server", "warn"); }
      st = fresh(); render();
    };
  }
  function signOut() { st.ended = true; send({ op: "logout" }); setTimeout(() => ws && ws.close(), 800); }

  function onAudio(buf) {
    if (!player) return;
    player.push(new Int16Array(buf));
  }

  function onMsg(m) {
    switch (m.op) {
      case "log": log(m.msg, m.level, m.t); break;
      case "logged_in": st.cid = m.cid; ui.cid.textContent = m.cid; break;
      case "state":
        st.state = m.state; if (m.peer) st.peer = m.peer;
        if (m.state === "idle") { st.ended = true; if (ws) ws.close(); }
        render(); break;
      case "online": st.online = m.users.filter((u) => u !== st.user); renderPresence(); break;
      case "incoming": SW.toast(`Incoming call from ${m.peer}`, "info", 3000); break;
      case "call_started":
        st.peer = m.peer; st.state = "in_call"; st.callStart = Date.now(); st.stats = null; report = null;
        if (mic) mic.sending = true;
        resetLive(); render();
        stopCallUi(); ui.timer.textContent = "00:00"; timer = setInterval(() => (ui.timer.textContent = clock()), 500);
        SW.toast(`${m.role === "caller" ? "Connected to" : "Answered"} ${m.peer}`, "ok", 2500); break;
      case "stats": st.stats = m; updateLive(m); break;
      case "call_ended":
        if (mic) mic.sending = false;
        report = m.report || {}; report.reason = m.reason; st.peer = null;
        stopCallUi(); render(); renderReport(); break;
      case "error":
        SW.toast(m.msg, "bad", 6000); log(m.msg, "error");
        if (m.fatal || st.state === "connecting") { st.ended = true; if (ws) ws.close(); }
        else if (st.state === "calling") { st.state = "online"; render(); }
        break;
    }
  }

  /* ---------- rendering ---------- */
  function render() {
    const s = st.state, signed = s !== "idle" && s !== "connecting";
    ui.conn.className = "badge " + (s === "in_call" ? "live" : signed ? "ok" : s === "connecting" ? "warn" : "");
    ui.conn.textContent = s === "in_call" ? "LIVE" : signed ? "SIGNED IN" : s === "connecting" ? "SIGNING IN" : "OFFLINE";
    ui.signin.classList.toggle("hidden", s !== "idle");
    ui.signout.classList.toggle("hidden", s === "idle");
    ui.signin.disabled = s === "connecting";
    [ui.user, ui.pw, ui.relay].forEach((i) => (i.disabled = s !== "idle"));
    ui.audio.disable(s !== "idle"); ui.prot.disable(s !== "idle"); ui.mode.disable(s !== "idle");
    ui.who.textContent = st.user ? st.user : "—";
    ui.status.textContent = s === "in_call" ? `In call with ${st.peer}` : s === "calling" ? `Calling ${st.peer}…` : STATE_TEXT[s];
    ui.peerTag.textContent = s === "in_call" ? `${ui.prot.get()} · ${ui.mode.get()} · ${st.audio === "device" ? "server audio device" : "browser mic"}` : "";
    ui.hang.classList.toggle("hidden", !(s === "in_call" || s === "calling"));
    ui.mute.classList.toggle("hidden", s !== "in_call");
    ui.intro.classList.toggle("hidden", s !== "idle");
    ui.presence.classList.toggle("hidden", !(s === "online" || s === "calling"));
    ui.live.classList.toggle("hidden", s !== "in_call");
    ui.reportCard.classList.toggle("hidden", !(report && s !== "in_call"));
    ui.meters.classList.toggle("hidden", !signed);
    ui.timer.classList.toggle("hidden", s !== "in_call");
    if (s !== "in_call") { stopCallUi(); ui.timer.textContent = ""; }
    if (signed && !raf) meterLoop();
    renderPresence();
  }
  const clock = () => { const t = Math.floor((Date.now() - st.callStart) / 1000); return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; };

  function renderPresence() {
    if (st.state !== "online" && st.state !== "calling") return;
    const list = st.online;
    ui.plist.innerHTML = list.length
      ? list.map((u) => `<tr><td><span class="badge ok">●</span> <b>${esc(u)}</b></td><td class="right-align"><button class="btn sm primary" data-call="${esc(u)}" ${st.state === "calling" ? "disabled" : ""}>Call</button></td></tr>`).join("")
      : `<tr><td colspan="2" class="muted center" style="padding:16px">Nobody else is signed in to this relay yet. Ask your friend to sign in — the list refreshes every few seconds.</td></tr>`;
    SW.$$("[data-call]", ui.plist).forEach((b) => (b.onclick = () => call(b.dataset.call)));
  }

  function call(peer) { if (!peer) return; st.peer = peer; st.state = "calling"; render(); send({ op: "call", peer }); }
  function hangup() { send({ op: "hangup" }); }
  function toggleMute() {
    st.muted = !st.muted;
    if (mic) mic.muted = st.muted;
    send({ op: "mute", on: st.muted });
    ui.mute.classList.toggle("on", st.muted);
    ui.mute.querySelector("span").textContent = st.muted ? "Unmute" : "Mute";
  }
  function stopCallUi() { clearInterval(timer); timer = 0; }

  function meterLoop() {
    raf = requestAnimationFrame(() => {
      const m = mic ? Math.min(1, mic.level * 6) : 0, p = player ? Math.min(1, player.level * 6) : 0;
      ui.micMeter.style.width = m * 100 + "%"; ui.spkMeter.style.width = p * 100 + "%";
      if (player) player.level *= 0.85;
      raf = st.state === "idle" ? 0 : requestAnimationFrame(meterLoop);
    });
  }

  /* ---------- live panel ---------- */
  function resetLive() {
    Object.values(ui.k).forEach((k) => k.set("—", ""));
    ui.gauge.set(NaN); ui.peerMos.textContent = "—";
    ui.chart.data.datasets.forEach((d) => (d.data = [])); ui.chart.update("none");
    ui.fates.innerHTML = FATE_NAME.map((n, i) => `<span><i class="swatch" style="background:${FATE_COL[i]}"></i>${n} <b>0</b></span>`).join("");
  }
  function updateLive(s) {
    const k = ui.k;
    k.sent.set(fmt.int(s.tx_frames), `${fmt.int(s.tx_parity)} parity · ${fmt.int(s.tx_replicas)} replicas`);
    k.rate.set(`${fmt.n(s.kbps, 0)}<small>kbit/s</small>`, s.codec ? `NAVIK: ${s.codec} · ρ ${(s.rho * 100).toFixed(0)}%` : "warming up");
    k.rx.set(fmt.int(s.rx_frames), `${fmt.int(s.played)} played`);
    k.fec.set(fmt.int(s.fates[1]), `${fmt.int(s.fates[2])} by anchor replica`);
    k.conc.set(fmt.int(s.concealed), `of ${fmt.int(s.played)} frames`);
    k.after.set(fmt.pct(s.loss_after, 1), `channel dropped ${fmt.int(s.drops)} of yours`);
    k.net.set(`${fmt.n(s.net_loss, 1)}<small>%</small>`, `burst ${fmt.n(s.burst, 1)} · jitter ${fmt.n(s.jitter, 1)} ms`);
    k.jb.set(`${fmt.n(s.depth, 0)}<small>ms</small>`, s.auth_fail ? `${s.auth_fail} auth failures!` : "0 auth failures");
    ui.gauge.set(s.mos > 0 ? s.mos : NaN, "MOS · what you hear");
    ui.peerMos.textContent = s.peer_mos > 0 ? fmt.n(s.peer_mos, 2) : "—";
    ui.peerMos.style.color = s.peer_mos > 0 ? SW.mosColor(s.peer_mos) : "";
    ui.fates.innerHTML = FATE_NAME.map((n, i) => `<span><i class="swatch" style="background:${FATE_COL[i]}"></i>${n} <b>${fmt.int(s.fates[i])}</b></span>`).join("");
    const ds = ui.chart.data.datasets;
    ds[0].data.push({ x: s.t, y: s.loss_after }); ds[1].data.push({ x: s.t, y: s.mos });
    if (ds[0].data.length > 240) { ds[0].data.shift(); ds[1].data.shift(); }
    ui.chart.update("none");
    if (s.mic && s.mic.underruns > 20 && !ui._warned) { ui._warned = true; log(`microphone stream is stuttering (${s.mic.underruns} gaps) — close other tabs or move closer to the Wi-Fi`, "warn"); }
  }

  function renderReport() {
    const r = report; if (!r) return;
    const mos = r.mos || 0, col = SW.mosColor(mos);
    ui.reportCard.innerHTML = `
      <div class="card-h"><h3>Call report</h3><span class="sub">with ${esc(r.peer || "?")} · ${fmt.dur(r.duration)} · ${esc(r.reason || "")}</span>
        <div class="right">${r.peer ? `<button class="btn sm primary" id="ph-again">Call ${esc(r.peer)} again</button>` : ""}</div></div>
      <div class="card-b"><div class="grid g4">
        <div class="kpi" style="--kc:${col}"><div class="k-label">Est. MOS (what you heard)</div><div class="k-value" style="color:${col}">${mos > 0 ? fmt.n(mos, 2) : "—"}</div><div class="k-sub">${mos > 0 ? SW.mosLabel(mos) : ""}</div></div>
        <div class="kpi" style="--kc:var(--conceal)"><div class="k-label">Unrecoverable</div><div class="k-value">${fmt.pct(r.loss_after)}</div><div class="k-sub">${fmt.int(r.concealed)} of ${fmt.int(r.played)} frames concealed</div></div>
        <div class="kpi" style="--kc:var(--fec)"><div class="k-label">Repaired</div><div class="k-value">${fmt.int((r.fates || [0, 0, 0])[1] + (r.fates || [0, 0, 0])[2])}</div><div class="k-sub">${fmt.int((r.fates || [0, 0])[1])} FEC · ${fmt.int((r.fates || [0, 0, 0])[2])} replica</div></div>
        <div class="kpi" style="--kc:var(--uniform)"><div class="k-label">You sent</div><div class="k-value">${fmt.n(r.avg_kbps, 0)}<small>kbit/s</small></div><div class="k-sub">${fmt.int(r.tx_frames)} frames · ${fmt.int(r.tx_parity)} parity · ${fmt.int(r.tx_replicas)} replicas</div></div>
      </div></div>`;
    const b = ui.reportCard.querySelector("#ph-again");
    if (b) b.onclick = () => call(r.peer);
  }

  /* ---------- page ---------- */
  SW.page("phone", {
    title: "Softphone",
    group: "Console",
    mount(el) {
      el.innerHTML = `
      <div class="split">
        <div class="stack">
          <div class="card">
            <div class="card-h"><h3>Sign in</h3><span class="badge" id="ph-conn">OFFLINE</span></div>
            <div class="card-b" id="ph-cfg"></div>
          </div>
          <div class="note small"><b>How it works.</b> Your browser streams your microphone to a SWARA client that runs inside this server. That client does the real thing — SPARK sign-in, perceptual FEC, ChaCha20-Poly1305, UDP to the relay — and streams back what it decodes. Your friend can do the same from their own console, or from a phone using this server's <b>https://</b> share link.</div>
        </div>
        <div class="stack">
          <div class="card">
            <div class="card-h"><h3 id="ph-status">Not signed in</h3><span class="sub" id="ph-peertag"></span>
              <div class="right"><span class="badge mono" id="ph-timer"></span><button class="btn sm hidden" id="ph-mute"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v5"/></svg><span>Mute</span></button><button class="btn sm danger hidden" id="ph-hang"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>Hang up</button></div></div>
            <div class="card-b">
              <div class="row wrap" style="gap:18px"><div class="row"><span class="muted small">You</span><b class="mono" id="ph-who">—</b></div><div class="row"><span class="muted small">Session</span><span class="mono small" id="ph-cid">—</span></div></div>
              <div class="grid g2 mt hidden" id="ph-meters" style="gap:10px">
                <div><div class="tiny muted">Your microphone</div><div class="meter mt-s"><i id="ph-mic"></i></div></div>
                <div><div class="tiny muted">What you hear</div><div class="meter mt-s"><i id="ph-spk"></i></div></div>
              </div>
            </div>
          </div>
          <div class="card" id="ph-intro"><div class="card-b">${SW.empty("Call a real person", "Sign in to a relay on the left. Everyone signed in to the same relay sees each other here and can call with one click — on this computer, on your friend's computer running the same console, or from a phone via the HTTPS share link.", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/></svg>')}</div></div>
          <div class="card hidden" id="ph-presence">
            <div class="card-h"><h3>Who's online</h3><span class="sub">on this relay · click Call, or wait — incoming calls are answered automatically</span>
              <div class="right row"><input type="text" id="ph-dial" placeholder="or type a name" style="height:30px;width:150px"><button class="btn sm" id="ph-dialbtn">Call</button></div></div>
            <table class="t"><tbody id="ph-list"></tbody></table>
          </div>
          <div class="stack hidden" id="ph-live">
            <div class="grid" style="grid-template-columns:230px minmax(0,1fr)">
              <div class="card"><div class="card-b" style="display:grid;justify-items:center;gap:8px">
                <div id="ph-gauge"></div>
                <div class="kv" style="width:100%"><dt>They hear (reported)</dt><dd id="ph-peermos">—</dd></div>
              </div></div>
              <div class="grid g4" id="ph-kpis"></div>
            </div>
            <div class="grid" style="grid-template-columns:minmax(0,1.4fr) minmax(0,1fr)">
              <div class="card"><div class="card-h"><h3>Quality over the call</h3></div><div class="card-b"><div class="chart-box sm"><canvas id="ph-chart"></canvas></div></div></div>
              <div class="card"><div class="card-h"><h3>Frames you received</h3></div><div class="card-b"><div class="fate-legend" id="ph-fates" style="flex-direction:column;gap:8px"></div></div></div>
            </div>
          </div>
          <div class="card glow hidden" id="ph-report"></div>
          <div class="card"><div class="card-h"><h3>Log</h3></div><div class="card-b"><div class="console" id="ph-log" style="height:170px"></div></div></div>
        </div>
      </div>`;

      ui = {
        conn: el.querySelector("#ph-conn"), status: el.querySelector("#ph-status"), peerTag: el.querySelector("#ph-peertag"),
        timer: el.querySelector("#ph-timer"), mute: el.querySelector("#ph-mute"), hang: el.querySelector("#ph-hang"),
        who: el.querySelector("#ph-who"), cid: el.querySelector("#ph-cid"), meters: el.querySelector("#ph-meters"),
        micMeter: el.querySelector("#ph-mic"), spkMeter: el.querySelector("#ph-spk"), intro: el.querySelector("#ph-intro"),
        presence: el.querySelector("#ph-presence"), plist: el.querySelector("#ph-list"), live: el.querySelector("#ph-live"),
        reportCard: el.querySelector("#ph-report"), log: el.querySelector("#ph-log"), fates: el.querySelector("#ph-fates"),
        peerMos: el.querySelector("#ph-peermos"),
      };
      ui.hang.onclick = hangup; ui.mute.onclick = toggleMute;
      el.querySelector("#ph-dialbtn").onclick = () => call(el.querySelector("#ph-dial").value.trim());
      el.querySelector("#ph-dial").onkeydown = (e) => { if (e.key === "Enter") call(e.target.value.trim()); };

      // --- sign-in form
      const cfg = el.querySelector("#ph-cfg");
      const relayRow = h(`<div class="field"><label>Relay address <span class="val" style="font-weight:500">host:port</span></label><input type="text" id="ph-relay" placeholder="127.0.0.1:9000"><div class="row wrap mt-s" style="gap:6px"><button type="button" class="chip" style="height:26px;font-size:11.5px" id="ph-thisrelay">This computer's relay</button></div><div class="hint">On your friend's computer, type <b>your</b> IP address here, e.g. 192.168.1.20:9000.</div></div>`);
      cfg.appendChild(relayRow); ui.relay = relayRow.querySelector("input");
      const ur = h(`<div class="grid g2" style="gap:12px"><div class="field"><label>Username</label><input type="text" id="ph-user" maxlength="40" autocomplete="username"></div><div class="field"><label>Password</label><input type="password" id="ph-pw" autocomplete="current-password"></div></div>`);
      cfg.appendChild(ur); ui.user = ur.querySelector("#ph-user"); ui.pw = ur.querySelector("#ph-pw");
      cfg.appendChild(h(`<div class="hint" style="margin:-8px 0 14px">Demo users: <code>alice</code> / <code>secret1</code> and <code>bob</code> / <code>secret2</code>. Register more on the relay's <a href="#/relay">Relay &amp; Users</a> page.</div>`));
      ui.audio = SW.seg([["browser", "Browser mic"], ["device", "This computer's device"]], "browser");
      cfg.appendChild(SW.field("Audio", ui.audio));
      ui.prot = SW.seg([["swara", "SWARA"], ["uniform", "Uniform FEC"], ["none", "None"]], "swara");
      cfg.appendChild(SW.field("Loss protection", ui.prot));
      ui.mode = SW.seg([["wideband", "Wideband 16 kHz"], ["narrowband", "Narrowband 8 kHz"]], "wideband");
      cfg.appendChild(SW.field("Codec mode", ui.mode));
      cfg.appendChild(h(`<div class="divider"></div>`));
      const pushImp = SW.debounce((v) => { if (st.state !== "idle") send({ op: "impairment", ...v }); }, 220);
      ui.net = SW.netControls({ loss: 0, burst: 1, jitter: 0, extra_delay: 0 }, pushImp);
      cfg.appendChild(ui.net.el);
      cfg.appendChild(h(`<div class="hint small dim" style="margin:-6px 0 12px">Applied to the packets <b>you</b> send; adjustable during the call to show the protection working.</div>`));
      ui.signin = h(`<button class="btn primary lg block"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>Sign in</button>`);
      ui.signout = h(`<button class="btn lg block hidden">Sign out</button>`);
      ui.signin.onclick = signIn; ui.signout.onclick = signOut;
      cfg.appendChild(ui.signin); cfg.appendChild(ui.signout);
      ui.pw.onkeydown = (e) => { if (e.key === "Enter" && st.state === "idle") signIn(); };

      // --- live panel widgets
      ui.gauge = SW.gauge(190); el.querySelector("#ph-gauge").appendChild(ui.gauge.el);
      const kp = el.querySelector("#ph-kpis");
      ui.k = {
        sent: SW.kpi("Frames sent", "var(--accent)"), rate: SW.kpi("Wire rate", "var(--accent-2)"),
        rx: SW.kpi("Frames received", "var(--swara)"), fec: SW.kpi("Rebuilt by FEC", "var(--fec)"),
        conc: SW.kpi("Concealed", "var(--conceal)"), after: SW.kpi("Loss after repair", "var(--bad)"),
        net: SW.kpi("Measured loss", "var(--uniform)"), jb: SW.kpi("Jitter buffer", "var(--info)"),
      };
      Object.values(ui.k).forEach((k) => kp.appendChild(k.el));
      ui.chart = SW.chart(el.querySelector("#ph-chart"), {
        type: "line",
        data: { datasets: [{ label: "loss after repair %", borderColor: "#fb7185", backgroundColor: SW.alpha("#fb7185", 0.12), data: [], yAxisID: "y2", tension: 0.25, fill: true }, { label: "MOS", borderColor: "#2dd4bf", data: [], yAxisID: "y", tension: 0.25 }] },
        options: { animation: false, parsing: false, interaction: { mode: "nearest", intersect: false, axis: "x" },
          scales: { x: SW.axis("seconds", { type: "linear", min: 0, ticks: { maxTicksLimit: 8 } }), y: SW.axis("MOS", { min: 1, max: 4.5 }), y2: SW.axis("%", { position: "right", beginAtZero: true, grid: { display: false } }) },
          plugins: { legend: { position: "top", align: "end" } } },
      });
      resetLive();

      SW.api("/api/phone/defaults").then((d) => {
        defaults = d;
        ui.relay.value = d.relay;
        ui.audio.enable("device", !!d.device_audio);
        el.querySelector("#ph-thisrelay").onclick = () => (ui.relay.value = d.relay);
        if (!d.relay_running) log("this server's relay is not running — start it on the Relay & Users page, or enter another relay's address", "warn");
        if (location.protocol === "http:" && !["localhost", "127.0.0.1"].includes(location.hostname))
          log(`you opened the console over plain http from another device: the browser will refuse the microphone. Use https://${d.lan_ip}:${d.https_port || 8443}/ instead${d.https_port ? "" : " (start the server from the launcher with HTTPS on)"}`, "warn");
      }).catch(() => {});
      render();
    },
    hide() { /* keep the call alive while browsing other pages */ },
  });
})();
