#!/usr/bin/env python3
"""
SWARA web console and REST API.

    python api_server.py                 # http://localhost:8000
    uvicorn api_server:app --port 8000   # equivalent

Serves the operations console at /, the OpenAPI docs at /docs, and hosts the
real UDP relay (server.py) on udp://0.0.0.0:9000 inside the same process so
command-line clients can connect to it while the console watches.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import server as server_mod                                   # noqa: E402
from server import load_users, save_user                       # noqa: E402
from webapp import tools                                       # noqa: E402
from webapp.lab import LabManager, pcm_to_wav                  # noqa: E402
from webapp.relay import ExperimentJob, RelayManager           # noqa: E402
from webapp.softphone import AGENTS, PhoneAgent                # noqa: E402
from webapp.tls import ensure_cert, lan_ip                     # noqa: E402

FIG_DIR = os.path.join(BASE_DIR, "figures")
STATIC_DIR = os.path.join(BASE_DIR, "static")
RESULTS_FILE = os.path.join(BASE_DIR, "experiments", "results.json")
USERS_FILE = os.path.join(BASE_DIR, server_mod.USERS_FILE)
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
UPLOAD_DIR = os.path.join(RUNTIME_DIR, "uploads")
TLS_DIR = os.path.join(RUNTIME_DIR, "tls")
LAB_USERS_FILE = os.path.join(RUNTIME_DIR, "lab_users.json")
os.makedirs(UPLOAD_DIR, exist_ok=True)
HTTPS_PORT = int(os.environ.get("SWARA_HTTPS_PORT", "0"))     # set by the launcher / __main__

RELAY_PORT = int(os.environ.get("SWARA_RELAY_PORT", "9000"))
RELAY_AUTOSTART = os.environ.get("SWARA_RELAY_AUTOSTART", "1") != "0"
STARTED = time.time()

relay = RelayManager(USERS_FILE)
lab = LabManager(LAB_USERS_FILE)
experiments = ExperimentJob(BASE_DIR)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if RELAY_AUTOSTART:
        relay.start("0.0.0.0", RELAY_PORT)
    yield
    relay.stop()
    for run in list(lab.history.values()):
        run.stop()


app = FastAPI(
    title="SWARA API & Control Console",
    description=("Secure Wideband Adaptive Real-time Audio over UDP. "
                 "REST API behind the operations console at `/`: live instrumented calls, "
                 "A/B benchmarks, the hosted UDP relay, and interactive access to every "
                 "protocol component (E-model, NAVIK, PI analyser, Reed-Solomon, SPARK, SETU)."),
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


def read_results() -> dict:
    if not os.path.exists(RESULTS_FILE):
        raise HTTPException(404, "Results file not found. Run the experiments first.")
    with open(RESULTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def wav_response(pcm: np.ndarray, name: str) -> Response:
    if pcm is None or pcm.size == 0:
        raise HTTPException(404, "no audio for this run")
    return Response(pcm_to_wav(pcm), media_type="audio/wav",
                    headers={"Content-Disposition": f'inline; filename="{name}.wav"',
                             "Cache-Control": "no-store"})


# ===========================================================================
# Models
# ===========================================================================
class RegisterRequest(BaseModel):
    username: str
    password: str


class DemoRequest(BaseModel):
    seconds: float = 10.0
    loss: float = 0.15
    burst: float = 2.0
    compare: bool = True


class CallRequest(BaseModel):
    seconds: float = Field(15.0, ge=3, le=60, description="call length")
    loss: float = Field(0.10, ge=0, le=0.6, description="long-run packet loss, 0..1")
    burst: float = Field(3.0, ge=1, le=20, description="mean loss burst, packets")
    jitter: float = Field(10.0, ge=0, le=200, description="delay jitter, ms")
    extra_delay: float = Field(0.0, ge=0, le=500, description="added one-way delay, ms")
    protection: Literal["swara", "uniform", "none"] = "swara"
    mode: Literal["wideband", "narrowband"] = "wideband"
    source: Literal["synthetic", "upload", "mic"] = "synthetic"
    upload_id: Optional[str] = None
    seed: int = 7


class BenchRequest(CallRequest):
    repeat: int = Field(1, ge=1, le=3)


class ImpairmentUpdate(BaseModel):
    loss: Optional[float] = Field(None, ge=0, le=0.6)
    burst: Optional[float] = Field(None, ge=1, le=20)
    jitter: Optional[float] = Field(None, ge=0, le=200)
    extra_delay: Optional[float] = Field(None, ge=0, le=500)


class RelayStart(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(9000, ge=1024, le=65535)


class EModelRequest(BaseModel):
    codec: str = "opus-24k"
    delay_ms: float = Field(150.0, ge=0, le=800)
    loss_pct: float = Field(3.0, ge=0, le=50)
    burst: float = Field(1.0, ge=1, le=20)
    mobile: bool = False


class NavikRequest(BaseModel):
    loss_pct: float = Field(8.0, ge=0, le=50)
    burst: float = Field(3.0, ge=1, le=20)
    jitter_ms: float = Field(12.0, ge=0, le=200)
    owd_ms: float = Field(40.0, ge=0, le=500)
    depth_ms: float = Field(80.0, ge=0, le=400)
    capacity_kbps: float = Field(400.0, ge=8, le=5000)
    pi_afec: bool = True
    mobile: bool = False


class PerceptualRequest(BaseModel):
    seconds: float = Field(8.0, ge=1, le=20)
    seed: int = 7
    k: int = Field(4, ge=2, le=12)
    rho: float = Field(0.3, ge=0, le=1)
    burst: float = Field(2.0, ge=1, le=20)


class FecPlanRequest(BaseModel):
    pis: list[int] = Field(default_factory=lambda: [20, 210, 180, 90])
    rho: float = Field(0.3, ge=0, le=1)
    burst: float = Field(2.0, ge=1, le=20)


class RSRequest(BaseModel):
    message: str = Field("SWARA: the same budget, spent where it is heard.", max_length=400)
    k: int = Field(4, ge=1, le=16)
    r: int = Field(2, ge=0, le=8)
    lost: list[int] = Field(default_factory=list)


class HandshakeRequest(BaseModel):
    username: str = Field("alice", min_length=1, max_length=40)
    password: str = Field("hostel-wifi-1", min_length=1, max_length=128)
    attempt: Optional[str] = Field(None, description="password the client types; defaults to the correct one")


class HeaderRequest(BaseModel):
    ptype: int = Field(6, ge=0, le=15)
    flags: int = Field(0x09, ge=0, le=255)
    seq: int = Field(4242, ge=0, le=65535)
    cid: int = Field(0x5A17C0DE, ge=0, le=0xFFFFFFFF)
    ts_ms: int = Field(123456, ge=0, le=0xFFFFFFFF)
    pi: int = Field(212, ge=0, le=255)
    fec_group: int = Field(17, ge=0, le=255)
    fec_index: int = Field(2, ge=0, le=255)


class OverlayRequest(BaseModel):
    nodes: int = Field(7, ge=3, le=12)
    bad_frac: float = Field(0.28, ge=0, le=1)
    seed: int = 4


class SimRequest(BaseModel):
    loss: float = Field(0.10, ge=0, le=0.6)
    burst: float = Field(4.0, ge=1, le=20)
    delay: float = Field(60.0, ge=0, le=600)
    jitter: float = Field(25.0, ge=0, le=200)
    capacity: float = Field(400.0, ge=50, le=20000)
    reorder: float = Field(0.0, ge=0, le=0.3)
    duration: float = Field(20.0, ge=4, le=60)
    seed: int = 3
    codec: str = "opus-24k"
    mobile: bool = False


# ===========================================================================
# Console & health
# ===========================================================================
@app.get("/", include_in_schema=False)
def console():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api", summary="API health & metadata", tags=["System"])
def api_root():
    return {
        "status": "online",
        "app": "SWARA — Secure Wideband Adaptive Real-time Audio over UDP",
        "version": app.version,
        "uptime_s": time.time() - STARTED,
        "udp_relay": f"{relay.host}:{relay.port}" if relay.running else None,
        "console": "/", "swagger_docs": "/docs", "redoc": "/redoc",
    }


@app.get("/api/status", summary="Live system status for the console header", tags=["System"])
def api_status():
    active = lab.active
    return {
        "uptime_s": time.time() - STARTED,
        "relay": {"running": relay.running, "port": relay.port, "host": relay.host,
                  "online": len(relay.srv.online) if relay.srv else 0,
                  "calls": len(relay.srv.calls) if relay.srv else 0, "error": relay.error},
        "lab": {"busy": lab.busy(), "kind": getattr(active, "kind", None) if lab.busy() else None,
                "id": active.id if active is not None and lab.busy() else None,
                "phase": active.phase if active is not None else None},
        "experiments": {"running": experiments.running},
        "audio_backends": lab.audio_backends(),
        "results_available": os.path.exists(RESULTS_FILE),
        "phones": [a.describe() for a in list(AGENTS)],
        "https_port": HTTPS_PORT or None,
        "lan_ip": lan_ip(),
    }


# ===========================================================================
# Softphone: a person in a browser, a real SWARA client in this process
# ===========================================================================
@app.get("/api/phone/defaults", summary="Defaults for the Softphone page", tags=["Softphone"])
def phone_defaults():
    return {
        "relay": f"127.0.0.1:{relay.port}" if relay.running else "127.0.0.1:9000",
        "relay_running": relay.running,
        "device_audio": lab.audio_backends().get("live", False),
        "users": list(load_users(USERS_FILE)),
        "lan_ip": lan_ip(),
        "https_port": HTTPS_PORT or None,
        "secure": True,
    }


@app.websocket("/ws/phone")
async def ws_phone(ws: WebSocket):
    """One browser tab = one agent.  Text frames carry JSON commands and events;
    binary frames carry 16 kHz int16 PCM in both directions."""
    await ws.accept()
    loop = asyncio.get_running_loop()
    outq: asyncio.Queue = asyncio.Queue()
    agent: PhoneAgent | None = None

    async def pump():
        while True:
            item = await outq.get()
            if isinstance(item, (bytes, bytearray)):
                await ws.send_bytes(item)
            else:
                await ws.send_text(item)

    sender = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                if agent is not None:
                    agent.push_audio(msg["bytes"])
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                cmd = json.loads(text)
            except ValueError:
                continue
            if cmd.get("op") == "login":
                if agent is not None and agent.alive():
                    await ws.send_text(json.dumps({"op": "error", "msg": "already signed in on this connection"}))
                    continue
                try:
                    agent = PhoneAgent(cmd, loop, outq)
                    agent.start()
                except ValueError as e:
                    await ws.send_text(json.dumps({"op": "error", "msg": str(e), "fatal": True}))
            elif agent is not None:
                agent.command(cmd)
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        if agent is not None:
            agent.stop()


@app.get("/api/system", summary="Runtime, packages and module inventory", tags=["System"])
def api_system():
    return {**tools.system_info(), "modules": tools.module_inventory(BASE_DIR),
            "audio_backends": lab.audio_backends(),
            "routes": [{"path": r.path, "methods": sorted(getattr(r, "methods", []) or []),
                        "summary": getattr(r, "summary", None), "tags": getattr(r, "tags", [])}
                       for r in app.routes if getattr(r, "include_in_schema", False)]}


# ===========================================================================
# Users (original endpoints kept for compatibility)
# ===========================================================================
@app.get("/users", summary="List registered users", tags=["Users"])
def list_registered_users():
    users = load_users(USERS_FILE)
    return {"count": len(users), "usernames": list(users.keys())}


@app.get("/api/users", summary="Registered users with stored-record details", tags=["Users"])
def api_users():
    users = load_users(USERS_FILE)
    online = set(relay.srv.online) if relay.srv else set()
    return {"count": len(users), "users": [
        {"username": u, "record_bytes": r.size_bytes(), "salt": r.salt.hex(),
         "pub": r.client_static_pub.hex(), "envelope_bytes": len(r.envelope),
         "online": u in online} for u, r in users.items()]}


@app.post("/users/register", summary="Register a new user", tags=["Users"])
@app.post("/api/users", summary="Register a new user", tags=["Users"], include_in_schema=False)
def register_new_user(req: RegisterRequest):
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(400, "Username and password are required")
    if len(username) > 40 or not all(c.isalnum() or c in "-_." for c in username):
        raise HTTPException(400, "Username may contain letters, digits, '-', '_' and '.' (max 40)")
    save_user(username, req.password, USERS_FILE)
    rec = load_users(USERS_FILE)[username]
    relay.add_user_record(rec)
    return {"message": f"User '{username}' registered successfully",
            "record_bytes": rec.size_bytes()}


@app.delete("/api/users/{username}", summary="Delete a registered user", tags=["Users"])
def delete_user(username: str):
    if not os.path.exists(USERS_FILE):
        raise HTTPException(404, "no users file")
    with open(USERS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    if username not in raw:
        raise HTTPException(404, f"user '{username}' not found")
    raw.pop(username)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
    relay.remove_user(username)
    return {"message": f"User '{username}' deleted"}


# ===========================================================================
# Relay
# ===========================================================================
@app.get("/api/relay", summary="Hosted UDP relay status", tags=["Relay"])
def relay_status(since: float = 0.0):
    return {**relay.status(), "logs": relay.log_tail(since)}


@app.post("/api/relay/start", summary="Start the hosted UDP relay", tags=["Relay"])
def relay_start(req: RelayStart):
    return relay.start(req.host, req.port)


@app.post("/api/relay/stop", summary="Stop the hosted UDP relay", tags=["Relay"])
def relay_stop():
    return relay.stop()


# ===========================================================================
# Live call lab
# ===========================================================================
def _upload_path(req: CallRequest) -> Optional[str]:
    if req.source != "upload":
        if req.source == "mic" and not lab.audio_backends().get("live"):
            raise HTTPException(400, "server microphone unavailable (sounddevice not installed)")
        return None
    if not req.upload_id or not req.upload_id.isalnum():
        raise HTTPException(400, "upload_id required for source=upload")
    p = os.path.join(UPLOAD_DIR, f"{req.upload_id}.wav")
    if not os.path.exists(p):
        raise HTTPException(404, "uploaded clip not found; record or upload it again")
    return p


@app.post("/api/lab/call", summary="Start an instrumented live call", tags=["Live lab"])
def lab_call(req: CallRequest):
    try:
        run = lab.start_call(req.model_dump(), _upload_path(req))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return run.describe()


@app.post("/api/lab/bench", summary="Start an A/B/C benchmark (SWARA vs uniform FEC vs none)",
          tags=["Live lab"])
def lab_bench(req: BenchRequest):
    try:
        run = lab.start_bench(req.model_dump(), _upload_path(req))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return run.describe()


@app.get("/api/lab/active", summary="The run currently in progress, if any", tags=["Live lab"])
def lab_active():
    a = lab.active
    return {"busy": lab.busy(), "run": a.describe() if a is not None else None}


@app.get("/api/lab/{rid}", summary="Describe a call or benchmark", tags=["Live lab"])
def lab_get(rid: str):
    run = lab.get(rid)
    if run is None:
        raise HTTPException(404, "unknown run")
    return run.describe()


@app.post("/api/lab/{rid}/stop", summary="Hang up / cancel a run", tags=["Live lab"])
def lab_stop(rid: str):
    run = lab.get(rid)
    if run is None:
        raise HTTPException(404, "unknown run")
    run.stop()
    return {"stopping": True}


@app.post("/api/lab/{rid}/impairment", summary="Change the network during a live call",
          tags=["Live lab"])
def lab_impair(rid: str, req: ImpairmentUpdate):
    run = lab.get(rid)
    if run is None or getattr(run, "kind", "") != "call":
        raise HTTPException(404, "unknown call")
    run.configure_impairment(req.loss, req.burst, req.jitter, req.extra_delay)
    return {"impairment": run.impairment}


@app.get("/api/lab/{rid}/stream", summary="Server-sent events: telemetry, frame fates, audio",
         tags=["Live lab"])
async def lab_stream(rid: str, request: Request, audio: int = 1):
    run = lab.get(rid)
    if run is None or getattr(run, "kind", "") != "call":
        raise HTTPException(404, "unknown call")

    async def gen():
        cur = {"t": 0, "e": 0, "f": 0, "a": 0}
        yield f"event: hello\ndata: {json.dumps(run.describe())}\n\n"
        while True:
            if await request.is_disconnected():
                return
            finished = run.done.is_set()
            payload = run.poll(cur, with_audio=bool(audio))
            yield f"data: {json.dumps(payload)}\n\n"
            if finished:
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/lab/{rid}/audio/{which}.wav", summary="Sent or received audio of a finished call",
         tags=["Live lab"])
def lab_audio(rid: str, which: Literal["sent", "received"]):
    run = lab.get(rid)
    if run is None or getattr(run, "kind", "") != "call":
        raise HTTPException(404, "unknown call")
    return wav_response(run.sent_pcm if which == "sent" else run.heard_pcm, f"swara-{rid}-{which}")


@app.post("/api/audio/upload", summary="Upload a clip (raw 16 kHz mono int16 little-endian PCM)",
          tags=["Live lab"])
async def audio_upload(request: Request):
    body = await request.body()
    if len(body) < 3200 or len(body) > 16000 * 2 * 61:
        raise HTTPException(400, "clip must be between 0.1 s and 60 s of 16 kHz int16 PCM")
    pcm = np.frombuffer(body[: len(body) // 2 * 2], dtype="<i2").astype(np.int16)
    uid = uuid.uuid4().hex[:12]
    with open(os.path.join(UPLOAD_DIR, f"{uid}.wav"), "wb") as f:
        f.write(pcm_to_wav(pcm))
    for fn in sorted(os.listdir(UPLOAD_DIR), key=lambda n: os.path.getmtime(os.path.join(UPLOAD_DIR, n)))[:-12]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, fn))
        except OSError:
            pass
    return {"upload_id": uid, "seconds": pcm.size / 16000.0,
            "peak": int(np.abs(pcm).max()) if pcm.size else 0}


# ===========================================================================
# Offline simulator
# ===========================================================================
@app.get("/api/sim/profiles", summary="Named network profiles", tags=["Simulator"])
def sim_profiles():
    return {"profiles": tools.profiles()}


@app.post("/api/sim/compare", summary="Simulate SWARA vs uniform FEC vs baseline", tags=["Simulator"])
def sim_compare(req: SimRequest):
    return tools.simulate(req.model_dump())


@app.post("/api/sim/sweep", summary="MOS against packet loss for all three systems", tags=["Simulator"])
def sim_sweep(req: SimRequest):
    return tools.simulate_sweep(req.model_dump())


# ===========================================================================
# Toolkit
# ===========================================================================
@app.post("/api/tools/emodel", summary="ITU-T G.107 E-model evaluation", tags=["Toolkit"])
def tool_emodel(req: EModelRequest):
    try:
        return tools.emodel_eval(req.codec, req.delay_ms, req.loss_pct, req.burst, req.mobile)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tools/navik", summary="NAVIK joint controller decision", tags=["Toolkit"])
def tool_navik(req: NavikRequest):
    return tools.navik_eval(req.loss_pct, req.burst, req.jitter_ms, req.owd_ms, req.depth_ms,
                            req.capacity_kbps, req.pi_afec, req.mobile)


@app.post("/api/tools/perceptual", summary="Perceptual-importance analysis of synthetic speech",
          tags=["Toolkit"])
def tool_perceptual(req: PerceptualRequest):
    pcm, truth = tools.synthetic_speech(req.seconds, req.seed)
    return tools.perceptual_analysis(pcm, truth, req.k, req.rho, req.burst)


@app.post("/api/tools/perceptual/pcm", summary="Perceptual analysis of raw 16 kHz int16 PCM",
          tags=["Toolkit"])
async def tool_perceptual_pcm(request: Request, k: int = 4, rho: float = 0.3, burst: float = 2.0):
    body = await request.body()
    if len(body) < 3200:
        raise HTTPException(400, "clip too short")
    pcm = np.frombuffer(body[: min(len(body), 16000 * 2 * 20) // 2 * 2], dtype="<i2").astype(np.int16)
    return tools.perceptual_analysis(pcm, None, max(2, min(12, k)), rho, burst)


@app.post("/api/tools/fec/plan", summary="PI-AFEC plan for one group", tags=["Toolkit"])
def tool_fec_plan(req: FecPlanRequest):
    if not 2 <= len(req.pis) <= 12:
        raise HTTPException(400, "2..12 PI scores")
    return tools.fec_plan([max(0, min(255, p)) for p in req.pis], req.rho, req.burst)


@app.post("/api/tools/rs", summary="Reed-Solomon encode, erase, decode", tags=["Toolkit"])
def tool_rs(req: RSRequest):
    try:
        return tools.rs_playground(req.message, req.k, req.r, req.lost)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tools/handshake", summary="Run a SPARK handshake in memory", tags=["Toolkit"])
def tool_handshake(req: HandshakeRequest):
    return tools.spark_handshake(req.username, req.password,
                                 req.attempt if req.attempt is not None else req.password)


@app.post("/api/tools/header", summary="Pack a 16-byte SWARA header", tags=["Toolkit"])
def tool_header(req: HeaderRequest):
    return tools.pack_header(**req.model_dump())


@app.post("/api/tools/overlay", summary="SETU distance-vector overlay", tags=["Toolkit"])
def tool_overlay(req: OverlayRequest):
    return tools.overlay_eval(req.nodes, req.bad_frac, req.seed)


# ===========================================================================
# Research results & figures (original endpoints kept)
# ===========================================================================
@app.get("/experiments/results", summary="Research benchmark results", tags=["Research"])
def get_results():
    return read_results()


@app.get("/api/experiments/job", summary="Experiment regeneration job status", tags=["Research"])
def experiments_status(since: int = 0):
    return experiments.status(since)


@app.post("/api/experiments/run", summary="Regenerate all results and figures (~3 min)",
          tags=["Research"])
def experiments_run():
    if lab.busy():
        raise HTTPException(409, "a live lab run is in progress; wait for it to finish")
    return experiments.start()


@app.get("/figures", summary="List research figures", tags=["Research"])
def list_figures():
    if not os.path.exists(FIG_DIR):
        return {"count": 0, "figures": []}
    files = sorted(f for f in os.listdir(FIG_DIR) if f.endswith(".png"))
    return {"count": len(files), "figures": files,
            "mtime": {f: os.path.getmtime(os.path.join(FIG_DIR, f)) for f in files}}


@app.get("/figures/{filename}", summary="Get a figure image", tags=["Research"])
def get_figure_file(filename: str):
    safe = os.path.basename(filename)
    file_path = os.path.join(FIG_DIR, safe)
    if not safe.endswith(".png") or not os.path.exists(file_path):
        raise HTTPException(404, "Figure not found")
    return FileResponse(file_path, media_type="image/png")


@app.post("/demo/run", summary="Run demo.py and return its console output", tags=["Research"])
def run_demo(req: DemoRequest):
    if lab.busy():
        raise HTTPException(409, "a live lab run is in progress")
    cmd = [sys.executable, os.path.join(BASE_DIR, "demo.py"), "--seconds", str(req.seconds)]
    if req.loss > 0:
        cmd.extend(["--loss", str(req.loss)])
    if req.burst > 1:
        cmd.extend(["--burst", str(req.burst)])
    if req.compare:
        cmd.append("--compare")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=RUNTIME_DIR,
                          encoding="utf-8", errors="replace")
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


@app.exception_handler(Exception)
async def unhandled(_req: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    """Serve HTTP (and, unless disabled, HTTPS on a second port for phones and
    other devices, whose browsers allow the microphone only on secure origins)."""
    import argparse
    import threading

    import uvicorn

    ap = argparse.ArgumentParser(description="SWARA web console")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--https-port", type=int, default=8443, help="0 disables the HTTPS listener")
    a = ap.parse_args()

    global HTTPS_PORT
    https_line = ""
    if a.https_port:
        try:
            cert, key = ensure_cert(TLS_DIR)
            scfg = uvicorn.Config(app, host=a.host, port=a.https_port, ssl_certfile=cert, ssl_keyfile=key,
                                  log_level="info")
            threading.Thread(target=uvicorn.Server(scfg).run, name="https", daemon=True).start()
            HTTPS_PORT = a.https_port
            https_line = f"  Share link      https://{lan_ip()}:{a.https_port}/   (other devices; accept the certificate warning)\n"
        except Exception as e:                      # HTTPS is a convenience, never fatal
            print(f"  HTTPS disabled: {e}")
    print(f"\n  SWARA console   http://localhost:{a.port}/\n"
          f"  On the network  http://{lan_ip()}:{a.port}/\n{https_line}"
          f"  API docs        http://localhost:{a.port}/docs\n"
          f"  UDP relay       udp://0.0.0.0:{RELAY_PORT}\n", flush=True)
    uvicorn.Server(uvicorn.Config(app, host=a.host, port=a.port, log_level="info")).run()


if __name__ == "__main__":
    main()
