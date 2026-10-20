"""
The production relay, hosted inside the web process.

This is the same `server.Server` that `python server.py` runs: SPARK
authentication, presence, call setup and blind media relay on a real UDP port.
Hosting it here means command-line clients on this or another machine can log
in and call each other while the console shows who is online and which calls
are being relayed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque

import server as server_mod


class ObservedServer(server_mod.Server):
    """server.Server with its log lines captured instead of printed."""

    def __init__(self, *a, logbuf: deque, **kw):
        self._logbuf = logbuf
        super().__init__(*a, **kw)

    def log(self, msg):
        self._logbuf.append({"ts": time.time(), "msg": msg})


class RelayManager:
    def __init__(self, users_file: str):
        self.users_file = users_file
        self.srv: ObservedServer | None = None
        self.thread: threading.Thread | None = None
        self.host = "0.0.0.0"
        self.port = 9000
        self.started_at: float | None = None
        self.error: str | None = None
        self.logs: deque = deque(maxlen=400)
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.srv is not None and self.srv.running.is_set()

    def start(self, host: str = "0.0.0.0", port: int = 9000) -> dict:
        with self._lock:
            if self.running:
                return self.status()
            try:
                self.srv = ObservedServer(host, port, self.users_file, verbose=True,
                                          logbuf=self.logs)
            except OSError as e:
                self.srv = None
                self.error = f"cannot bind udp://{host}:{port}: {e.strerror or e}"
                self.logs.append({"ts": time.time(), "msg": self.error})
                return self.status()
            self.host, self.port, self.error = host, port, None
            self.thread = threading.Thread(target=self._serve, name="relay", daemon=True)
            self.thread.start()
            self.started_at = time.time()
            for _ in range(20):
                if self.srv.running.is_set():
                    break
                time.sleep(0.01)
        return self.status()

    def _serve(self):
        srv = self.srv
        try:
            srv.serve_forever()
        except Exception as e:                       # socket closed under us
            if srv.running.is_set():
                self.error = str(e)
                self.logs.append({"ts": time.time(), "msg": f"relay stopped: {e}"})

    def stop(self) -> dict:
        with self._lock:
            if self.srv is not None:
                self.srv.stop()
                self.logs.append({"ts": time.time(), "msg": "relay stopped by operator"})
            self.srv = None
            self.started_at = None
        return self.status()

    def add_user_record(self, rec) -> None:
        if self.srv is not None:
            self.srv.spark.add_user(rec)

    def remove_user(self, username: str) -> None:
        if self.srv is not None:
            self.srv.spark.users.pop(username, None)

    def status(self) -> dict:
        srv = self.srv
        base = {"running": self.running, "host": self.host, "port": self.port,
                "error": self.error, "started_at": self.started_at,
                "uptime_s": (time.time() - self.started_at) if self.started_at else 0}
        if srv is None:
            return {**base, "online": [], "calls": [], "users_known": 0,
                    "spark": {}, "packets": {}}
        online = [{"user": u, "addr": f"{s['addr'][0]}:{s['addr'][1]}",
                   "cid": f"{s['cid']:08x}"} for u, s in list(srv.online.items())]
        calls = [{"call_id": f"{cid:08x}", "a": c["a"], "b": c["b"],
                  "a_addr": f"{c['a_addr'][0]}:{c['a_addr'][1]}" if c.get("a_addr") else None,
                  "b_addr": f"{c['b_addr'][0]}:{c['b_addr'][1]}" if c.get("b_addr") else None}
                 for cid, c in list(srv.calls.items())]
        ep = srv.ep
        return {**base, "online": online, "calls": calls,
                "users_known": len(srv.spark.users),
                "spark": dict(srv.spark.stats),
                "packets": {"rx": ep.rx_packets, "tx": ep.tx_packets,
                            "rx_bytes": ep.rx_bytes, "tx_bytes": ep.tx_bytes}}

    def log_tail(self, since: float = 0.0) -> list:
        return [e for e in list(self.logs) if e["ts"] > since]


class ExperimentJob:
    """Runs experiments/run_all.py (about three minutes) and captures its log."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.started: float | None = None
        self.finished: float | None = None
        self.returncode: int | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> dict:
        if self.running:
            return self.status()
        self.lines, self.returncode, self.finished = [], None, None
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(self.base_dir, "experiments", "run_all.py")],
            cwd=self.base_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env)
        self.started = time.time()
        threading.Thread(target=self._pump, daemon=True).start()
        return self.status()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
        self.returncode = self.proc.wait()
        self.finished = time.time()

    def status(self, since: int = 0) -> dict:
        return {"running": self.running, "started": self.started, "finished": self.finished,
                "returncode": self.returncode, "lines": self.lines[since:],
                "count": len(self.lines),
                "elapsed_s": ((self.finished or time.time()) - self.started) if self.started else 0}
