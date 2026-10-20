/* System & API — runtime, module inventory and every REST endpoint */
(function () {
  const { h, esc, fmt } = SW;

  SW.page("system", {
    title: "System & API",
    group: "Operations",
    async mount(el) {
      el.innerHTML = `<div class="grid g4" id="y-kpi"></div>
        <div class="grid g2 mt">
          <div class="card"><div class="card-h"><h3>Runtime</h3></div><div class="card-b" id="y-rt"></div></div>
          <div class="card"><div class="card-h"><h3>API</h3><span class="sub">OpenAPI 3 · FastAPI · served by uvicorn</span><div class="right"><a class="btn sm primary" href="/docs" target="_blank">Swagger UI</a><a class="btn sm" href="/redoc" target="_blank">ReDoc</a><a class="btn sm ghost" href="/openapi.json" target="_blank">openapi.json</a></div></div>
            <div class="card-b"><div class="small muted">Everything the console does goes through this API, so any of it can be scripted:</div>
            <div id="y-curl" class="mt-s"></div></div></div>
        </div>
        <div class="section-title"><h2>Source modules</h2><span class="sub">the code behind each page</span></div>
        <div class="card"><div class="table-wrap"><table class="t"><thead><tr><th>File</th><th class="num">Lines</th><th>Responsibility</th></tr></thead><tbody id="y-mod"></tbody></table></div></div>
        <div class="section-title"><h2>Endpoints</h2></div>
        <div class="card"><div class="table-wrap"><table class="t"><thead><tr><th>Method</th><th>Path</th><th>Summary</th><th>Group</th></tr></thead><tbody id="y-routes"></tbody></table></div></div>`;
      el.querySelector("#y-curl").appendChild(SW.codeBlock(`curl -X POST ${location.origin}/api/lab/call -H "Content-Type: application/json" \\\n  -d '{"seconds": 15, "loss": 0.15, "burst": 4, "protection": "swara"}'`));
      try {
        const d = await SW.api("/api/system");
        const lines = d.modules.reduce((a, m) => a + m.lines, 0);
        const kp = el.querySelector("#y-kpi");
        [["Python", d.python, d.implementation, "var(--accent)"], ["Source modules", d.modules.length, `${fmt.int(lines)} lines of Python`, "var(--info)"],
         ["Endpoints", d.routes.length, "documented in OpenAPI", "var(--uniform)"], ["Audio I/O", d.audio_backends.live ? "Live" : "Synthetic", d.audio_backends.live ? "sounddevice available" : "no sounddevice", "var(--replica)"]]
          .forEach(([l, v, s, c]) => { const k = SW.kpi(l, c); k.set(esc(String(v)), s); kp.appendChild(k.el); });
        el.querySelector("#y-rt").innerHTML = `<dl class="kv"><dt>Platform</dt><dd>${esc(d.platform)}</dd><dt>Machine</dt><dd>${esc(d.machine)} · ${d.cpu_count} logical CPUs</dd><dt>Interpreter</dt><dd title="${esc(d.executable)}">${esc(d.executable)}</dd>
          ${Object.entries(d.packages).map(([k, v]) => `<dt>${k}</dt><dd>${v ? esc(v) : '<span style="color:var(--bad)">not installed</span>'}</dd>`).join("")}</dl>`;
        el.querySelector("#y-mod").innerHTML = d.modules.map((m) => `<tr title="${esc(m.doc)}"><td class="mono">${esc(m.file)}</td><td class="num">${m.lines}</td><td class="small">${esc(m.summary)}</td></tr>`).join("");
        const mcol = { GET: "info", POST: "ok", DELETE: "bad" };
        el.querySelector("#y-routes").innerHTML = d.routes.sort((a, b) => a.path.localeCompare(b.path)).map((r) => `<tr><td>${r.methods.filter((m) => m !== "HEAD").map((m) => `<span class="badge ${mcol[m] || ""}">${m}</span>`).join(" ")}</td><td class="mono">${esc(r.path)}</td><td class="small">${esc(r.summary || "")}</td><td class="small muted">${esc((r.tags || []).join(", "))}</td></tr>`).join("");
      } catch (e) { SW.toast(e.message, "bad"); }
    },
  });
})();
