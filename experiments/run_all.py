#!/usr/bin/env python3
"""Run the complete SWARA evaluation and write results + figures."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

from swara import emodel
from swara.channel import ChannelConfig, PROFILES
from swara.engine import CallSimulator, compare
from swara.perceptual import PerceptualImportanceAnalyzer, damage_weight
from swara.speech import SpeechGenerator

FIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "figures")
os.makedirs(FIG, exist_ok=True)
RESULTS: dict = {}

# ---- house style -----------------------------------------------------------
C_BASE, C_UNI, C_SWARA = "#8B8B8B", "#E08D2F", "#0E7C7B"
C_ACCENT, C_DARK = "#C1292E", "#22223B"
plt.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 170, "font.size": 10,
    "font.family": "DejaVu Sans", "axes.grid": True, "grid.alpha": 0.25,
    "grid.linestyle": "-", "axes.spines.top": False, "axes.spines.right": False,
    "axes.labelweight": "bold", "axes.titleweight": "bold", "axes.titlesize": 11.5,
    "legend.frameon": False, "figure.facecolor": "white",
})


def save(fig, name):
    p = os.path.join(FIG, name)
    fig.savefig(p, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"    wrote {name}")


# ===========================================================================
def exp1_loss_sweep(seeds=(5, 17, 29, 41, 53), duration=22.0):
    """Headline: MOS vs packet loss, all three systems, equal budget."""
    print("[1] loss sweep")
    losses = [0.0, 0.02, 0.05, 0.08, 0.12, 0.16, 0.20, 0.25, 0.30]
    out = {s: {"mos": [], "err": [], "perc": []} for s in ("baseline", "uniform_fec", "swara")}
    for L in losses:
        cfg = ChannelConfig(loss_rate=L, mean_burst=3.0, base_delay_ms=40.0,
                            jitter_ms=14.0, capacity_kbps=800)
        acc = defaultdict(list)
        for sd in seeds:
            for k, v in compare(cfg, duration_s=duration, seed=sd).items():
                acc[k].append(v)
        for s in out:
            m = [r.mos for r in acc[s]]
            out[s]["mos"].append(float(np.mean(m)))
            out[s]["err"].append(float(np.std(m) / np.sqrt(len(m))))
            out[s]["perc"].append(float(np.mean([r.perceptual_loss_pct for r in acc[s]])))
        print(f"    loss {L*100:4.0f}%  base {out['baseline']['mos'][-1]:.2f}  "
              f"uni {out['uniform_fec']['mos'][-1]:.2f}  swara {out['swara']['mos'][-1]:.2f}")
    RESULTS["loss_sweep"] = {"losses": losses, **out}

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.1))
    x = np.array(losses) * 100
    for s, c, lab, mk in [("baseline", C_BASE, "Baseline (plain UDP, no FEC)", "o"),
                          ("uniform_fec", C_UNI, "Uniform adaptive FEC (equal budget)", "s"),
                          ("swara", C_SWARA, "SWARA (PI-AFEC)", "D")]:
        ax.errorbar(x, out[s]["mos"], yerr=out[s]["err"], color=c, marker=mk,
                    ms=5, lw=2.2, capsize=3, label=lab)
    ax.axhspan(3.6, 4.5, color="#2E7D32", alpha=0.06)
    ax.axhline(3.6, color="#2E7D32", lw=1, ls="--", alpha=0.6)
    ax.text(30, 3.63, "acceptable call quality", ha="right", fontsize=8, color="#2E7D32")
    ax.set_xlabel("Channel packet loss (%)")
    ax.set_ylabel("Predicted MOS (ITU-T G.107)")
    ax.set_title("Call quality under loss")
    ax.legend(fontsize=8.2, loc="lower left")
    ax.set_ylim(1, 4.6)

    gain = np.array(out["swara"]["perc"]) / np.maximum(np.array(out["baseline"]["perc"]), 1e-9)
    red = (1 - gain) * 100
    ax2.bar(x, red, width=1.9, color=C_SWARA, alpha=0.9)
    ax2.set_xlabel("Channel packet loss (%)")
    ax2.set_ylabel("Reduction in perceptual loss (%)")
    ax2.set_title("Perceptually-weighted loss removed vs baseline")
    ax2.set_ylim(0, max(70, red.max() * 1.15))
    for xi, ri in zip(x, red):
        if ri > 1:
            ax2.text(xi, ri + 1.2, f"{ri:.0f}", ha="center", fontsize=7.5, color=C_DARK)
    fig.tight_layout()
    save(fig, "fig1_loss_sweep.png")


# ===========================================================================
def exp2_burst_sweep(seeds=(5, 17, 29, 41), duration=22.0):
    """Burst resilience: the case interleaving cannot help with."""
    print("[2] burst sweep")
    bursts = [1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0]
    out = {s: [] for s in ("baseline", "uniform_fec", "swara")}
    anchors, fecrec = [], []
    for B in bursts:
        cfg = ChannelConfig(loss_rate=0.12, mean_burst=B, base_delay_ms=40.0,
                            jitter_ms=14.0, capacity_kbps=800)
        acc = defaultdict(list)
        for sd in seeds:
            for k, v in compare(cfg, duration_s=duration, seed=sd).items():
                acc[k].append(v)
        for s in out:
            out[s].append(float(np.mean([r.mos for r in acc[s]])))
        anchors.append(float(np.mean([r.frames_recovered_anchor for r in acc["swara"]])))
        fecrec.append(float(np.mean([r.frames_recovered_fec for r in acc["swara"]])))
        print(f"    burst {B:4.1f}  base {out['baseline'][-1]:.2f}  "
              f"uni {out['uniform_fec'][-1]:.2f}  swara {out['swara'][-1]:.2f}")
    RESULTS["burst_sweep"] = {"bursts": bursts, **out,
                              "anchor_recoveries": anchors, "fec_recoveries": fecrec}

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.1))
    for s, c, lab, mk in [("baseline", C_BASE, "Baseline", "o"),
                          ("uniform_fec", C_UNI, "Uniform FEC", "s"),
                          ("swara", C_SWARA, "SWARA", "D")]:
        ax.plot(bursts, out[s], color=c, marker=mk, ms=5, lw=2.2, label=lab)
    ax.set_xlabel("Mean loss burst length (packets)")
    ax.set_ylabel("Predicted MOS")
    ax.set_title("Resilience to bursty loss (12% average loss)")
    ax.legend(fontsize=8.5)

    w = 0.38
    idx = np.arange(len(bursts))
    ax2.bar(idx - w/2, fecrec, w, label="Reed-Solomon parity", color=C_UNI)
    ax2.bar(idx + w/2, anchors, w, label="Perceptual anchor replica", color=C_SWARA)
    ax2.set_xticks(idx); ax2.set_xticklabels([f"{b:g}" for b in bursts])
    ax2.set_xlabel("Mean loss burst length (packets)")
    ax2.set_ylabel("Frames recovered per 25 s call")
    ax2.set_title("Which mechanism does the rescuing")
    ax2.legend(fontsize=8.5)
    fig.tight_layout()
    save(fig, "fig2_burst_sweep.png")


# ===========================================================================
def exp3_overhead_efficiency(seeds=(5, 17, 29, 41), duration=22.0):
    """MOS achieved per unit of redundancy spent."""
    print("[3] overhead efficiency")
    rhos = [0.0, 0.075, 0.15, 0.225, 0.30, 0.40, 0.50]
    cfg = ChannelConfig(loss_rate=0.12, mean_burst=4.0, base_delay_ms=40.0,
                        jitter_ms=14.0, capacity_kbps=800)
    sw, un, cost_s, cost_u = [], [], [], []
    for rho in rhos:
        a, b, ca, cb = [], [], [], []
        for sd in seeds:
            r1 = CallSimulator("swara", cfg, "opus-24k", duration, sd, forced_rho=rho).run()
            r2 = CallSimulator("uniform_fec", cfg, "opus-24k", duration, sd, forced_rho=rho).run()
            a.append(r1.mos); ca.append(r1.redundancy_pct)
            b.append(r2.mos); cb.append(r2.redundancy_pct)
        sw.append(float(np.mean(a))); un.append(float(np.mean(b)))
        cost_s.append(float(np.mean(ca))); cost_u.append(float(np.mean(cb)))
        print(f"    rho {rho:.3f}  swara {sw[-1]:.3f} @ {cost_s[-1]:.1f}%   "
              f"uniform {un[-1]:.3f} @ {cost_u[-1]:.1f}%")
    RESULTS["overhead"] = {"rho": rhos, "swara_mos": sw, "uniform_mos": un,
                           "swara_cost": cost_s, "uniform_cost": cost_u}

    fig, ax = plt.subplots(figsize=(6.4, 4.3))
    ax.plot(cost_u, un, color=C_UNI, marker="s", ms=6, lw=2.2, label="Uniform adaptive FEC")
    ax.plot(cost_s, sw, color=C_SWARA, marker="D", ms=6, lw=2.4, label="SWARA PI-AFEC")
    target = un[-1]
    ok = [c for c, m in zip(cost_s, sw) if m >= target]
    if ok and cost_u[-1] > 0:
        need = min(ok)
        ax.axhline(target, color=C_ACCENT, ls="--", lw=1.2, alpha=0.8)
        ax.annotate(f"SWARA reaches the same quality\nfor {need:.0f}% overhead instead of {cost_u[-1]:.0f}%",
                    xy=(need, target), xytext=(need + 6, target - 0.34), fontsize=8.4,
                    color=C_ACCENT, arrowprops=dict(arrowstyle="->", color=C_ACCENT, lw=1.2))
        RESULTS["overhead"]["equal_quality_saving_pct"] = float(cost_u[-1] - need)
    ax.set_xlabel("Bandwidth overhead actually spent (%)")
    ax.set_ylabel("Predicted MOS")
    ax.set_title("Quality returned per byte of redundancy\n(12% loss, mean burst 4)")
    ax.legend(fontsize=8.6, loc="lower right")
    fig.tight_layout()
    save(fig, "fig3_overhead_efficiency.png")


# ===========================================================================
def exp4_pi_distribution():
    """PI score separation by speech class + the budget reallocation story."""
    print("[4] PI distribution")
    buckets = defaultdict(list)
    onsets = []
    for sd in (11, 23, 44, 57, 68, 79):
        g = SpeechGenerator(seed=sd)
        frames, _ = g.frames(14.0)
        an = PerceptualImportanceAnalyzer()
        res = []
        for _, seg, kind in frames:
            f = an.analyze(seg)
            res.append((kind, f.pi)); buckets[kind].append(f.pi)
        ks = [k for k, _ in res]
        for i in range(2, len(ks)):
            if ks[i] != "silence" and ks[i-1] == "silence" and ks[i-2] == "silence":
                onsets.append(res[i][1])
    order = ["silence", "plosive", "unvoiced", "voiced"]
    stats = {k: {"n": len(buckets[k]), "mean": float(np.mean(buckets[k])),
                 "p10": float(np.percentile(buckets[k], 10)),
                 "p90": float(np.percentile(buckets[k], 90))} for k in order}
    stats["onset"] = {"n": len(onsets), "mean": float(np.mean(onsets)),
                      "p10": float(np.percentile(onsets, 10)),
                      "p90": float(np.percentile(onsets, 90))}
    tot_frames = sum(len(buckets[k]) for k in order)
    tot_pi = sum(float(np.sum(buckets[k])) for k in order)
    sil_frac = len(buckets["silence"]) / tot_frames
    sil_pi = float(np.sum(buckets["silence"])) / tot_pi
    stats["silence_frame_share"] = sil_frac
    stats["silence_pi_share"] = sil_pi
    RESULTS["pi_distribution"] = stats
    print(f"    silence = {sil_frac*100:.1f}% of frames but {sil_pi*100:.1f}% of importance mass")

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.1),
                                  gridspec_kw={"width_ratios": [1.5, 1]})
    labels = ["Silence", "Plosive", "Unvoiced\nfricative", "Voiced\nvowel", "Talkspurt\nonset"]
    data = [buckets["silence"], buckets["plosive"], buckets["unvoiced"],
            buckets["voiced"], onsets]
    cols = ["#B8B8B8", "#8AB0AB", "#E0A93F", C_SWARA, C_ACCENT]
    parts = ax.violinplot(data, showmeans=True, widths=0.75)
    for pc, c in zip(parts["bodies"], cols):
        pc.set_facecolor(c); pc.set_alpha(0.75); pc.set_edgecolor("white")
    for key in ("cmeans", "cmins", "cmaxes", "cbars"):
        parts[key].set_color(C_DARK); parts[key].set_linewidth(1.1)
    ax.set_xticks(range(1, 6)); ax.set_xticklabels(labels, fontsize=8.6)
    ax.set_ylabel("Perceptual Importance score (0–255)")
    ax.set_title("PI separates what matters from what does not")
    ax.set_ylim(-8, 268)

    ax2.bar(["Share of\nframes", "Share of\nimportance"], [sil_frac*100, sil_pi*100],
            color=["#B8B8B8", C_SWARA], width=0.55)
    ax2.set_ylabel("Silence frames (%)")
    ax2.set_title("The budget PI-AFEC reclaims")
    for i, v in enumerate([sil_frac*100, sil_pi*100]):
        ax2.text(i, v + 1.0, f"{v:.1f}%", ha="center", fontweight="bold", fontsize=10)
    ax2.set_ylim(0, max(sil_frac*100, sil_pi*100) * 1.28)
    fig.tight_layout()
    save(fig, "fig4_pi_distribution.png")


# ===========================================================================
def exp5_jitter_buffer():
    """The R-factor optimum: why 'minimise loss' is the wrong objective."""
    print("[5] jitter buffer optimum")
    cfg = ChannelConfig(loss_rate=0.06, mean_burst=3.0, base_delay_ms=45.0,
                        jitter_ms=22.0, jitter_tail=0.7, capacity_kbps=800)
    sim = CallSimulator("swara", cfg, "opus-24k", 25.0, 9)
    res = sim.run()
    curve = res.jb_curve
    RESULTS["jitter_buffer"] = {"curve": curve, "chosen_ms": res.playout_delay_ms}

    depths = [c[0] for c in curve]
    rs = [c[1] for c in curve]
    # Measured MOS at forced depths, to confirm the model's optimum is real.
    meas_d = [20, 40, 60, 80, 100, 120, 150, 180]
    meas = []
    for d in meas_d:
        vals = [CallSimulator("swara", cfg, "opus-24k", 22.0, sd, forced_depth=d).run().mos
                for sd in (9, 21, 33)]
        meas.append(float(np.mean(vals)))
    RESULTS["jitter_buffer"]["measured_depths"] = meas_d
    RESULTS["jitter_buffer"]["measured_mos"] = meas
    best_meas = meas_d[int(np.argmax(meas))]
    RESULTS["jitter_buffer"]["measured_optimum_ms"] = best_meas
    print(f"    model optimum {depths[int(np.argmax(rs))]:.0f} ms, "
          f"measured optimum {best_meas} ms, chosen {res.playout_delay_ms:.0f} ms")

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.1))
    ax.plot(depths, rs, color=C_SWARA, lw=2.4, marker="o", ms=4.5)
    bi = int(np.argmax(rs))
    ax.scatter([depths[bi]], [rs[bi]], s=150, color=C_ACCENT, zorder=5, marker="*")
    ax.annotate(f"R-factor optimum\n{depths[bi]:.0f} ms", xy=(depths[bi], rs[bi]),
                xytext=(depths[bi] + 22, rs[bi] - 5.0), fontsize=8.6, color=C_ACCENT,
                arrowprops=dict(arrowstyle="->", color=C_ACCENT))
    ax.set_xlabel("Jitter buffer depth (ms)")
    ax.set_ylabel("Predicted R-factor")
    ax.set_title("Too shallow loses packets, too deep loses the conversation")
    ax.text(0.03, 0.06, "left of the peak: late-packet loss\nright of the peak: delay impairment",
            transform=ax.transAxes, fontsize=8, color="#555")

    ax2.plot(meas_d, meas, color=C_DARK, lw=2.2, marker="s", ms=5)
    mi = int(np.argmax(meas))
    ax2.scatter([meas_d[mi]], [meas[mi]], s=150, color=C_ACCENT, zorder=5, marker="*")
    ax2.axvline(res.playout_delay_ms, color=C_SWARA, ls="--", lw=1.6)
    ax2.text(res.playout_delay_ms + 3, min(meas) + 0.02,
             f"SWARA chose\n{res.playout_delay_ms:.0f} ms", fontsize=8.4, color=C_SWARA)
    ax2.set_xlabel("Forced jitter buffer depth (ms)")
    ax2.set_ylabel("Measured MOS (end-to-end simulation)")
    ax2.set_title("The optimiser's choice, validated end to end")
    fig.tight_layout()
    save(fig, "fig5_jitter_buffer.png")


# ===========================================================================
def exp6_handshake():
    """Handshake cost and security properties, measured."""
    print("[6] handshake")
    import hashlib
    from swara.auth import (MediaCipher, SparkClient, SparkServer, register)

    srv = SparkServer()
    rec, _ = register("ansh", "S3cure!Pass#2026")
    srv.add_user(rec)
    addr = ("10.7.1.9", 51234)

    t0 = time.perf_counter()
    cli = SparkClient("ansh", "S3cure!Pass#2026")
    hello = cli.make_hello()
    retry = srv.handle_hello(addr, hello)
    init = cli.make_auth_init(retry)
    resp, pending = srv.handle_auth_init(addr, init)
    tag = cli.make_auth_confirm(resp)
    keys, ticket = srv.handle_auth_confirm(pending, tag)
    full_ms = (time.perf_counter() - t0) * 1000
    cli.ticket = ticket

    t0 = time.perf_counter()
    for _ in range(50):
        srv.handle_path_migrate(("192.168.4.4", 40001), cli.make_path_migrate())
    mig_ms = (time.perf_counter() - t0) / 50 * 1000

    total_bytes = sum(len(x) for x in (hello, retry, init, resp, tag, ticket))
    mig_bytes = len(cli.make_path_migrate())

    # Per-packet crypto throughput
    mc = MediaCipher(keys.k_c2s, b"c2s")
    pt = os.urandom(80); hdr = os.urandom(16)
    t0 = time.perf_counter()
    for i in range(20000):
        mc.seal(i, hdr, pt)
    seal_us = (time.perf_counter() - t0) / 20000 * 1e6

    data = {
        "spark_rtts": 2, "spark_resume_rtts": 0,
        "spark_bytes": total_bytes, "spark_cpu_ms": full_ms,
        "migration_bytes": mig_bytes, "migration_cpu_ms": mig_ms,
        "hello_bytes": len(hello), "retry_bytes": len(retry),
        "amplification_factor": len(retry) / len(hello),
        "db_record_bytes": rec.size_bytes(),
        "seal_us_per_packet": seal_us,
        "seal_pct_of_frame_budget": seal_us / 20000 * 100,
        "dtls_srtp_rtts": 3, "dtls_srtp_bytes": 1900,   # published typical figures
        "plain_rtts": 0, "plain_bytes": 0,
    }
    RESULTS["handshake"] = data
    print(f"    SPARK: 2 RTT, {total_bytes} B, {full_ms:.1f} ms CPU; "
          f"resume 0 RTT, {mig_bytes} B, {mig_ms:.3f} ms")
    print(f"    amplification factor {data['amplification_factor']:.2f}x; "
          f"AEAD {seal_us:.1f} us/packet ({data['seal_pct_of_frame_budget']:.2f}% of budget)")

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.0))
    names = ["Plain UDP\n(no security)", "DTLS-SRTP\n(WebRTC)", "SPARK\nfirst call", "SPARK\nresume"]
    rtts = [0, 3, 2, 0]
    byts = [0, 1900, total_bytes, mig_bytes]
    cols = [C_BASE, C_UNI, C_SWARA, "#0A5C5B"]
    b1 = ax.bar(names, rtts, color=cols, width=0.6)
    ax.set_ylabel("Round trips before media flows")
    ax.set_title("Call setup cost")
    for r, v in zip(b1, rtts):
        ax.text(r.get_x() + r.get_width()/2, v + 0.07, str(v), ha="center", fontweight="bold")
    ax.set_ylim(0, 3.6)
    ax.text(0.02, 0.9, "no authentication,\nno encryption", transform=ax.transAxes,
            fontsize=7.6, color=C_ACCENT)

    b2 = ax2.bar(names, byts, color=cols, width=0.6)
    ax2.set_ylabel("Handshake bytes on the wire")
    ax2.set_title("Setup bandwidth")
    for r, v in zip(b2, byts):
        ax2.text(r.get_x() + r.get_width()/2, v + 45, str(v), ha="center",
                 fontweight="bold", fontsize=9)
    ax2.set_ylim(0, 2200)
    fig.tight_layout()
    save(fig, "fig6_handshake.png")


# ===========================================================================
def exp7_overlay():
    """SETU: how often a relay beats the direct path."""
    print("[7] SETU overlay")
    from swara.overlay import LinkMeasurement, SetuOverlay

    rng = np.random.default_rng(4)
    trials, improved, gains, hop_hist = 300, 0, [], defaultdict(int)
    for _ in range(trials):
        ov = SetuOverlay()
        ids = [f"P{i}" for i in range(6)]
        for i in ids:
            ov.add_node(i)
        for i, a in enumerate(ids):
            for b in ids[i+1:]:
                # Most links are fine; a minority traverse a congested exchange.
                if rng.random() < 0.28:
                    m = LinkMeasurement(rng.uniform(70, 190), rng.uniform(20, 60),
                                        rng.uniform(0.04, 0.18))
                else:
                    m = LinkMeasurement(rng.uniform(8, 55), rng.uniform(1, 12),
                                        rng.uniform(0.0, 0.02))
                ov.set_link(a, b, m)
        ov.converge()
        for rep in ov.improvement_report():
            hop_hist[rep["hops"]] += 1
            if rep["gain_pct"] > 1.0:
                improved += 1
                gains.append(rep["gain_pct"])
            trials_pairs = 1
    total_pairs = sum(hop_hist.values())
    data = {"pairs": total_pairs,
            "improved_pct": 100.0 * improved / total_pairs,
            "mean_gain_pct": float(np.mean(gains)) if gains else 0.0,
            "median_gain_pct": float(np.median(gains)) if gains else 0.0,
            "hop_histogram": dict(hop_hist),
            "convergence_rounds": ov.rounds_to_converge}
    RESULTS["overlay"] = data
    print(f"    {data['improved_pct']:.1f}% of pairs improved by relaying; "
          f"mean gain {data['mean_gain_pct']:.1f}%; converged in {ov.rounds_to_converge} rounds")

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.0))
    ax.hist(gains, bins=26, color=C_SWARA, alpha=0.88)
    ax.axvline(np.mean(gains), color=C_ACCENT, ls="--", lw=1.8,
               label=f"mean {np.mean(gains):.1f}%")
    ax.set_xlabel("Path cost reduction vs the direct route (%)")
    ax.set_ylabel("Number of peer pairs")
    ax.set_title("When the triangle inequality fails,\nSETU finds the better path")
    ax.legend(fontsize=8.6)

    ks = sorted(hop_hist)
    vals = [100.0 * hop_hist[k] / total_pairs for k in ks]
    ax2.bar([f"{k} hop{'s' if k != 1 else ''}" for k in ks], vals,
            color=[C_BASE if k == 1 else C_SWARA for k in ks], width=0.55)
    ax2.set_ylabel("Share of peer pairs (%)")
    ax2.set_title("Route chosen by SETU")
    for i, v in enumerate(vals):
        ax2.text(i, v + 1.2, f"{v:.0f}%", ha="center", fontweight="bold")
    ax2.set_ylim(0, max(vals) * 1.25)
    fig.tight_layout()
    save(fig, "fig7_overlay.png")


# ===========================================================================
def exp8_profiles(seeds=(5, 17, 29, 41), duration=22.0):
    """Realistic named profiles -- the table that goes in the paper."""
    print("[8] realistic profiles")
    rows = []
    names = ["good_wifi", "busy_wifi", "congested_4g", "hostel_wifi", "poor_3g", "satellite"]
    for nm in names:
        acc = defaultdict(list)
        for sd in seeds:
            for k, v in compare(PROFILES[nm], duration_s=duration, seed=sd).items():
                acc[k].append(v)
        row = {"profile": nm, "label": PROFILES[nm].label()}
        for s in ("baseline", "uniform_fec", "swara"):
            row[s] = {
                "mos": float(np.mean([r.mos for r in acc[s]])),
                "r": float(np.mean([r.r_factor for r in acc[s]])),
                "resid": float(np.mean([r.residual_loss_pct for r in acc[s]])),
                "perc": float(np.mean([r.perceptual_loss_pct for r in acc[s]])),
                "delay": float(np.mean([r.mouth_to_ear_ms for r in acc[s]])),
                "redund": float(np.mean([r.redundancy_pct for r in acc[s]])),
            }
        rows.append(row)
        print(f"    {nm:<14} base {row['baseline']['mos']:.2f} | "
              f"uni {row['uniform_fec']['mos']:.2f} | swara {row['swara']['mos']:.2f}")
    RESULTS["profiles"] = rows

    fig, ax = plt.subplots(figsize=(9.4, 4.4))
    idx = np.arange(len(names))
    w = 0.26
    pretty = ["Good\nWi-Fi", "Busy\nWi-Fi", "Congested\n4G", "Hostel\nWi-Fi",
              "Poor\n3G", "Satellite"]
    for off, s, c, lab in [(-w, "baseline", C_BASE, "Baseline"),
                           (0, "uniform_fec", C_UNI, "Uniform FEC (equal budget)"),
                           (w, "swara", C_SWARA, "SWARA")]:
        v = [r[s]["mos"] for r in rows]
        bars = ax.bar(idx + off, v, w, label=lab, color=c)
        for b, val in zip(bars, v):
            ax.text(b.get_x() + b.get_width()/2, val + 0.04, f"{val:.2f}",
                    ha="center", fontsize=7.2)
    ax.set_xticks(idx); ax.set_xticklabels(pretty, fontsize=8.8)
    ax.set_ylabel("Predicted MOS")
    ax.set_title("Measured call quality across realistic network profiles")
    ax.axhline(3.6, color="#2E7D32", ls="--", lw=1, alpha=0.6)
    ax.legend(fontsize=8.6, ncol=3, loc="upper right")
    ax.set_ylim(0, 5.0)
    fig.tight_layout()
    save(fig, "fig8_profiles.png")


# ===========================================================================
if __name__ == "__main__":
    t0 = time.time()
    exp4_pi_distribution()
    exp6_handshake()
    exp7_overlay()
    exp1_loss_sweep()
    exp2_burst_sweep()
    exp3_overhead_efficiency()
    exp5_jitter_buffer()
    exp8_profiles()

    out = os.path.join(os.path.dirname(FIG), "experiments", "results.json")
    with open(out, "w") as f:
        json.dump(RESULTS, f, indent=2)
    print(f"\nAll experiments complete in {time.time()-t0:.0f}s -> {out}")
