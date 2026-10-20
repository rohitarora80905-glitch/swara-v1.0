/* Relay & Users — the production relay hosted by this process, and the user registry */
(function () {
  const { h, esc, fmt } = SW;
  let ui = {}, timer = 0, lastTs = 0, users = [];

  async function refresh() {
    try {
      const r = await SW.api(`/api/relay?since=${lastTs}`);
      renderRelay(r);
      if (r.logs.length) {
        r.logs.forEach((l) => ui.log.appendChild(h(`<div class="ln ${/FAILED|error|cannot/i.test(l.msg) ? "error" : /authenticated|call /.test(l.msg) ? "phase" : ""}"><span class="ts">${fmt.time(l.ts)}</span><span>${esc(l.msg)}</span></div>`)));
        lastTs = r.logs[r.logs.length - 1].ts;
        ui.log.scrollTop = ui.log.scrollHeight;
      }
    } catch {}
  }

  function renderRelay(r) {
    ui.state.innerHTML = `<span class="badge ${r.running ? "ok" : "bad"}">${r.running ? "RUNNING" : "STOPPED"}</span><span class="mono">udp://${r.host}:${r.port}</span>${r.running ? `<span class="muted small">up ${fmt.dur(r.uptime_s)}</span>` : ""}`;
    ui.err.classList.toggle("hidden", !r.error); ui.err.textContent = r.error || "";
    ui.startB.classList.toggle("hidden", r.running); ui.stopB.classList.toggle("hidden", !r.running);
    const sp = r.spark || {}, pk = r.packets || {};
    ui.k.online.set(r.online.length, `${r.users_known} users loaded`);
    ui.k.calls.set(r.calls.length, "being relayed now");
    ui.k.auth.set(`${sp.auth_ok ?? 0}<small> ok</small> · ${sp.auth_fail ?? 0}<small> fail</small>`, `${sp.hello ?? 0} HELLOs · ${sp.cookie_bad ?? 0} bad cookies`);
    ui.k.pk.set(`${fmt.int(pk.rx ?? 0)}<small> in</small> · ${fmt.int(pk.tx ?? 0)}<small> out</small>`, `${fmt.bytes(pk.rx_bytes ?? 0)} in · ${fmt.bytes(pk.tx_bytes ?? 0)} out`);
    ui.online.innerHTML = r.online.length ? r.online.map((o) => `<tr><td><span class="badge ok">●</span> <b>${esc(o.user)}</b></td><td class="mono">${esc(o.addr)}</td><td class="mono">${o.cid}</td></tr>`).join("")
      : `<tr><td colspan="3" class="muted center" style="padding:18px">Nobody is signed in. Start a client with the commands below.</td></tr>`;
    ui.calls.innerHTML = r.calls.length ? r.calls.map((c) => `<tr><td class="mono">${c.call_id}</td><td>${esc(c.a)} ⇄ ${esc(c.b)}</td><td class="mono small">${esc(c.a_addr || "")} ⇄ ${esc(c.b_addr || "")}</td></tr>`).join("")
      : `<tr><td colspan="3" class="muted center" style="padding:18px">No active calls.</td></tr>`;
    ui.port.value = ui.port.value || r.port;
    cli(r);
    const set = new Set(r.online.map((o) => o.user));
    SW.$$("tr[data-u]", ui.users).forEach((tr) => tr.querySelector(".on").innerHTML = set.has(tr.dataset.u) ? '<span class="badge ok">online</span>' : '<span class="dim small">offline</span>');
  }

  function cli(r) {
    const host = location.hostname === "localhost" || location.hostname === "127.0.0.1" ? "127.0.0.1" : location.hostname;
    const a = ui.ua.value || "alice", b = ui.ub.value || "bob";
    const key = `${host}:${r.port}:${a}:${b}`;
    if (ui.cliKey === key) return; ui.cliKey = key;
    ui.cli.innerHTML = "";
    ui.cli.appendChild(h(`<div class="small muted">1 · callee waits (terminal 1, inside the <code>SWARA</code> folder)</div>`));
    ui.cli.appendChild(SW.codeBlock(`python client.py --user ${b} --password <${b}-password> --server ${host}:${r.port} --wait`));
    ui.cli.appendChild(h(`<div class="small muted mt-s">2 · caller dials (terminal 2) — add <code>--audio live</code> for mic &amp; speakers, <code>--loss 0.15 --burst 4</code> to impair</div>`));
    ui.cli.appendChild(SW.codeBlock(`python client.py --user ${a} --password <${a}-password> --server ${host}:${r.port} --call ${b}`));
  }

  async function loadUsers() {
    try {
      const d = await SW.api("/api/users");
      users = d.users;
      ui.users.innerHTML = users.length ? users.map((u) => `<tr data-u="${esc(u.username)}"><td><b>${esc(u.username)}</b></td><td class="on"><span class="dim small">${u.online ? "online" : "offline"}</span></td>
        <td class="mono small" title="${u.salt}">${u.salt.slice(0, 12)}…</td><td class="mono small" title="${u.pub}">${u.pub.slice(0, 16)}…</td><td class="num">${u.record_bytes} B</td>
        <td class="num"><button class="btn sm ghost" data-del="${esc(u.username)}">Delete</button></td></tr>`).join("")
        : `<tr><td colspan="6" class="muted center" style="padding:18px">No users registered yet.</td></tr>`;
      SW.$$("[data-del]", ui.users).forEach((b) => (b.onclick = async () => {
        if (!confirm(`Delete user '${b.dataset.del}'? Their credential record will be removed from users.json.`)) return;
        try { await SW.api(`/api/users/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); SW.toast(`Deleted ${b.dataset.del}`, "ok"); loadUsers(); } catch (e) { SW.toast(e.message, "bad"); }
      }));
      const opts = users.map((u) => `<option>${esc(u.username)}</option>`).join("");
      const va = ui.ua.value, vb = ui.ub.value;
      ui.ua.innerHTML = opts; ui.ub.innerHTML = opts;
      ui.ua.value = va && users.some((u) => u.username === va) ? va : users[0]?.username || "";
      ui.ub.value = vb && users.some((u) => u.username === vb) ? vb : users[1]?.username || users[0]?.username || "";
      ui.cliKey = null;
    } catch (e) { SW.toast(e.message, "bad"); }
  }

  SW.page("relay", {
    title: "Relay & Users",
    group: "Operations",
    mount(el) {
      el.innerHTML = `
      <div class="card glow"><div class="card-h"><h3>Hosted UDP relay</h3><div class="row" id="r-state"></div>
        <div class="right"><input type="number" id="r-port" style="width:100px;height:30px" min="1024" max="65535"><button class="btn sm primary" id="r-start">Start relay</button><button class="btn sm danger hidden" id="r-stop">Stop relay</button></div></div>
        <div class="card-b"><div class="note warn hidden" id="r-err"></div><div class="grid g4" id="r-kpis"></div>
        <div class="small muted mt">This is <code>server.Server</code> from <code>server.py</code> — SPARK authentication, presence, call setup and blind media relay — running inside the uvicorn process. Media is forwarded verbatim by connection id; the relay never holds media keys.</div></div></div>
      <div class="grid g2 mt">
        <div class="card"><div class="card-h"><h3>Signed-in users</h3></div><table class="t"><thead><tr><th>User</th><th>Address</th><th>CID</th></tr></thead><tbody id="r-online"></tbody></table></div>
        <div class="card"><div class="card-h"><h3>Active calls</h3></div><table class="t"><thead><tr><th>Call</th><th>Parties</th><th>Endpoints</th></tr></thead><tbody id="r-calls"></tbody></table></div>
      </div>
      <div class="grid g2 mt">
        <div class="card"><div class="card-h"><h3>Connect real clients</h3><div class="right row"><select id="r-ua" style="height:30px;width:auto"></select><span class="dim">calls</span><select id="r-ub" style="height:30px;width:auto"></select></div></div>
          <div class="card-b stack" style="gap:8px" id="r-cli"></div>
          <div class="card-b" style="padding-top:0"><div class="note small">Clients on another laptop use this machine's LAN IP instead of 127.0.0.1. Allow the relay's UDP port through the firewall; many campus Wi-Fi networks block device-to-device UDP — the Live Call Lab needs no network at all.</div></div></div>
        <div class="card"><div class="card-h"><h3>Relay log</h3><div class="right"><button class="btn sm ghost" id="r-clear">Clear</button></div></div><div class="card-b"><div class="console" id="r-log" style="height:300px"></div></div></div>
      </div>
      <div class="section-title"><h2>User registry</h2><span class="sub">users.json — salt, AEAD envelope and public key; no password or password-equivalent is stored</span></div>
      <div class="grid" style="grid-template-columns:320px minmax(0,1fr)">
        <div class="card"><div class="card-h"><h3>Register a user</h3></div><div class="card-b">
          <form id="r-form"><div class="field"><label>Username</label><input type="text" name="u" required maxlength="40" pattern="[A-Za-z0-9._\\-]+" placeholder="e.g. carol"></div>
          <div class="field"><label>Password</label><input type="password" name="p" required placeholder="never stored"></div>
          <button class="btn primary block" type="submit">Register</button></form>
          <div class="small muted mt">Registration stretches the password with PBKDF2 (200,000 iterations), generates an X25519 static key and seals it in an envelope only the password can open. A running relay picks the new user up immediately.</div></div></div>
        <div class="card"><div class="table-wrap"><table class="t"><thead><tr><th>User</th><th>Status</th><th>Salt</th><th>Static public key</th><th class="num">Record</th><th></th></tr></thead><tbody id="r-users"></tbody></table></div></div>
      </div>`;
      ui.state = el.querySelector("#r-state"); ui.err = el.querySelector("#r-err"); ui.port = el.querySelector("#r-port");
      ui.startB = el.querySelector("#r-start"); ui.stopB = el.querySelector("#r-stop");
      ui.online = el.querySelector("#r-online"); ui.calls = el.querySelector("#r-calls"); ui.log = el.querySelector("#r-log");
      ui.users = el.querySelector("#r-users"); ui.cli = el.querySelector("#r-cli"); ui.ua = el.querySelector("#r-ua"); ui.ub = el.querySelector("#r-ub");
      const kp = el.querySelector("#r-kpis");
      ui.k = { online: SW.kpi("Signed in", "var(--ok)"), calls: SW.kpi("Calls relayed", "var(--accent)"), auth: SW.kpi("SPARK handshakes", "var(--info)"), pk: SW.kpi("Datagrams", "var(--uniform)") };
      Object.values(ui.k).forEach((k) => kp.appendChild(k.el));
      ui.startB.onclick = async () => { try { const r = await SW.api("/api/relay/start", { body: { host: "0.0.0.0", port: parseInt(ui.port.value || "9000", 10) } }); r.running ? SW.toast(`Relay listening on udp:${r.port}`, "ok") : SW.toast(r.error || "Relay failed to start", "bad"); refresh(); } catch (e) { SW.toast(e.message, "bad"); } };
      ui.stopB.onclick = async () => { await SW.api("/api/relay/stop", { method: "POST" }).catch(() => {}); SW.toast("Relay stopped", "warn"); refresh(); };
      el.querySelector("#r-clear").onclick = () => (ui.log.innerHTML = "");
      ui.ua.onchange = ui.ub.onchange = () => { ui.cliKey = null; refresh(); };
      el.querySelector("#r-form").onsubmit = async (e) => {
        e.preventDefault();
        const f = e.target;
        try {
          const d = await SW.api("/api/users", { body: { username: f.u.value.trim(), password: f.p.value } });
          SW.toast(`${d.message} · ${d.record_bytes} B stored`, "ok"); f.reset(); loadUsers();
        } catch (err) { SW.toast(err.message, "bad"); }
      };
      loadUsers();
    },
    show() { refresh(); clearInterval(timer); timer = setInterval(refresh, 2000); },
    hide() { clearInterval(timer); },
  });
})();
