#!/usr/bin/env python3
"""
SWARA end-to-end demonstration.

Starts a real server and two real clients in one process, each on its own UDP
socket, and runs an actual call between them.  Audio goes in as a waveform and
comes out as a waveform, having genuinely traversed: codec -> perceptual
analysis -> FEC -> AEAD -> UDP -> relay -> UDP -> AEAD -> repair -> jitter
buffer -> codec.

    python3 demo.py                              # clean network
    python3 demo.py --loss 0.15 --burst 4        # impaired
    python3 demo.py --loss 0.15 --compare        # A/B: swara vs uniform vs none
"""
from __future__ import annotations
import argparse, os, sys, threading, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swara.audio_io import AudioSink, AudioSource
from swara.codec import Mode
from swara.net import Impairment
from swara.session import CallSession
from swara.perceptual import damage_weight
from client import Client
import server as server_mod

PORT = 9310


def start_server(port, users_file):
    srv = server_mod.Server("127.0.0.1", port, users_file, verbose=False)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def audio_quality(sent: np.ndarray, recv: np.ndarray) -> dict:
    """Align the two waveforms and measure how much of the original survived.

    Alignment is done by exact cross-correlation via FFT rather than by
    stepping a coarse lag grid.  The coarse search that preceded it sampled
    every 80th lag and simply missed the true peak on some runs, reporting a
    correlation near zero for audio that was in fact intact -- a defect in the
    instrument that looked exactly like a defect in the system.
    """
    if recv.size < 1600 or sent.size < 1600:
        return {"corr": 0.0, "snr_db": -99.0, "coverage": 0.0, "lag_ms": 0.0}
    a = sent.astype(np.float64); b = recv.astype(np.float64)
    a -= a.mean(); b -= b.mean()
    n = 1 << int(np.ceil(np.log2(a.size + b.size)))
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    search = min(corr.size, 16000 * 4)          # allow up to 4 s of offset
    lag = int(np.argmax(corr[:search]))
    x = a[lag:]; y = b[:x.size]
    m = min(x.size, y.size)
    if m < 1600:
        return {"corr": 0.0, "snr_db": -99.0, "coverage": recv.size / max(1, sent.size),
                "lag_ms": lag / 16.0}
    x, y = x[:m], y[:m]
    denom = np.linalg.norm(x) * np.linalg.norm(y)
    c = float(np.dot(x, y) / denom) if denom > 0 else 0.0
    scale = np.dot(x, y) / max(np.dot(y, y), 1e-9)
    err = x - scale * y
    snr = 10 * np.log10(max(np.dot(x, x), 1e-9) / max(np.dot(err, err), 1e-9))
    return {"corr": c, "snr_db": snr, "coverage": recv.size / max(1, sent.size),
            "lag_ms": lag / 16.0}


def run_one(protect, args, users_file, port, tag=""):
    srv, _ = start_server(port, users_file)
    time.sleep(0.25)

    imp_a, imp_b = Impairment(), Impairment()
    for imp in (imp_a, imp_b):
        if args.loss > 0 or args.jitter > 0 or args.extra_delay > 0:
            imp.configure(loss_rate=args.loss, mean_burst=args.burst,
                          extra_delay_ms=args.extra_delay, jitter_ms=args.jitter,
                          enabled=True)

    alice = Client("alice", "hostel-wifi-1", ("127.0.0.1", port), imp_a, verbose=False)
    bob = Client("bob", "hostel-wifi-2", ("127.0.0.1", port), imp_b, verbose=False)
    assert alice.login(), "alice login failed"
    assert bob.login(), "bob login failed"

    got = {}
    def bob_wait():
        got["info"] = bob.wait_for({"incoming"}, 20.0)
    tb = threading.Thread(target=bob_wait, daemon=True); tb.start()
    time.sleep(0.3)
    # Reliable placement: retransmit with a fixed request id (server dedups),
    # then keep nudging until the callee's 'incoming' also survives the loss.
    a_info = alice.place_call("bob", timeout=15.0)
    deadline = time.time() + 15.0
    while got.get("info") is None and time.time() < deadline:
        alice.nudge_call()
        time.sleep(0.8)
    tb.join(timeout=2.0)
    b_info = got.get("info")
    assert a_info and a_info["op"] == "call_ready", f"call setup failed: {a_info}"
    assert b_info, "bob never received the invitation"
    assert b_info["call_id"] == a_info["call_id"], "dedup failed: mismatched call ids"

    src_a = AudioSource("synthetic", duration_s=args.seconds, seed=args.seed)
    sink_b = AudioSink("null")
    src_b = AudioSource("synthetic", duration_s=args.seconds, seed=args.seed + 100)
    sink_a = AudioSink("null")
    sent_a = src_a._buf.copy()

    sa = CallSession(alice.ep, alice.server, bytes.fromhex(a_info["tx"]),
                     bytes.fromhex(a_info["rx"]), a_info["call_id"], src_a, sink_a,
                     mode=Mode.WIDEBAND, protection=protect, name="alice")
    sb = CallSession(bob.ep, bob.server, bytes.fromhex(b_info["tx"]),
                     bytes.fromhex(b_info["rx"]), b_info["call_id"], src_b, sink_b,
                     mode=Mode.WIDEBAND, protection=protect, name="bob")
    # Identical channel realisation for every arm -- see Impairment.reset().
    imp_a.reset(1234 + int(args.seed)); imp_b.reset(4321 + int(args.seed))
    sa.start(); sb.start()

    limit = time.time() + args.seconds + 12
    while time.time() < limit and not (sa.finished.is_set() and sb.finished.is_set()):
        time.sleep(0.2)
    time.sleep(2.0)
    sa.stop(); sb.stop()
    src_a.close(); src_b.close(); sink_a.close(); sink_b.close()

    # Perceptual loss: weight each concealed frame by how much a listener
    # would actually have lost.  The sender knows every frame's PI; the
    # receiver knows which slots it had to conceal.
    lost = sb.concealed_seqs
    dmg, run, prev = 0.0, 0, -10
    for q in sorted(lost):
        run = run + 1 if q == prev + 1 else 0
        dmg += damage_weight(sa.sent_pi.get(q, 128), run)
        prev = q
    perceptual_pct = 100.0 * dmg / max(1, sb.stats.played)

    heard = sink_b.collected
    q = audio_quality(sent_a, heard)
    st = sa.stats.snapshot(); sr = sb.stats.snapshot()
    srv.stop()

    if args.outfile and protect == "swara":
        import wave
        with wave.open(args.outfile, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(heard.tobytes())
        print(f"  wrote received audio -> {args.outfile}")

    return {"perceptual_pct": perceptual_pct, "protect": protect, "sent_frames": st["tx_frames"], "parity": st["tx_parity"],
            "replicas": st["tx_replicas"], "played": sr["played"],
            "fec_rec": sr["recovered_fec"], "rep_rec": sr["recovered_replica"],
            "concealed": sr["concealed"],
            "loss_after_repair": sr["loss_after_repair_pct"],
            "auth_fail": sr["rx_auth_fail"],
            "tx_kbps": st["tx_bytes"] * 8 / max(1e-9, args.seconds) / 1000,
            "drops": alice.ep.dropped_by_impairment, "rx_frames": sr["rx_frames"],
            "tx_dgrams": alice.ep.tx_packets, **q}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--burst", type=float, default=1.0)
    ap.add_argument("--extra-delay", type=float, default=0.0)
    ap.add_argument("--jitter", type=float, default=0.0)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat each arm and average -- the live channel is noisy")
    ap.add_argument("--out", dest="outfile", default=None)
    a = ap.parse_args()

    users_file = "demo_users.json"
    if os.path.exists(users_file):
        os.remove(users_file)
    server_mod.save_user("alice", "hostel-wifi-1", users_file)
    server_mod.save_user("bob", "hostel-wifi-2", users_file)

    print(f"\nnetwork: loss={a.loss*100:.0f}% burst={a.burst} "
          f"+{a.extra_delay:.0f}ms jitter={a.jitter:.0f}ms   duration={a.seconds:.0f}s\n")
    arms = ["swara", "uniform", "none"] if a.compare else ["swara"]
    rows = []
    for i, arm in enumerate(arms):
        reps = []
        for rep in range(a.repeat):
            print(f"running arm '{arm}' ({rep+1}/{a.repeat}) ...", flush=True)
            a.seed = 7 + rep * 11
            reps.append(run_one(arm, a, users_file, PORT + i * 7 + rep * 3))
        avg = {k: (float(np.mean([r[k] for r in reps]))
                   if isinstance(reps[0][k], (int, float)) else reps[0][k])
               for k in reps[0]}
        avg["protect"] = arm
        rows.append(avg)

    print("\n" + "=" * 112)
    print(f"{'arm':<9}{'sent':>7}{'parity':>8}{'repl':>6}{'played':>8}{'fecRec':>8}{'repRec':>8}"
          f"{'conceal':>9}{'lossAfter':>11}{'kbps':>8}{'corr':>8}{'SNR dB':>9}{'authFail':>10}")
    print("-" * 112)
    for r in rows:
        print(f"{r['protect']:<9}{r['sent_frames']:>7.0f}{r['parity']:>8.0f}{r['replicas']:>6.0f}"
              f"{r['played']:>8.0f}{r['fec_rec']:>8.1f}{r['rep_rec']:>8.1f}{r['concealed']:>9.1f}"
              f"{r['loss_after_repair']:>10.1f}%{r['tx_kbps']:>8.1f}"
              f"{r['corr']:>8.3f}{r['snr_db']:>9.1f}{r['auth_fail']:>10}")
    print("=" * 112)
    if a.compare and len(rows) >= 2:
        s = next(r for r in rows if r["protect"] == "swara")
        n = next(r for r in rows if r["protect"] == "none")
        u = next((r for r in rows if r["protect"] == "uniform"), None)
        print(f"\nPerceptually weighted damage (lost frames x how audible each one was):")
        print(f"  unprotected {n['perceptual_pct']:.1f}%"
              + (f"   uniform FEC {u['perceptual_pct']:.1f}%" if u else "")
              + f"   SWARA {s['perceptual_pct']:.1f}%")
        print(f"  raw frames unrecoverable: none {n['loss_after_repair']:.1f}%"
              + (f", uniform {u['loss_after_repair']:.1f}%" if u else "")
              + f", swara {s['loss_after_repair']:.1f}%")
        if u:
            print(f"  wire rate: swara {s['tx_kbps']:.0f} kbit/s vs uniform {u['tx_kbps']:.0f} kbit/s "
                  f"({100*(1 - s['tx_kbps']/max(1e-9,u['tx_kbps'])):.0f}% less bandwidth)")
        if a.seconds < 25 or a.repeat < 2:
            print("  note: short single runs are noisy -- for a stable comparison use"
                  "\n        python3 demo.py --seconds 30 --repeat 3 --compare --loss 0.15 --burst 4")
    print()


if __name__ == "__main__":
    main()
