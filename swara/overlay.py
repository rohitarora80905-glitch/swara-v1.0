"""
SETU -- Self-Evaluating Topology Underlay.

("Setu" = bridge.)  An application-layer overlay that chooses relay paths for
group calls, and doubles as the NAT-traversal fallback.

Why an overlay at all?  Because the Internet's routing does not optimise for
what a voice call needs.  BGP picks paths on policy and AS-hop count, not on
latency, and certainly not on jitter or loss.  The consequence is well
documented and easy to reproduce: the triangle inequality is frequently
VIOLATED at the application layer.  The direct path A->B can be measurably
worse than the two-hop path A->C->B, because the direct route traverses a
congested peering point that the detour avoids.

SETU runs a distance-vector computation (Bellman-Ford with split horizon and
poisoned reverse -- the same algorithm family as RIP, which the syllabus
covers) over a composite link cost that expresses everything voice cares about
in one number:

    cost(u,v) = alpha * owd_ms  +  beta * jitter_ms  +  gamma * loss_penalty

    loss_penalty = -10 * log10(1 - p)    "loss as delay-equivalent"

The loss term is the interesting part.  Expressing loss in decibels of survival
probability makes losses ADD along a path exactly as delays do, so a path's
total cost is the plain sum of its links' costs and Bellman-Ford is valid.  A
1% link and a 2% link compose to 2.97% end to end, and -10log10(0.99) +
-10log10(0.98) = -10log10(0.9702).  The arithmetic is exact, not an
approximation.

Count-to-infinity, the classic distance-vector failure, is handled by poisoned
reverse plus a hard cost ceiling (INFINITY), and because the overlay has tens
of nodes rather than thousands, convergence is sub-second.
"""

from __future__ import annotations

import math
import numpy as np
from dataclasses import dataclass, field

ALPHA = 1.0      # weight on one-way delay (ms)
BETA = 2.0       # weight on jitter -- jitter hurts more than raw delay
GAMMA = 45.0     # weight on the dB loss penalty
INFINITY = 1e9
RELAY_PENALTY = 12.0   # ms-equivalent cost of an extra relay hop (CPU + queueing)


def link_cost(owd_ms: float, jitter_ms: float, loss: float) -> float:
    p = float(np.clip(loss, 0.0, 0.95))
    loss_db = -10.0 * math.log10(max(1e-6, 1.0 - p))
    return ALPHA * owd_ms + BETA * jitter_ms + GAMMA * loss_db


@dataclass
class LinkMeasurement:
    owd_ms: float
    jitter_ms: float
    loss: float

    @property
    def cost(self) -> float:
        return link_cost(self.owd_ms, self.jitter_ms, self.loss)


@dataclass
class OverlayNode:
    node_id: str
    links: dict = field(default_factory=dict)          # neighbour -> LinkMeasurement
    dv: dict = field(default_factory=dict)             # dest -> (cost, next_hop)
    neighbour_dv: dict = field(default_factory=dict)   # neighbour -> their table

    def local_table(self) -> dict:
        t = {self.node_id: (0.0, self.node_id)}
        for n, m in self.links.items():
            t[n] = (m.cost, n)
        return t

    def advertise(self, to: str) -> dict:
        """Split horizon with poisoned reverse: routes learned FROM a neighbour
        are advertised back TO that neighbour at infinite cost, which prevents
        the two of them from forming a mutual-belief routing loop."""
        out = {}
        for dest, (cost, nh) in self.dv.items():
            out[dest] = INFINITY if nh == to and dest != to else cost
        return out

    def recompute(self) -> bool:
        """One Bellman-Ford relaxation.  Returns True if the table changed."""
        new = self.local_table()
        for nb, table in self.neighbour_dv.items():
            if nb not in self.links:
                continue
            base = self.links[nb].cost
            for dest, cost in table.items():
                if dest == self.node_id or cost >= INFINITY:
                    continue
                total = base + cost + RELAY_PENALTY
                if dest not in new or total < new[dest][0]:
                    new[dest] = (total, nb)
        changed = new != self.dv
        self.dv = new
        return changed


class SetuOverlay:
    def __init__(self) -> None:
        self.nodes: dict[str, OverlayNode] = {}
        self.rounds_to_converge = 0

    def add_node(self, node_id: str) -> OverlayNode:
        n = OverlayNode(node_id)
        self.nodes[node_id] = n
        return n

    def set_link(self, a: str, b: str, m: LinkMeasurement) -> None:
        self.nodes[a].links[b] = m
        self.nodes[b].links[a] = m

    def converge(self, max_rounds: int = 40) -> int:
        for n in self.nodes.values():
            n.dv = n.local_table()
        for rnd in range(1, max_rounds + 1):
            for a in self.nodes.values():
                for b in a.links:
                    self.nodes[b].neighbour_dv[a.node_id] = a.advertise(b)
            changed = any(n.recompute() for n in self.nodes.values())
            if not changed:
                self.rounds_to_converge = rnd
                return rnd
        self.rounds_to_converge = max_rounds
        return max_rounds

    def path(self, src: str, dst: str) -> list[str]:
        p, cur, guard = [src], src, 0
        while cur != dst and guard < 32:
            entry = self.nodes[cur].dv.get(dst)
            if entry is None or entry[0] >= INFINITY:
                return []
            cur = entry[1]
            p.append(cur)
            guard += 1
        return p if cur == dst else []

    def direct_cost(self, a: str, b: str) -> float:
        m = self.nodes[a].links.get(b)
        return m.cost if m else INFINITY

    def improvement_report(self) -> list[dict]:
        """For every pair, compare the direct path against SETU's choice."""
        out = []
        ids = list(self.nodes)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                direct = self.direct_cost(a, b)
                chosen = self.nodes[a].dv.get(b, (INFINITY, None))[0]
                p = self.path(a, b)
                out.append({
                    "pair": f"{a}->{b}", "direct_cost": direct,
                    "setu_cost": chosen, "path": p, "hops": max(0, len(p) - 1),
                    "gain_pct": (100.0 * (direct - chosen) / direct
                                 if direct < INFINITY and direct > 0 else 0.0),
                })
        return out
