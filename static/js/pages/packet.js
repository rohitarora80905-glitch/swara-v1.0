/* Packet Anatomy — the 16-byte SWARA header, packed by swara/packet.py */
(function () {
  const { h, esc, fmt } = SW;
  const FIELDS = [
    { k: "version", n: "Ver", bits: 4, bytes: [0], c: "#94a3b8", d: "Protocol version (1)." },
    { k: "ptype", n: "Type", bits: 4, bytes: [0], c: "#2dd4bf", d: "Datagram type: HELLO … MEDIA, FEC, CONTROL." },
    { k: "flags", n: "Flags", bits: 8, bytes: [1], c: "#fbbf24", d: "Telemetry piggyback, marker, EF request, encrypted, last-in-group, resumed." },
    { k: "seq", n: "Sequence number", bits: 16, bytes: [2, 3], c: "#60a5fa", d: "16-bit wire sequence; the receiver unwraps it to a 64-bit counter used as the AEAD nonce." },
    { k: "cid", n: "Connection ID", bits: 32, bytes: [4, 5, 6, 7], c: "#c084fc", d: "Derived from the handshake. Survives NAT rebinding and path migration; the relay routes on it." },
    { k: "ts_ms", n: "Timestamp (ms)", bits: 32, bytes: [8, 9, 10, 11], c: "#f472b6", d: "Sender clock; feeds RFC 3550 jitter and the one-way-delay trend NAVIK uses to spot congestion." },
    { k: "pi", n: "PI score", bits: 8, bytes: [12], c: "#34d399", d: "Perceptual importance 0–255. Authenticated but NOT encrypted, so relays and routers can act on it without reading speech." },
    { k: "fec_group", n: "FEC group", bits: 8, bytes: [13], c: "#f59e0b", d: "Which Reed–Solomon group this packet belongs to." },
    { k: "fec_index", n: "FEC index", bits: 8, bytes: [14], c: "#fb923c", d: "Position within the group; high bit set = parity shard." },
    { k: "reserved", n: "Reserved", bits: 8, bytes: [15], c: "#475569", d: "Must be zero." },
  ];
  let ui = {}, D = null, hl = null;

  function vals() {
    let flags = 0; SW.$$("input[data-flag]", ui.root).forEach((c) => c.checked && (flags |= +c.dataset.flag));
    return { ptype: +ui.type.value, flags, seq: ui.seq.get(), cid: parseInt(ui.cid.value || "0", 16) >>> 0, ts_ms: Math.max(0, parseInt(ui.ts.value || "0", 10)), pi: ui.pi.get(), fec_group: ui.grp.get(), fec_index: ui.idx.get() };
  }
  async function pack() {
    try { D = await SW.api("/api/tools/header", { body: vals() }); draw(); } catch (e) { SW.toast(e.message, "bad"); }
  }
  const packSoon = SW.debounce(pack, 120);

  function fval(f) {
    const r = D.roundtrip;
    const v = r[f.k] ?? 0;
    if (f.k === "ptype") return `${v} ${D.type_name}`;
    if (f.k === "cid") return "0x" + v.toString(16).padStart(8, "0");
    if (f.k === "flags") return "0x" + v.toString(16).padStart(2, "0");
    return String(v);
  }

  function draw() {
    if (!D) return;
    let g = `<div class="woff"></div>` + Array.from({ length: 32 }, (_, i) => `<div class="bitn">${i % 8 === 0 ? i : i % 4 === 0 ? "·" : ""}</div>`).join("");
    const rows = [["version", "ptype", "flags", "seq"], ["cid"], ["ts_ms"], ["pi", "fec_group", "fec_index", "reserved"]];
    rows.forEach((r, ri) => {
      g += `<div class="woff">${ri * 4}</div>`;
      r.forEach((k) => {
        const f = FIELDS.find((x) => x.k === k);
        g += `<div class="fld ${hl === k ? "hl" : ""}" data-k="${k}" style="grid-column:span ${f.bits};background:${SW.alpha(f.c, 0.14)};border-color:${SW.alpha(f.c, 0.55)};color:${f.c}">${f.n}<small>${esc(fval(f))}</small></div>`;
      });
    });
    ui.grid.innerHTML = g;
    ui.hex.innerHTML = D.bytes.map((b, i) => {
      const f = FIELDS.find((x) => x.bytes.includes(i) && x.k !== "version") || FIELDS[0];
      return `<span data-k="${f.k}" class="${hl === f.k || (hl === "version" && i === 0) ? "hl" : ""}" style="background:${SW.alpha(f.c, 0.14)};border-color:${SW.alpha(f.c, 0.5)};color:${f.c}" title="byte ${i} · ${f.n}">${b.toString(16).padStart(2, "0")}</span>`;
    }).join("");
    const f = FIELDS.find((x) => x.k === hl);
    ui.desc.innerHTML = f ? `<b style="color:${f.c}">${f.n}</b> · ${f.bits} bits · byte${f.bytes.length > 1 ? "s" : ""} ${f.bytes.join("–")}<br><span class="muted">${f.d}</span>` : `<span class="muted">Hover a field or a byte to see what it carries. Values are packed by <code>Header.pack()</code> and verified with <code>Header.unpack()</code>.</span>`;
    SW.$$("[data-k]", ui.grid).concat(SW.$$("[data-k]", ui.hex)).forEach((x) => {
      x.onmouseenter = () => { hl = x.dataset.k; draw(); };
    });
    ui.flagsOut.textContent = D.flags_set.length ? D.flags_set.join(" | ") : "none";
  }

  function compose(el, parts) {
    const tot = parts.reduce((a, p) => a + p[1], 0);
    el.innerHTML = `<div class="compose">${parts.map(([n, b, c]) => `<div style="flex:${b};background:${c}" title="${n}: ${b} B">${b >= 14 ? `${n} ${b}` : b}</div>`).join("")}</div>
      <div class="row between small mt-s"><span class="muted">${parts.map(([n, b]) => `${n} ${b} B`).join(" + ")}</span><b class="mono">${tot} B · ${fmt.n((tot * 8 * 50) / 1000, 1)} kbit/s at 50 pps</b></div>`;
  }

  SW.page("packet", {
    title: "Packet Anatomy",
    group: "Protocol Toolkit",
    mount(el) {
      ui.root = el;
      el.innerHTML = `
      <div class="split wide">
        <div class="card"><div class="card-h"><h3>Header fields</h3></div><div class="card-b" id="k-cfg"></div></div>
        <div class="stack">
          <div class="card"><div class="card-h"><h3>Wire layout</h3><span class="sub">16 bytes, network byte order — <code>struct "!BBHIIBBBB"</code></span></div>
            <div class="card-b"><div class="bitgrid" id="k-grid"></div><div class="mt" id="k-desc" style="min-height:44px;font-size:13px"></div></div></div>
          <div class="card"><div class="card-h"><h3>Packed bytes</h3><span class="sub">flags: <span class="mono" id="k-flags"></span></span></div><div class="card-b"><div class="hexdump" id="k-hex"></div></div></div>
          <div class="card"><div class="card-h"><h3>What protects what</h3></div><div class="card-b">
            <div class="compose" style="height:54px">
              <div style="flex:16;background:#2dd4bf">SWARA header · AAD</div><div style="flex:16;background:#5eead4">telemetry · AAD</div>
              <div style="flex:60;background:#c084fc">encrypted: replicas + ADPCM frame</div><div style="flex:16;background:#fbbf24">Poly1305 tag</div></div>
            <div class="small muted mt-s">The header and the 16-byte piggybacked telemetry report are passed to ChaCha20-Poly1305 as <b>associated data</b>: readable on the path, impossible to alter. Everything after them is ciphertext. The nonce is the unwrapped 64-bit sequence number, so a replayed or reordered packet fails authentication or the 64-entry replay window.</div>
          </div></div>
          <div class="grid g2">
            <div class="card"><div class="card-h"><h3>Media datagram · wideband</h3></div><div class="card-b" id="k-wb"></div></div>
            <div class="card"><div class="card-h"><h3>Media datagram · narrowband</h3></div><div class="card-b" id="k-nb"></div></div>
          </div>
          <div class="card"><div class="card-h"><h3>Media plaintext (before sealing)</h3></div><div class="card-b">
            <div class="code" style="padding-right:13px">+--------+------------------------------------------+-----------------+
| nrep 1B| nrep × { displacement 1B, len 2B, payload } |  primary frame  |
+--------+------------------------------------------+-----------------+
primary frame = 3-byte ADPCM preamble (predictor + step index) + 4-bit samples
              = 163 B wideband (320 samples) · 83 B narrowband (160 samples)</div></div></div>
        </div>
      </div>`;
      const cfg = el.querySelector("#k-cfg");
      const ts = h(`<div class="field"><label>Type</label><select></select></div>`);
      ui.type = ts.querySelector("select");
      ["HELLO", "RETRY", "AUTH_INIT", "AUTH_RESP", "AUTH_CONFIRM", "AUTH_TICKET", "MEDIA", "FEC", "ANCHOR", "TELEMETRY", "NACK", "PATH_MIGRATE", "PATH_ACK", "OVERLAY_LSA", "BYE", "CONTROL"].forEach((n, i) => ui.type.appendChild(h(`<option value="${i}" ${i === 6 ? "selected" : ""}>${i} · ${n}</option>`)));
      ui.type.onchange = packSoon; cfg.appendChild(ts);
      const fl = h(`<div class="field"><label>Flags</label><div class="grid g2" style="gap:8px"></div></div>`);
      [["TELEMETRY_PIGGYBACK", 1, true], ["MARKER", 2], ["DSCP_EF", 4], ["ENCRYPTED", 8, true], ["LAST_IN_GROUP", 16], ["RESUMED", 32]].forEach(([n, v, on]) => {
        const t = h(`<label class="check" style="font-size:11.5px"><input type="checkbox" data-flag="${v}" ${on ? "checked" : ""}><span class="tog"></span><span>${n.toLowerCase().replace(/_/g, " ")}</span></label>`);
        t.querySelector("input").onchange = packSoon; fl.querySelector(".grid").appendChild(t);
      });
      cfg.appendChild(fl);
      ui.seq = SW.slider({ label: "Sequence", min: 0, max: 65535, value: 4242, onInput: packSoon });
      ui.pi = SW.slider({ label: "PI score", min: 0, max: 255, value: 212, onInput: packSoon, hint: "≥160 → DSCP EF · ≥80 → AF41 · else best effort" });
      ui.grp = SW.slider({ label: "FEC group", min: 0, max: 255, value: 17, onInput: packSoon });
      ui.idx = SW.slider({ label: "FEC index", min: 0, max: 255, value: 2, onInput: packSoon, hint: "128+ marks a parity shard" });
      cfg.appendChild(ui.seq.el); cfg.appendChild(ui.pi.el);
      const cid = h(`<div class="field"><label>Connection ID (hex)</label><input type="text" value="5a17c0de" maxlength="8"></div>`);
      ui.cid = cid.querySelector("input"); ui.cid.oninput = packSoon; cfg.appendChild(cid);
      const tsf = h(`<div class="field"><label>Timestamp (ms)</label><input type="number" value="123456" min="0"></div>`);
      ui.ts = tsf.querySelector("input"); ui.ts.oninput = packSoon; cfg.appendChild(tsf);
      cfg.appendChild(ui.grp.el); cfg.appendChild(ui.idx.el);
      ui.grid = el.querySelector("#k-grid"); ui.hex = el.querySelector("#k-hex"); ui.desc = el.querySelector("#k-desc"); ui.flagsOut = el.querySelector("#k-flags");
      el.querySelector("#k-grid").onmouseleave = () => { hl = null; draw(); };
      compose(el.querySelector("#k-wb"), [["IP", 20, "#475569"], ["UDP", 8, "#64748b"], ["hdr", 16, "#2dd4bf"], ["tel", 16, "#5eead4"], ["n", 1, "#94a3b8"], ["ADPCM", 163, "#c084fc"], ["tag", 16, "#fbbf24"]]);
      compose(el.querySelector("#k-nb"), [["IP", 20, "#475569"], ["UDP", 8, "#64748b"], ["hdr", 16, "#2dd4bf"], ["tel", 16, "#5eead4"], ["n", 1, "#94a3b8"], ["ADPCM", 83, "#c084fc"], ["tag", 16, "#fbbf24"]]);
      pack();
    },
  });
})();
