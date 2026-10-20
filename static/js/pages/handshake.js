/* SPARK Handshake — run the real key exchange in memory and watch each message */
(function () {
  const { h, esc, fmt } = SW;
  let ui = {};

  async function run(wrong) {
    const body = { username: ui.user.value.trim() || "alice", password: ui.pw.value || "x", attempt: wrong ? (ui.pw.value || "x") + "-typo" : ui.attempt.value || ui.pw.value };
    ui.go.disabled = ui.bad.disabled = true;
    try { render(await SW.api("/api/tools/handshake", { body })); }
    catch (e) { SW.toast(e.message, "bad"); }
    finally { ui.go.disabled = ui.bad.disabled = false; }
  }

  function render(d) {
    const box = ui.seq;
    box.innerHTML = `<div class="seq"><div class="lane-h">Client · ${esc(d.username)}</div><div></div><div class="lane-h">Server</div></div>`;
    d.steps.forEach((s, i) => {
      const right = s.from === "client";
      const c = right ? "#2dd4bf" : "#60a5fa";
      const m = h(`<div class="seq"><div class="msg ${right ? "r" : "l"}" style="--c:${c};animation-delay:${i * 0.18}s">
        <div class="tiny dim center">${right ? `${fmt.n(s.ms, 2)} ms` : ""}</div>
        <div class="arrow"><div class="lbl"><b>${s.name}</b><span class="mono tiny muted">${s.bytes} B on the wire</span></div><div class="line"></div>
          <div class="desc">${esc(s.desc)}<br><span class="mono dim" style="font-size:10.5px">${esc(s.preview)}${s.payload_bytes > 24 ? " …" : ""}</span></div></div>
        <div class="tiny dim center">${right ? "" : `${fmt.n(s.ms, 2)} ms`}</div></div></div>`);
      box.appendChild(m);
    });
    if (d.outcome !== "authenticated") {
      box.appendChild(h(`<div class="seq"><div class="msg" style="animation-delay:${d.steps.length * 0.18}s"><div></div><div class="note warn" style="margin:8px 18px"><b>✕ ${d.outcome === "wrong_password" ? "Wrong password — stopped on the client" : "Rejected"}</b><br>${esc(d.detail)}</div><div></div></div></div>`));
    } else {
      box.appendChild(h(`<div class="seq"><div class="msg" style="animation-delay:${d.steps.length * 0.18}s"><div></div><div class="note ok" style="margin:8px 18px"><b>✓ Mutually authenticated in 2 round trips</b><br>${esc(d.detail)}</div><div></div></div></div>`));
    }
    const tile = (l, v, s, c) => `<div class="kpi" style="--kc:${c}"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${s}</div></div>`;
    ui.kpis.innerHTML = tile("Outcome", d.outcome === "authenticated" ? '<span style="color:var(--ok);font-size:20px">Authenticated</span>' : '<span style="color:var(--bad);font-size:20px">Refused</span>', d.rtts ? `${d.rtts} round trips` : "no key confirmation sent", d.outcome === "authenticated" ? "var(--ok)" : "var(--bad)") +
      tile("Bytes exchanged", `${d.total_bytes}<small>B</small>`, `${d.steps.length} messages incl. 16 B headers`, "var(--accent)") +
      tile("Crypto time", `${fmt.n(d.total_ms, 1)}<small>ms</small>`, "dominated by PBKDF2 on the client", "var(--info)") +
      tile("Amplification", d.amplification ? `${d.amplification.toFixed(2)}<small>×</small>` : "—", "RETRY bytes ÷ HELLO bytes", "var(--uniform)");
    if (d.keys) {
      const k = d.keys;
      ui.keys.innerHTML = `<div class="card-h"><h3>Derived keys</h3><span class="sub">SHA-256 fingerprints — raw keys never leave memory</span><div class="right"><span class="badge ${k.match ? "ok" : "bad"}">${k.match ? "CLIENT = SERVER" : "MISMATCH"}</span></div></div>
        <div class="card-b"><table class="t"><thead><tr><th>Key</th><th>Client</th><th>Server</th></tr></thead><tbody>
          <tr><td>Connection id</td><td class="mono" colspan="2">${k.cid}</td></tr>
          <tr><td>k<sub>c2s</sub> (client → server)</td><td class="mono">${k.client_c2s_fp}</td><td class="mono">${k.server_c2s_fp}</td></tr>
          <tr><td>k<sub>s2c</sub> (server → client)</td><td class="mono">${k.client_s2c_fp}</td><td class="mono">${k.server_s2c_fp}</td></tr>
          <tr><td>k<sub>resume</sub></td><td class="mono" colspan="2">${k.resume_fp}</td></tr>
          <tr><td>Transcript hash</td><td class="mono" colspan="2">${k.transcript_fp}</td></tr></tbody></table>
          <div class="small muted mt">MS = HKDF-Extract(H(transcript), dh1 ‖ dh2 ‖ dh3) where dh1 = X25519(c<sub>eph</sub>, s<sub>static</sub>) authenticates the server, dh2 = X25519(c<sub>static</sub>, s<sub>eph</sub>) authenticates the client, dh3 = X25519(c<sub>eph</sub>, s<sub>eph</sub>) gives forward secrecy.</div></div>`;
      const m = d.migration;
      ui.mig.innerHTML = `<div class="card-h"><h3>Path migration</h3><span class="sub">Wi-Fi → mobile handover mid-call</span><div class="right"><span class="badge ${m.ok ? "ok" : "bad"}">${m.ok ? "REBOUND" : "FAILED"}</span></div></div>
        <div class="card-b"><dl class="kv"><dt>New address</dt><dd>${m.new_addr}</dd><dt>Round trips of silence</dt><dd>${m.rtts}</dd><dt>PATH_MIGRATE size</dt><dd>${m.bytes} B</dd><dt>Server work</dt><dd>${fmt.n(m.ms * 1000, 0)} µs</dd></dl>
        <div class="small muted mt">The client presents its encrypted ticket plus an AEAD proof under k<sub>resume</sub> from the new address. The server keeps no per-session table; an off-path attacker replaying the ticket alone cannot hijack the call.</div></div>`;
    } else { ui.keys.innerHTML = ""; ui.mig.innerHTML = ""; }
    ui.keys.classList.toggle("hidden", !d.keys); ui.mig.classList.toggle("hidden", !d.keys);
    ui.store.innerHTML = `<dl class="kv"><dt>Stored per user</dt><dd>${d.record_bytes} B</dd><dt>Registration (PBKDF2 ×200k)</dt><dd>${fmt.n(d.register_ms, 0)} ms</dd></dl>
      <div class="small muted mt-s">The server keeps {salt, AEAD envelope, client static public key}. Stealing it yields a target for an offline dictionary attack and nothing that can be replayed.</div>`;
  }

  SW.page("handshake", {
    title: "SPARK Handshake",
    group: "Protocol Toolkit",
    mount(el) {
      el.innerHTML = `
      <div class="split">
        <div class="stack">
          <div class="card"><div class="card-h"><h3>Credentials</h3></div><div class="card-b">
            <div class="field"><label>Username</label><input type="text" id="h-user" value="alice" maxlength="40"></div>
            <div class="field"><label>Registered password</label><input type="text" id="h-pw" value="hostel-wifi-1"></div>
            <div class="field"><label>Password the client types</label><input type="text" id="h-att" placeholder="same as registered"><div class="hint">Leave empty to use the correct one.</div></div>
            <button class="btn primary block" id="h-go">Run handshake</button>
            <button class="btn block mt-s" id="h-bad">Try a wrong password</button>
          </div></div>
          <div class="card"><div class="card-h"><h3>What the server stores</h3></div><div class="card-b" id="h-store"><div class="small muted">Run a handshake.</div></div></div>
          <div class="note small"><b>SPARK</b> — Stateless Password-Authenticated Retry &amp; Key-agreement. Every message is run through the real <code>SparkClient</code> / <code>SparkServer</code> from <code>swara/auth.py</code> with X25519, HKDF-SHA256, PBKDF2 and ChaCha20-Poly1305.</div>
        </div>
        <div class="stack">
          <div class="grid g4" id="h-kpis"></div>
          <div class="card"><div class="card-h"><h3>Message sequence</h3><span class="sub">no server state is allocated until the cookie comes back</span></div><div class="card-b"><div class="seq-wrap" id="h-seq"></div></div></div>
          <div class="grid g2"><div class="card hidden" id="h-keys"></div><div class="card hidden" id="h-mig"></div></div>
        </div>
      </div>`;
      ui.user = el.querySelector("#h-user"); ui.pw = el.querySelector("#h-pw"); ui.attempt = el.querySelector("#h-att");
      ui.go = el.querySelector("#h-go"); ui.bad = el.querySelector("#h-bad");
      ui.seq = el.querySelector("#h-seq"); ui.kpis = el.querySelector("#h-kpis"); ui.keys = el.querySelector("#h-keys"); ui.mig = el.querySelector("#h-mig"); ui.store = el.querySelector("#h-store");
      ui.go.onclick = () => run(false); ui.bad.onclick = () => run(true);
      run(false);
    },
  });
})();
