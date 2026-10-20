#!/usr/bin/env python3
"""
SWARA Server Control — a desktop window that starts, stops and monitors the
web console (uvicorn + api_server) and its hosted UDP relay, so nobody has to
touch a terminal.

    pythonw launcher.py          # or double-click "SWARA Server.bat"
"""
from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import messagebox, ttk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "runtime", "launcher.json")

BG, PANEL, PANEL2, BORDER = "#070b14", "#0e1626", "#131d33", "#1d2940"
TEXT, MUTED, DIM = "#e7eef9", "#8193b2", "#566684"
ACCENT, OK, WARN, BAD = "#2dd4bf", "#34d399", "#fbbf24", "#f87171"
FONT, MONO = "Segoe UI", "Consolas"


def server_python() -> str:
    """The interpreter that runs uvicorn: python.exe next to us, not pythonw."""
    d = os.path.dirname(sys.executable)
    for name in ("python.exe", "python3", "python"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return sys.executable


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def fetch_json(url: str, timeout: float = 1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def port_busy(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _bindable(port: int, kind: int) -> bool:
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def tcp_free(port: int) -> bool:
    return not port_busy(port) and _bindable(port, socket.SOCK_STREAM)


def udp_free(port: int) -> bool:
    return _bindable(port, socket.SOCK_DGRAM)


def next_free(start: int, check, tries: int = 50):
    for p in range(start, min(65535, start + tries)):
        if check(p):
            return p
    return None


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SWARA Server Control")
        self.configure(bg=BG)
        self.geometry("980x700")
        self.minsize(860, 600)
        try:
            self.iconphoto(True, self._icon())
        except Exception:
            pass

        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.started_at: float | None = None
        self.state = "stopped"          # stopped | starting | running | external | stopping
        self.opened_browser = False
        self.settings = self._load_settings()

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._drain_log)
        self.after(400, self._poll)
        self._detect_external()
        if self.settings.get("autostart", True) and self.state == "stopped":
            self.after(300, self.start)

    # ------------------------------------------------------------------ ui
    def _icon(self):
        img = tk.PhotoImage(width=32, height=32)
        img.put(ACCENT, to=(0, 0, 32, 32))
        for x in range(4, 28):
            y = 16 + int(9 * __import__("math").sin((x - 4) / 3.2))
            img.put("#042f2e", to=(x, y - 1, x + 1, y + 2))
        return img

    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL2, bordercolor=BORDER,
                     font=(FONT, 10))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 9))
        st.configure("Title.TLabel", background=BG, foreground=TEXT, font=(FONT, 17, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
        st.configure("Big.TLabel", background=PANEL, foreground=TEXT, font=(FONT, 15, "bold"))
        st.configure("Value.TLabel", background=PANEL, foreground=TEXT, font=(MONO, 10))
        st.configure("Link.TLabel", background=PANEL, foreground=ACCENT, font=(MONO, 10, "underline"))
        st.configure("TCheckbutton", background=PANEL, foreground=TEXT, indicatorbackground=PANEL2,
                     indicatorforeground="#022c2a", indicatorrelief="flat", indicatormargin=(0, 0, 8, 0), padding=2)
        st.map("TCheckbutton", background=[("active", PANEL)],
               indicatorbackground=[("selected", ACCENT), ("!selected", PANEL2)])
        st.configure("TEntry", fieldbackground=PANEL2, foreground=TEXT, insertcolor=TEXT, padding=5)
        for name, bg, fg, hov in (("Primary", "#14b8a6", "#022c2a", "#2dd4bf"), ("Danger", "#3b1820", "#fecaca", "#4c1d26"),
                                  ("Ghost", PANEL2, TEXT, "#1b2944")):
            st.configure(f"{name}.TButton", background=bg, foreground=fg, bordercolor=BORDER, focusthickness=0,
                         padding=(14, 8), font=(FONT, 10, "bold"))
            st.map(f"{name}.TButton", background=[("disabled", "#151d2e"), ("active", hov)],
                   foreground=[("disabled", DIM)])

    def _card(self, parent, **grid):
        f = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        f.grid(**grid)
        return f

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        head = ttk.Frame(self, padding=(20, 16, 20, 8))
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="SWARA Server Control", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text="Secure Wideband Adaptive Real-time Audio over UDP — web console, REST API and UDP relay",
                  style="Sub.TLabel").pack(anchor="w")

        top = ttk.Frame(self, padding=(20, 4, 20, 8))
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(0, weight=3, minsize=470)
        top.columnconfigure(1, weight=2)

        # status card
        sc = self._card(top, row=0, column=0, sticky="nsew", padx=(0, 10))
        sc.columnconfigure(1, weight=1)
        row = tk.Frame(sc, bg=PANEL)
        row.grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 6))
        self.dot = tk.Canvas(row, width=16, height=16, bg=PANEL, highlightthickness=0)
        self.dot.pack(side="left")
        self.dot_id = self.dot.create_oval(2, 2, 14, 14, fill=DIM, outline="")
        self.status_lbl = ttk.Label(row, text="Stopped", style="Big.TLabel")
        self.status_lbl.pack(side="left", padx=(8, 0))
        self.kv = {}
        for i, (k, label) in enumerate((("url", "Console"), ("lan", "On your network"), ("share", "Share link (HTTPS)"),
                                        ("relay", "UDP relay"), ("engine", "Call engine"), ("uptime", "Uptime"))):
            links = k in ("url", "lan", "share")
            ttk.Label(sc, text=label, style="Muted.TLabel").grid(row=i + 1, column=0, sticky="w", padx=16, pady=2)
            v = ttk.Label(sc, text="—", style="Link.TLabel" if links else "Value.TLabel", cursor="hand2" if links else "")
            v.grid(row=i + 1, column=1, sticky="w", padx=(10, 16), pady=2)
            self.kv[k] = v
        self.kv["url"].bind("<Button-1>", lambda e: self.open_console())
        self.kv["lan"].bind("<Button-1>", lambda e: self._copy(self.kv["lan"].cget("text")))
        self.kv["share"].bind("<Button-1>", lambda e: self._copy(self.kv["share"].cget("text")))
        tk.Frame(sc, bg=PANEL, height=12).grid(row=9, column=0)

        # settings card
        cc = self._card(top, row=0, column=1, sticky="nsew")
        cc.columnconfigure(1, weight=1)
        ttk.Label(cc, text="Settings", style="Card.TLabel", font=(FONT, 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 8))
        self.web_port = tk.StringVar(value=str(self.settings.get("web_port", 8000)))
        self.relay_port = tk.StringVar(value=str(self.settings.get("relay_port", 9000)))
        self.https_port = tk.StringVar(value=str(self.settings.get("https_port", 8443)))
        self.https_on = tk.BooleanVar(value=self.settings.get("https_on", True))
        self.relay_auto = tk.BooleanVar(value=self.settings.get("relay_auto", True))
        self.open_browser = tk.BooleanVar(value=self.settings.get("open_browser", True))
        self.autostart = tk.BooleanVar(value=self.settings.get("autostart", True))
        for i, (label, var) in enumerate((("Web console port (HTTP)", self.web_port), ("Relay port (UDP)", self.relay_port),
                                          ("Share port (HTTPS)", self.https_port))):
            ttk.Label(cc, text=label, style="Muted.TLabel").grid(row=i + 1, column=0, sticky="w", padx=16, pady=3)
            e = ttk.Entry(cc, textvariable=var, width=8)
            e.grid(row=i + 1, column=1, sticky="e", padx=16, pady=3)
            setattr(self, f"entry{i}", e)
        for i, (label, var) in enumerate((("HTTPS share link (other devices need it for the mic)", self.https_on),
                                          ("Start the UDP relay with the server", self.relay_auto),
                                          ("Open the browser when ready", self.open_browser),
                                          ("Start the server when this window opens", self.autostart))):
            ttk.Checkbutton(cc, text=label, variable=var, command=self._save_settings).grid(row=i + 4, column=0, columnspan=2, sticky="w", padx=14, pady=1)

        # buttons
        bar = ttk.Frame(self, padding=(20, 6, 20, 8))
        bar.grid(row=2, column=0, sticky="ew")
        self.b_start = ttk.Button(bar, text="▶  Start server", style="Primary.TButton", command=self.start)
        self.b_stop = ttk.Button(bar, text="■  Stop", style="Danger.TButton", command=self.stop)
        self.b_restart = ttk.Button(bar, text="↻  Restart", style="Ghost.TButton", command=self.restart)
        self.b_open = ttk.Button(bar, text="Open console", style="Ghost.TButton", command=self.open_console)
        self.b_docs = ttk.Button(bar, text="API docs", style="Ghost.TButton", command=lambda: self._open("/docs"))
        for b in (self.b_start, self.b_stop, self.b_restart):
            b.pack(side="left", padx=(0, 8))
        for b in (self.b_docs, self.b_open):
            b.pack(side="right", padx=(8, 0))

        # log
        lc = self._card(self, row=3, column=0, sticky="nsew", padx=20, pady=(4, 18))
        lc.columnconfigure(0, weight=1)
        lc.rowconfigure(1, weight=1)
        lh = tk.Frame(lc, bg=PANEL)
        lh.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(10, 6))
        tk.Label(lh, text="Server log", bg=PANEL, fg=TEXT, font=(FONT, 11, "bold")).pack(side="left")
        tk.Button(lh, text="Clear", bg=PANEL, fg=MUTED, bd=0, activebackground=PANEL, activeforeground=TEXT,
                  cursor="hand2", command=lambda: self.log.delete("1.0", "end")).pack(side="right")
        self.log = tk.Text(lc, bg="#050912", fg="#a9b8d3", insertbackground=TEXT, bd=0, highlightthickness=0,
                           font=(MONO, 9), wrap="word", padx=10, pady=8)
        self.log.grid(row=1, column=0, sticky="nsew", padx=(14, 0), pady=(0, 14))
        sb = ttk.Scrollbar(lc, command=self.log.yview)
        sb.grid(row=1, column=1, sticky="ns", padx=(0, 8), pady=(0, 14))
        self.log.configure(yscrollcommand=sb.set)
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("err", foreground=BAD)
        self.log.tag_configure("me", foreground=ACCENT)
        self._render_state()

    # ------------------------------------------------------------- helpers
    def _load_settings(self) -> dict:
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self):
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        data = {"web_port": self._port(self.web_port, 8000), "relay_port": self._port(self.relay_port, 9000),
                "https_port": self._port(self.https_port, 8443), "https_on": self.https_on.get(),
                "relay_auto": self.relay_auto.get(), "open_browser": self.open_browser.get(),
                "autostart": self.autostart.get()}
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    @staticmethod
    def _port(var, default):
        try:
            p = int(var.get())
            return p if 1024 <= p <= 65535 else default
        except ValueError:
            return default

    def _say(self, msg, tag="me"):
        self.lines.put((f"[launcher {time.strftime('%H:%M:%S')}] {msg}\n", tag))

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._say(f"copied {text}")

    def base_url(self):
        return f"http://localhost:{self._port(self.web_port, 8000)}"

    def _open(self, path="/"):
        webbrowser.open(self.base_url() + path)

    def open_console(self):
        self._open("/")

    # ------------------------------------------------------------ actions
    def _detect_external(self):
        port = self._port(self.web_port, 8000)
        d = fetch_json(f"http://127.0.0.1:{port}/api")
        if d and "SWARA" in str(d.get("app", "")):
            self.state = "external"
            self._say(f"a SWARA server is already running on port {port} (started outside this window)", "warn")
            self._render_state()

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self._save_settings()
        port, rport = self._port(self.web_port, 8000), self._port(self.relay_port, 9000)
        if port_busy(port):
            d = fetch_json(f"http://127.0.0.1:{port}/api")
            if d and "SWARA" in str(d.get("app", "")):
                self.state = "external"
                self._say(f"SWARA is already running on port {port}; opening it", "warn")
                self._render_state()
                if self.open_browser.get():
                    self.open_console()
                return
            # Another program owns this port: move to the next free one rather than clash with it.
            free = next_free(port + 1, tcp_free)
            if free is None:
                messagebox.showerror("Port in use", f"Port {port} is used by another program and no free port\n"
                                     f"was found near it. Enter a different web port.")
                return
            self._say(f"port {port} is used by another program; using port {free} instead", "warn")
            port = free
            self.web_port.set(str(port))
        # A server that was just stopped releases its ports within a second or two;
        # wait for that before concluding the port belongs to someone else.
        for _ in range(8):
            if not self.relay_auto.get() or udp_free(rport):
                break
            time.sleep(0.5)
        if self.relay_auto.get() and not udp_free(rport):
            free = next_free(rport + 1, udp_free)
            if free is not None:
                self._say(f"UDP port {rport} is in use; the relay will listen on udp:{free} instead", "warn")
                rport = free
                self.relay_port.set(str(rport))
        hport = 0
        if self.https_on.get():
            hport = self._port(self.https_port, 8443)
            if not tcp_free(hport):
                free = next_free(hport + 1, tcp_free)
                if free is not None:
                    self._say(f"port {hport} is in use; HTTPS will use port {free} instead", "warn")
                    hport = free
                    self.https_port.set(str(hport))
        self._save_settings()
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                   SWARA_RELAY_PORT=str(rport), SWARA_RELAY_AUTOSTART="1" if self.relay_auto.get() else "0")
        cmd = [server_python(), "api_server.py", "--host", "0.0.0.0", "--port", str(port), "--https-port", str(hport)]
        flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(cmd, cwd=BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace", creationflags=flags)
        except Exception as e:
            messagebox.showerror("Could not start", str(e))
            return
        self.state, self.started_at, self.opened_browser = "starting", time.time(), False
        self._say(f"starting: {' '.join(cmd[1:])}  (relay udp:{rport}{'' if self.relay_auto.get() else ', off'})")
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self._render_state()

    # Routine polling by this window and the console would drown the log.
    QUIET = ('"GET /api/status', '"GET /api/relay', '"GET /api/lab/', '"GET /api/experiments/job',
             '"GET /static/', '"GET /figures/')

    def _pump(self, proc):
        for line in proc.stdout:
            if any(q in line for q in self.QUIET) and " 200 " in line:
                continue
            low = line.lower()
            tag = "err" if ("error" in low or "traceback" in low) else "warn" if "warning" in low else "ok" if "running on" in low or "startup complete" in low else ""
            self.lines.put((line, tag))
        code = proc.wait()
        self.lines.put((f"[launcher {time.strftime('%H:%M:%S')}] server exited (code {code})\n", "warn" if code else "me"))

    def stop(self):
        if self.state == "external":
            messagebox.showinfo("External server", "This server was started outside the launcher.\n"
                                "Stop it from the terminal that started it (Ctrl+C).")
            return
        if not self.proc or self.proc.poll() is not None:
            return
        self.state = "stopping"
        self._render_state()
        self._say("stopping server…")
        proc = self.proc

        def _kill():
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
        threading.Thread(target=_kill, daemon=True).start()

    def restart(self):
        if self.state == "external":
            self.stop()
            return
        self.stop()

        def _wait():
            if self.proc and self.proc.poll() is None:
                self.after(300, _wait)
            else:
                self.after(400, self.start)
        self.after(300, _wait)

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Quit", "Stop the SWARA server and close the launcher?"):
                return
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._save_settings()
        self.destroy()

    # ------------------------------------------------------------ polling
    def _drain_log(self):
        n = 0
        while not self.lines.empty() and n < 300:
            line, tag = self.lines.get_nowait()
            self.log.insert("end", line, tag or ())
            n += 1
        if n:
            self.log.see("end")
            if int(self.log.index("end-1c").split(".")[0]) > 3000:
                self.log.delete("1.0", "1000.0")
        self.after(150, self._drain_log)

    def _poll(self):
        own = self.proc is not None and self.proc.poll() is None
        if self.proc is not None and not own and self.state in ("starting", "running", "stopping"):
            self.state, self.started_at = "stopped", None
        st = None
        if own or self.state == "external":
            st = fetch_json(f"http://127.0.0.1:{self._port(self.web_port, 8000)}/api/status", 0.8)
            if st and self.state == "starting":
                self.state = "running"
                self._say(f"ready — {self.base_url()}/", "ok")
                if self.open_browser.get() and not self.opened_browser:
                    self.opened_browser = True
                    self.open_console()
            if not st and self.state == "external":
                self.state = "stopped"
        self._render_state(st)
        self.after(1500, self._poll)

    def _render_state(self, st: dict | None = None):
        colors = {"stopped": DIM, "starting": WARN, "running": OK, "external": OK, "stopping": WARN}
        labels = {"stopped": "Stopped", "starting": "Starting…", "running": "Running",
                  "external": "Running (external)", "stopping": "Stopping…"}
        self.dot.itemconfigure(self.dot_id, fill=colors[self.state])
        self.status_lbl.configure(text=labels[self.state])
        live = self.state in ("running", "external")
        port = self._port(self.web_port, 8000)
        self.kv["url"].configure(text=f"http://localhost:{port}/" if live else "—")
        self.kv["lan"].configure(text=f"http://{lan_ip()}:{port}/" if live else "—")
        if st:
            r = st.get("relay", {})
            self.kv["relay"].configure(text=(f"udp:{r.get('port')} · {r.get('online', 0)} online · {r.get('calls', 0)} calls"
                                             if r.get("running") else (r.get("error") or "not running")))
            hp = st.get("https_port")
            self.kv["share"].configure(text=f"https://{st.get('lan_ip') or lan_ip()}:{hp}/" if hp else "off")
            lab = st.get("lab", {})
            phones = st.get("phones") or []
            self.kv["engine"].configure(text=(f"{lab.get('kind')} · {lab.get('phase')}" if lab.get("busy")
                                              else "experiments running" if st.get("experiments", {}).get("running")
                                              else (f"{len(phones)} softphone(s): " + ", ".join(f"{p['user']} ({p['state']})" for p in phones[:4]))
                                              if phones else "idle"))
            up = st.get("uptime_s", 0)
            self.kv["uptime"].configure(text=f"{int(up // 3600)}h {int(up % 3600 // 60)}m {int(up % 60)}s")
        elif not live:
            for k in ("share", "relay", "engine", "uptime"):
                self.kv[k].configure(text="—")
        running_own = self.proc is not None and self.proc.poll() is None
        self.b_start.state(["disabled"] if running_own or self.state in ("external", "starting", "stopping") else ["!disabled"])
        self.b_stop.state(["!disabled"] if running_own and self.state != "stopping" else ["disabled"])
        self.b_restart.state(["!disabled"] if running_own and self.state == "running" else ["disabled"])
        self.b_open.state(["!disabled"] if live else ["disabled"])
        self.b_docs.state(["!disabled"] if live else ["disabled"])
        for e in (self.entry0, self.entry1, self.entry2):
            e.state(["disabled"] if running_own or self.state == "external" else ["!disabled"])


if __name__ == "__main__":
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    Launcher().mainloop()
