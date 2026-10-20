"""
Interactive wrappers around the SWARA library for the console's toolkit pages.

Every function here calls the real implementation in `swara/`; nothing is
re-implemented for display purposes.
"""
from __future__ import annotations

import base64
import hashlib
import os
import platform
import sys
import time
from dataclasses import asdict

import numpy as np

from swara import emodel
from swara.auth import SparkClient, SparkServer, register
from swara.channel import PROFILES, ChannelConfig
from swara.controller import BITRATE_LADDER, NavikController, dscp_for
from swara.engine import compare
from swara.fec import FecScheduler, ReedSolomon
from swara.overlay import LinkMeasurement, SetuOverlay
from swara.packet import HEADER_LEN, Flag, Header, PType
from swara.perceptual import PerceptualImportanceAnalyzer, damage_weight
from swara.speech import SpeechGenerator

SR = 16_000
FRAME = 320

PROFILE_TITLES = {
    "ideal_lan": "Ideal LAN", "good_wifi": "Good Wi-Fi", "busy_wifi": "Busy Wi-Fi",
    "congested_4g": "Congested 4G", "poor_3g": "Poor 3G", "hostel_wifi": "Hostel Wi-Fi",
    "satellite": "Satellite",
}


# ---------------------------------------------------------------------------
# Quality model (ITU-T G.107 E-model)
# ---------------------------------------------------------------------------
def emodel_eval(codec: str, delay_ms: float, loss_pct: float, burst: float,
                mobile: bool) -> dict:
    if codec not in emodel.CODEC_PARAMS:
        raise ValueError(f"unknown codec {codec}")
    br = emodel.burst_ratio(burst, loss_pct / 100.0)
    idd = emodel.delay_impairment(delay_ms)
    ie = emodel.equipment_impairment(codec, loss_pct, br)
    a = emodel.A_MOBILE if mobile else emodel.A_WIRED
    r = emodel.r_factor(codec, delay_ms, loss_pct, burst, mobile)
    m = emodel.r_to_mos(r)
    losses = [round(x * 0.5, 1) for x in range(0, 61)]
    delays = list(range(0, 501, 10))
    return {
        "r": r, "mos": m, "label": emodel.quality_label(m),
        "breakdown": {"R0": emodel.R0_DEFAULT, "Is": emodel.IS_DEFAULT, "Id": idd,
                      "Ie_eff": ie, "A": a, "burst_ratio": br},
        "codec_params": {"ie": emodel.CODEC_PARAMS[codec][0],
                         "bpl": emodel.CODEC_PARAMS[codec][1],
                         "kbps": emodel.CODEC_PARAMS[codec][2]},
        "curve_loss": {"x": losses,
                       "y": [emodel.mos(codec, delay_ms, L, burst, mobile) for L in losses]},
        "curve_delay": {"x": delays,
                        "y": [emodel.mos(codec, d, loss_pct, burst, mobile) for d in delays]},
        "codecs": [{"codec": c, "kbps": emodel.CODEC_PARAMS[c][2],
                    "mos": emodel.mos(c, delay_ms, loss_pct, burst, mobile)}
                   for c in emodel.CODEC_PARAMS],
    }


def _decision_dict(d) -> dict:
    return {"codec": d.codec, "bitrate_kbps": d.bitrate_kbps, "rho": d.rho,
            "dscp_high": d.dscp_high, "dscp_low": d.dscp_low,
            "predicted_r": d.predicted_r, "predicted_mos": d.predicted_mos,
            "offered_kbps": d.offered_kbps, "reason": d.reason}


def navik_eval(loss_pct: float, burst: float, jitter: float, owd: float, depth: float,
               capacity: float, pi_afec: bool, mobile: bool, k: int = 4) -> dict:
    def ctl():
        c = NavikController(k=k, mobile=mobile, pi_afec=pi_afec)
        c.capacity_est_kbps = c._cap_ewma = float(capacity)
        return c
    d = ctl().decide(loss_pct / 100.0, burst, jitter, owd, depth)
    sweep_x = [x * 1.0 for x in range(0, 31)]
    sweep = [ctl().decide(L / 100.0, burst, jitter, owd, depth) for L in sweep_x]
    marks = {"voiced (PI 200)": dscp_for(200, d), "unvoiced (PI 110)": dscp_for(110, d),
             "silence (PI 5)": dscp_for(5, d)}
    return {"decision": _decision_dict(d), "dscp": marks,
            "ladder": BITRATE_LADDER,
            "sweep": {"loss": sweep_x, "rho": [s.rho for s in sweep],
                      "mos": [s.predicted_mos for s in sweep],
                      "kbps": [s.bitrate_kbps for s in sweep],
                      "codec": [s.codec for s in sweep],
                      "offered": [s.offered_kbps for s in sweep]}}


# ---------------------------------------------------------------------------
# Perceptual importance
# ---------------------------------------------------------------------------
def perceptual_analysis(pcm: np.ndarray, truth: list | None, k: int, rho: float,
                        burst: float, include_audio: bool = True) -> dict:
    x = pcm.astype(np.float64) / 32768.0
    n = x.size // FRAME
    an = PerceptualImportanceAnalyzer()
    frames = []
    for i in range(n):
        seg = x[i * FRAME:(i + 1) * FRAME]
        f = an.analyze(seg)
        frames.append({"pi": int(f.pi), "label": f.label(), "energy_db": round(f.energy_db, 1),
                       "voicing": round(f.voicing, 3), "flux": round(f.flux, 3),
                       "onset": round(f.onset, 3), "zcr": round(f.zcr, 3),
                       "min": round(float(seg.min()), 4), "max": round(float(seg.max()), 4),
                       "damage": round(damage_weight(int(f.pi)), 4),
                       "truth": truth[i] if truth and i < len(truth) else None})
    sched = FecScheduler(k=k, rho_net=rho, mean_burst=burst)
    groups = []
    for g in range(n // k):
        pis = [frames[g * k + j]["pi"] for j in range(k)]
        plan = sched.plan(pis)
        groups.append({"start": g * k, "r": plan.r, "anchors": [list(a) for a in plan.anchors],
                       "mean_pi": round(plan.group_mean_pi, 3), "burst_mode": plan.burst_mode})
    classes = {}
    for f in frames:
        c = classes.setdefault(f["label"], {"n": 0, "pi_sum": 0})
        c["n"] += 1; c["pi_sum"] += f["pi"]
    total_pi = sum(f["pi"] for f in frames) or 1
    stats = {lab: {"n": c["n"], "share": c["n"] / max(1, n), "mean_pi": c["pi_sum"] / c["n"],
                   "pi_share": c["pi_sum"] / total_pi} for lab, c in classes.items()}
    out = {"frames": frames, "groups": groups, "stats": stats, "seconds": n * 0.02,
           "parity_total": sum(g["r"] for g in groups),
           "anchors_total": sum(len(g["anchors"]) for g in groups),
           "realised_overhead": sched.realised_overhead}
    if include_audio:
        out["audio"] = base64.b64encode(pcm[: n * FRAME].astype("<i2").tobytes()).decode()
    return out


def synthetic_speech(seconds: float, seed: int) -> tuple[np.ndarray, list]:
    frames, sig = SpeechGenerator(seed=seed).frames(seconds, 20)
    pcm = np.clip(sig * 26000.0, -32768, 32767).astype(np.int16)
    return pcm, [kind for _, _, kind in frames]


def fec_plan(pis: list[int], rho: float, burst: float) -> dict:
    s = FecScheduler(k=len(pis), rho_net=rho, mean_burst=burst)
    p = s.plan(pis)
    return {"k": p.k, "r": p.r, "anchors": [list(a) for a in p.anchors],
            "mean_pi": p.group_mean_pi, "burst_mode": p.burst_mode,
            "overhead_ratio": p.overhead_ratio,
            "damage": [damage_weight(int(x)) for x in pis]}


# ---------------------------------------------------------------------------
# Reed-Solomon playground
# ---------------------------------------------------------------------------
def rs_playground(message: str, k: int, r: int, lost: list[int]) -> dict:
    if not 1 <= k <= 16 or not 0 <= r <= 8:
        raise ValueError("k must be 1..16 and r 0..8")
    data = message.encode("utf-8") or b"\x00"
    shard_len = -(-len(data) // k)
    padded = data.ljust(shard_len * k, b"\x00")
    shards = [padded[i * shard_len:(i + 1) * shard_len] for i in range(k)]
    rs = ReedSolomon(k, r)
    t0 = time.perf_counter()
    parity = rs.encode(shards) if r else []
    enc_us = (time.perf_counter() - t0) * 1e6
    allp = shards + parity
    lost_set = {i for i in lost if 0 <= i < k + r}
    received = {i: allp[i] for i in range(k + r) if i not in lost_set}
    missing_data = [i for i in range(k) if i in lost_set]
    t0 = time.perf_counter()
    out = None
    if not missing_data:
        out = {i: shards[i] for i in range(k)}
    elif len(received) >= k:
        try:
            out = rs.decode(received, shard_len)
        except Exception:
            out = None
    dec_us = (time.perf_counter() - t0) * 1e6
    ok = out is not None and all(i in out for i in range(k))
    recovered = b"".join(out[i] for i in range(k)).rstrip(b"\x00") if ok else None

    def view(b: bytes):
        return {"hex": b.hex(" "), "text": b.decode("utf-8", errors="replace")}
    return {
        "k": k, "r": r, "n": k + r, "shard_len": shard_len,
        "shards": [{"index": i, "kind": "data" if i < k else "parity", "lost": i in lost_set,
                    "rebuilt": i in missing_data and ok, **view(allp[i])} for i in range(k + r)],
        "received": len(received), "missing_data": missing_data,
        "success": ok, "recovered": recovered.decode("utf-8", errors="replace") if ok else None,
        "intact": ok and recovered == data.rstrip(b"\x00"),
        "encode_us": enc_us, "decode_us": dec_us,
        "overhead_pct": 100.0 * r / k,
    }


# ---------------------------------------------------------------------------
# SPARK handshake, run in memory with real cryptography
# ---------------------------------------------------------------------------
def _fp(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def spark_handshake(username: str, password: str, attempt: str) -> dict:
    t_reg = time.perf_counter()
    rec, _ = register(username, password)
    reg_ms = (time.perf_counter() - t_reg) * 1000
    srv = SparkServer()
    srv.add_user(rec)
    addr = ("198.51.100.7", 40123)
    cli = SparkClient(username, attempt)
    steps = []

    def step(frm, to, name, payload, desc, ms):
        steps.append({"from": frm, "to": to, "name": name, "bytes": len(payload) + HEADER_LEN,
                      "payload_bytes": len(payload), "desc": desc, "ms": round(ms, 3),
                      "preview": payload[:24].hex(" ")})

    t0 = time.perf_counter(); hello = cli.make_hello(); t = time.perf_counter()
    step("client", "server", "HELLO", hello,
         "Padded probe. No identity revealed, no server state allocated.", (t - t0) * 1000)
    t0 = time.perf_counter(); retry = srv.handle_hello(addr, hello); t = time.perf_counter()
    step("server", "client", "RETRY", retry,
         "Stateless HMAC cookie bound to the client address. Smaller than HELLO, so amplification < 1.",
         (t - t0) * 1000)
    t0 = time.perf_counter(); init = cli.make_auth_init(retry); t = time.perf_counter()
    step("client", "server", "AUTH_INIT", init,
         "Echoes the cookie (proves return routability) plus an ephemeral X25519 key and the username.",
         (t - t0) * 1000)
    t0 = time.perf_counter(); resp, pending = srv.handle_auth_init(addr, init); t = time.perf_counter()
    step("server", "client", "AUTH_RESP", resp,
         "Salt, OPAQUE-style envelope, server ephemeral and static keys. No password-equivalent is stored.",
         (t - t0) * 1000)
    t0 = time.perf_counter(); tag = cli.make_auth_confirm(resp); t = time.perf_counter()
    client_ms = (t - t0) * 1000
    result = {"steps": steps, "register_ms": reg_ms, "record_bytes": rec.size_bytes(),
              "username": username, "password_ok": tag is not None}
    if tag is None:
        result.update({"outcome": "wrong_password",
                       "detail": "The envelope failed to open under the stretched password. "
                                 "Detected locally on the client; AUTH_CONFIRM is never sent, "
                                 "so the server learns nothing it could use as an oracle.",
                       "client_ms": client_ms})
        return _hs_totals(result)
    step("client", "server", "AUTH_CONFIRM", tag,
         "Envelope opened (PBKDF2), triple-DH mixed into the transcript, HMAC key-confirmation tag.",
         client_ms)
    t0 = time.perf_counter(); keys, ticket = srv.handle_auth_confirm(pending, tag); t = time.perf_counter()
    if keys is None:
        result.update({"outcome": "rejected", "detail": "server rejected the confirmation tag"})
        return _hs_totals(result)
    tick_msg = keys.cid.to_bytes(4, "big") + len(ticket).to_bytes(2, "big") + ticket
    step("server", "client", "AUTH_TICKET", tick_msg,
         "Mutual authentication complete. Encrypted resumption ticket; the server keeps no per-session table.",
         (t - t0) * 1000)
    cli.ticket = ticket
    t0 = time.perf_counter(); mig = cli.make_path_migrate()
    m = srv.handle_path_migrate(("203.0.113.9", 51000), mig)
    mig_ms = (time.perf_counter() - t0) * 1000
    result.update({
        "outcome": "authenticated",
        "detail": "Both sides derived identical session keys; the password never crossed the wire.",
        "keys": {"cid": f"{keys.cid:08x}",
                 "client_c2s_fp": _fp(cli.keys.k_c2s), "server_c2s_fp": _fp(keys.k_c2s),
                 "client_s2c_fp": _fp(cli.keys.k_s2c), "server_s2c_fp": _fp(keys.k_s2c),
                 "resume_fp": _fp(keys.k_resume), "transcript_fp": _fp(keys.transcript),
                 "match": cli.keys.k_c2s == keys.k_c2s and cli.keys.k_s2c == keys.k_s2c},
        "migration": {"bytes": len(mig) + HEADER_LEN, "ms": mig_ms, "ok": m is not None,
                      "rtts": 0, "new_addr": "203.0.113.9:51000"},
        "amplification": len(retry) / len(hello),
        "server_stats": dict(srv.stats),
    })
    return _hs_totals(result)


def _hs_totals(r: dict) -> dict:
    r["total_bytes"] = sum(s["bytes"] for s in r["steps"])
    r["total_ms"] = sum(s["ms"] for s in r["steps"])
    r["rtts"] = 2 if r.get("outcome") == "authenticated" else None
    return r


# ---------------------------------------------------------------------------
# Packet header
# ---------------------------------------------------------------------------
FLAG_NAMES = {name: getattr(Flag, name) for name in
              ("TELEMETRY_PIGGYBACK", "MARKER", "DSCP_EF", "ENCRYPTED", "LAST_IN_GROUP", "RESUMED")}


def pack_header(ptype: int, flags: int, seq: int, cid: int, ts_ms: int, pi: int,
                fec_group: int, fec_index: int) -> dict:
    h = Header(ptype=ptype, flags=flags, seq=seq, cid=cid, ts_ms=ts_ms, pi=pi,
               fec_group=fec_group, fec_index=fec_index)
    raw = h.pack()
    back = Header.unpack(raw)
    return {"hex": raw.hex(), "bytes": list(raw), "length": len(raw),
            "type_name": PType.NAMES.get(ptype, "?"),
            "flags_set": [n for n, v in FLAG_NAMES.items() if flags & v],
            "roundtrip": {"ptype": back.ptype, "version": back.version, "flags": back.flags,
                          "seq": back.seq, "cid": back.cid, "ts_ms": back.ts_ms, "pi": back.pi,
                          "fec_group": back.fec_group, "fec_index": back.fec_index},
            "types": PType.NAMES, "flags": FLAG_NAMES}


# ---------------------------------------------------------------------------
# SETU overlay
# ---------------------------------------------------------------------------
CITY_NAMES = ["Delhi", "Mumbai", "Chennai", "Kolkata", "Bengaluru", "Pune",
              "Hyderabad", "Jaipur", "Lucknow", "Kochi", "Indore", "Guwahati"]


def overlay_eval(n: int, bad_frac: float, seed: int) -> dict:
    n = max(3, min(len(CITY_NAMES), n))
    rng = np.random.default_rng(seed)
    ov = SetuOverlay()
    ids = CITY_NAMES[:n]
    for i in ids:
        ov.add_node(i)
    links = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            bad = bool(rng.random() < bad_frac)
            if bad:
                m = LinkMeasurement(rng.uniform(70, 190), rng.uniform(20, 60), rng.uniform(0.04, 0.18))
            else:
                m = LinkMeasurement(rng.uniform(8, 55), rng.uniform(1, 12), rng.uniform(0.0, 0.02))
            ov.set_link(a, b, m)
            links.append({"a": a, "b": b, "owd": m.owd_ms, "jitter": m.jitter_ms,
                          "loss": m.loss, "cost": m.cost, "congested": bad})
    t0 = time.perf_counter()
    rounds = ov.converge()
    conv_ms = (time.perf_counter() - t0) * 1000
    pairs = ov.improvement_report()
    improved = [p for p in pairs if p["gain_pct"] > 1.0]
    nodes = []
    for i, nid in enumerate(ids):
        ang = 2 * np.pi * i / n - np.pi / 2
        nodes.append({"id": nid, "x": float(np.cos(ang)), "y": float(np.sin(ang))})
    return {"nodes": nodes, "links": links, "pairs": pairs, "rounds": rounds,
            "converge_ms": conv_ms, "improved": len(improved), "total_pairs": len(pairs),
            "mean_gain": float(np.mean([p["gain_pct"] for p in improved])) if improved else 0.0,
            "weights": {"alpha": 1.0, "beta": 2.0, "gamma": 45.0, "relay_penalty": 12.0}}


# ---------------------------------------------------------------------------
# Offline simulator (the evaluation engine)
# ---------------------------------------------------------------------------
def profiles() -> list[dict]:
    return [{"id": k, "title": PROFILE_TITLES.get(k, k), "loss": c.loss_rate,
             "burst": c.mean_burst, "delay": c.base_delay_ms, "jitter": c.jitter_ms,
             "capacity": c.capacity_kbps, "reorder": c.reorder_prob,
             "label": c.label()} for k, c in PROFILES.items()]


def _cfg(p: dict) -> ChannelConfig:
    return ChannelConfig(loss_rate=float(p["loss"]), mean_burst=max(1.0, float(p["burst"])),
                         base_delay_ms=float(p["delay"]), jitter_ms=float(p["jitter"]),
                         capacity_kbps=float(p.get("capacity", 800)),
                         reorder_prob=float(p.get("reorder", 0.0)))


def simulate(p: dict) -> dict:
    t0 = time.perf_counter()
    res = compare(_cfg(p), duration_s=float(p.get("duration", 20)), seed=int(p.get("seed", 3)),
                  codec=p.get("codec", "opus-24k"), mobile=bool(p.get("mobile", False)))
    out = {}
    for k, v in res.items():
        d = asdict(v)
        d.pop("jb_curve", None)
        d["quality"] = emodel.quality_label(v.mos)
        out[k] = d
    return {"systems": out, "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "channel": _cfg(p).label()}


def simulate_sweep(p: dict) -> dict:
    losses = [0.0, 0.03, 0.06, 0.09, 0.12, 0.15, 0.20, 0.25, 0.30]
    out = {s: {"mos": [], "perc": [], "redund": []} for s in ("baseline", "uniform_fec", "swara")}
    t0 = time.perf_counter()
    for L in losses:
        res = compare(_cfg({**p, "loss": L}), duration_s=float(p.get("duration", 12)),
                      seed=int(p.get("seed", 3)), mobile=bool(p.get("mobile", False)))
        for s, r in res.items():
            out[s]["mos"].append(r.mos)
            out[s]["perc"].append(r.perceptual_loss_pct)
            out[s]["redund"].append(r.redundancy_pct)
    return {"losses": losses, **out, "elapsed_ms": (time.perf_counter() - t0) * 1000}


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
def module_inventory(base_dir: str) -> list[dict]:
    out = []
    for folder in ("", "swara"):
        d = os.path.join(base_dir, folder)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            path = os.path.join(d, fn)
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
            doc = ""
            for q in ('"""', "'''"):
                i = src.find(q)
                if 0 <= i < 400:
                    j = src.find(q, i + 3)
                    doc = src[i + 3:j].strip()
                    break
            lines = [ln.strip() for ln in doc.splitlines() if ln.strip()]
            out.append({"file": f"{folder}/{fn}" if folder else fn, "lines": src.count("\n") + 1,
                        "summary": lines[0] if lines else "", "doc": doc[:1200]})
    return out


def system_info() -> dict:
    import importlib.metadata as md
    pk = {}
    for name in ("fastapi", "uvicorn", "numpy", "matplotlib", "cryptography", "pydantic",
                 "sounddevice", "starlette"):
        try:
            pk[name] = md.version(name)
        except Exception:
            pk[name] = None
    return {"python": sys.version.split()[0], "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(),
            "cpu_count": os.cpu_count(), "packages": pk, "executable": sys.executable}
