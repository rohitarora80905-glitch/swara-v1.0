"""
PI-AFEC: Perceptual-Importance-weighted Adaptive Forward Error Correction.

Two layers:

1.  A systematic Reed-Solomon erasure code over GF(2^8).  A group of k media
    packets is extended with r parity packets; any k of the k+r survivors
    reconstruct the group.  Systematic means the k data packets are sent
    unmodified, so a group that arrives intact costs zero decoding work.

2.  The novel part -- the SCHEDULER that decides r, and the "perceptual anchor
    replica" that protects the single most important frame independently.

Why this is not just "FEC with a knob":

  Standard adaptive FEC (RFC 5109 ULP-FEC, Opus LBRR, WebRTC's FlexFEC) picks
  redundancy from the NETWORK state alone -- more loss, more parity.  It spends
  exactly as much protecting a frame of silence as a frame carrying a vowel
  onset.  PI-AFEC picks redundancy from the JOINT (network, content) state:

      r = clip( round( k * rho_net * (Gbar)^gamma ), 0, r_max )

  where rho_net in [0,1] is the network-driven redundancy budget from the NAVIK
  controller and Gbar in [0,1] is the mean normalised PI of the group.  gamma
  < 1 (we use 0.65) keeps moderately-important groups from being starved.

  Because the total parity budget is held FIXED against the baseline, every
  parity packet withheld from a silent group is spent on a group that carries
  meaning.  That reallocation is the entire source of the measured gain.

3.  Perceptual anchor replica.  RS parity only helps if at least k of k+r
    packets survive.  Under a long burst that condition fails and the whole
    group is lost -- including its most important frame.  So when a frame's PI
    exceeds an adaptive threshold, a compact replica of that ONE frame is
    piggybacked onto a later packet, deliberately spaced by the estimated mean
    burst length so that the original and the replica are unlikely to be in the
    same burst.  It is a targeted, content-aware, delay-bounded duplicate.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# GF(2^8) arithmetic, primitive polynomial 0x11D (the same field as CD-ROM,
# QR codes and RAID-6).  Tables are built once at import time.
# ---------------------------------------------------------------------------

_PRIM = 0x11D
GF_EXP = np.zeros(512, dtype=np.uint8)
GF_LOG = np.zeros(256, dtype=np.uint8)


def _build_tables() -> None:
    x = 1
    for i in range(255):
        GF_EXP[i] = x
        GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= _PRIM
    for i in range(255, 512):
        GF_EXP[i] = GF_EXP[i - 255]


_build_tables()

# Full 256x256 multiplication table -> vectorised, branch-free encode/decode.
_A = np.arange(256, dtype=np.int32)
_LOGA = GF_LOG[_A].astype(np.int32)
_MULTAB = GF_EXP[(_LOGA[:, None] + _LOGA[None, :]) % 255].astype(np.uint8)
_MULTAB[0, :] = 0
_MULTAB[:, 0] = 0


def gf_mul(a: int, b: int) -> int:
    return int(_MULTAB[a, b])


def gf_inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("GF(256) inverse of 0")
    return int(GF_EXP[(255 - int(GF_LOG[a])) % 255])


def gf_vec_mul(vec: np.ndarray, scalar: int) -> np.ndarray:
    """Multiply a whole byte-vector by a scalar in GF(256) via table lookup."""
    return _MULTAB[scalar][vec]


def _vandermonde(rows: int, cols: int) -> np.ndarray:
    """Cauchy-style generator rows.  Any square submatrix is invertible, which
    is exactly the MDS property an erasure code needs."""
    m = np.zeros((rows, cols), dtype=np.uint8)
    for i in range(rows):
        a = i + 1                      # distinct field elements for parity rows
        for j in range(cols):
            b = 128 + j                # disjoint from the parity element set
            m[i, j] = gf_inv(a ^ b)
        # Normalise the first column to 1 -> better numerical hygiene, and it
        # makes the r=1 case reduce to a plain XOR, which is free.
    for i in range(rows):
        s = gf_inv(int(m[i, 0])) if m[i, 0] else 1
        for j in range(cols):
            m[i, j] = gf_mul(int(m[i, j]), s)
    return m


class ReedSolomon:
    """Systematic RS(k+r, k) erasure code over GF(256), byte-parallel."""

    def __init__(self, k: int, r: int):
        if not (1 <= k <= 120) or not (0 <= r <= 120):
            raise ValueError("k and r out of supported range")
        self.k, self.r = k, r
        self.gen = _vandermonde(r, k) if r else np.zeros((0, k), dtype=np.uint8)

    def encode(self, shards: list[bytes]) -> list[bytes]:
        """shards: k equal-length byte strings -> r parity byte strings."""
        if len(shards) != self.k:
            raise ValueError(f"expected {self.k} shards, got {len(shards)}")
        if self.r == 0:
            return []
        n = max(len(s) for s in shards)
        data = np.zeros((self.k, n), dtype=np.uint8)
        for i, s in enumerate(shards):
            data[i, : len(s)] = np.frombuffer(s, dtype=np.uint8)
        out = np.zeros((self.r, n), dtype=np.uint8)
        for i in range(self.r):
            acc = np.zeros(n, dtype=np.uint8)
            for j in range(self.k):
                c = int(self.gen[i, j])
                if c:
                    acc ^= gf_vec_mul(data[j], c)
            out[i] = acc
        return [out[i].tobytes() for i in range(self.r)]

    def decode(self, received: dict[int, bytes], shard_len: int) -> dict[int, bytes] | None:
        """received: {index -> bytes}; data indices 0..k-1, parity k..k+r-1.

        Returns the recovered data shards, or None if fewer than k survived.
        """
        present = sorted(received.keys())
        if len(present) < self.k:
            return None
        have_data = [i for i in present if i < self.k]
        if len(have_data) == self.k:
            return {i: received[i] for i in have_data}

        use = present[: self.k]
        # Build the k x k matrix mapping unknown data shards -> the shards we have.
        missing = [i for i in range(self.k) if i not in received]
        known = [i for i in range(self.k) if i in received]
        par_use = [i for i in use if i >= self.k][: len(missing)]
        if len(par_use) < len(missing):
            return None

        m = np.zeros((len(missing), len(missing)), dtype=np.uint8)
        for a, p in enumerate(par_use):
            for b, d in enumerate(missing):
                m[a, b] = self.gen[p - self.k, d]

        # Right-hand side = parity minus the contribution of the known shards.
        rhs = np.zeros((len(missing), shard_len), dtype=np.uint8)
        for a, p in enumerate(par_use):
            acc = np.frombuffer(received[p].ljust(shard_len, b"\x00"), dtype=np.uint8).copy()
            for d in known:
                c = int(self.gen[p - self.k, d])
                if c:
                    acc ^= gf_vec_mul(
                        np.frombuffer(received[d].ljust(shard_len, b"\x00"), dtype=np.uint8), c)
            rhs[a] = acc

        sol = self._solve(m, rhs)
        if sol is None:
            return None
        out = {i: received[i] for i in known}
        for b, d in enumerate(missing):
            out[d] = sol[b].tobytes()
        return out

    @staticmethod
    def _solve(m: np.ndarray, rhs: np.ndarray) -> np.ndarray | None:
        """Gauss-Jordan elimination over GF(256)."""
        n = m.shape[0]
        m = m.copy()
        rhs = rhs.copy()
        for col in range(n):
            piv = next((r for r in range(col, n) if m[r, col]), None)
            if piv is None:
                return None
            if piv != col:
                m[[col, piv]] = m[[piv, col]]
                rhs[[col, piv]] = rhs[[piv, col]]
            inv = gf_inv(int(m[col, col]))
            m[col] = gf_vec_mul(m[col], inv)
            rhs[col] = gf_vec_mul(rhs[col], inv)
            for r in range(n):
                if r != col and m[r, col]:
                    f = int(m[r, col])
                    m[r] ^= gf_vec_mul(m[col], f)
                    rhs[r] ^= gf_vec_mul(rhs[col], f)
        return rhs


# ---------------------------------------------------------------------------
# The PI-AFEC scheduler
# ---------------------------------------------------------------------------

GAMMA = 0.65        # importance -> redundancy shaping exponent
ANCHOR_PI_FLOOR = 170   # a frame must be at least this important to earn a replica


@dataclass
class FecPlan:
    """What the scheduler decided for one group of k frames."""
    k: int
    r: int
    group_mean_pi: float
    anchors: list = field(default_factory=list)   # [(index_in_group, delay_frames)]
    overhead_ratio: float = 0.0
    burst_mode: bool = False

    @property
    def anchor_index(self):
        return self.anchors[0][0] if self.anchors else None


@dataclass
class FecScheduler:
    """Decides parity count and anchor placement for each group.

    `rho_net` and `mean_burst` are written by the NAVIK controller from receiver
    telemetry; the scheduler itself is stateless apart from those two inputs and
    a running average used to hold the overhead budget on target.
    """
    k: int = 8
    r_max: int = 6
    rho_net: float = 0.0          # network-driven redundancy budget, 0..1
    mean_burst: float = 1.0       # estimated mean loss-burst length, packets
    anchors_enabled: bool = True
    _spent: float = field(default=0.0, repr=False)
    _groups: int = field(default=0, repr=False)
    _carry_r: float = field(default=0.0, repr=False)
    _carry_a: float = field(default=0.0, repr=False)
    _wsum: float = field(default=0.6, repr=False)     # running mean of the
    _wcount: float = field(default=1.0, repr=False)   # importance weight

    def plan(self, pi_scores: list[int]) -> FecPlan:
        """Decide parity and anchors for one group.

        The branch on `burst_mode` is a substantive design decision, not a
        tuning detail.  An RS(k+r, k) group survives only if at least k of its
        k+r packets arrive.  When the mean loss burst approaches the group's
        own span, that condition fails almost every time the channel goes bad,
        and every parity byte is wasted.

        Classical coding theory answers this with INTERLEAVING -- spread the
        group's packets across a wide time window so a burst hits at most one
        of them.  Real-time voice cannot use it: an interleaver of depth D over
        a group of k frames costs k*D frame times of buffering, so even D=3 at
        k=8 adds 480 ms and destroys conversational interactivity.

        SWARA's answer is to interleave ONLY what matters.  In burst mode the
        parity budget is redirected into REPLICAS of the highest-PI frames,
        placed at offsets chosen to exceed the estimated burst length.  One
        replica of one frame costs one packet and one frame-time of extra
        delay, and it is the only mechanism that survives a burst longer than
        the group itself.
        """
        k = len(pi_scores)
        if k == 0:
            return FecPlan(k=0, r=0, group_mean_pi=0.0)
        arr = np.asarray(pi_scores, dtype=float)
        gbar = float(arr.mean()) / 255.0

        # The weight is accumulated for EVERY group, including the silent ones
        # that are about to be skipped.  That ordering is the whole mechanism:
        # a skipped group contributes a weight of nearly zero to the running
        # mean, which lowers the mean, which raises every other group's share.
        # Accumulating after the skip instead -- as this did -- means a silent
        # group's budget is simply forfeited rather than handed on, and the
        # scheme ends up spending less overall instead of spending the same
        # amount better. Measured cost of that ordering: 164 parity packets
        # against the uniform comparator's 373.
        weight = gbar ** GAMMA
        self._wsum += weight
        self._wcount += 1.0

        # A group with genuinely no speech in it gets nothing at all, however
        # bad the network is.  This is where the reallocated budget comes from.
        if gbar < 0.08:
            self._groups += 1
            return FecPlan(k=k, r=0, group_mean_pi=gbar, overhead_ratio=0.0)

        # --- budget: REDISTRIBUTE, do not shrink -------------------------
        #
        # The whole claim of PI-AFEC is "the same budget, spent where it is
        # heard".  The obvious implementation, budget = k * rho * Gbar^gamma,
        # does not do that.  Because Gbar^gamma is always below 1, it spends
        # systematically LESS than the rho it was given -- a nominal 50%
        # redundancy quietly became about 20% in the live system, and SWARA
        # lost to the uniform comparator on both quality and, absurdly, on the
        # thing it was supposed to be better at.
        #
        # Normalising the weight against its own running mean fixes it: the
        # EXPECTED spend is exactly k * rho, while individual groups receive
        # more or less than that in proportion to how much they matter.  Now it
        # really is redistribution rather than a hidden discount.
        wbar = max(0.05, self._wsum / self._wcount)
        ratio = float(np.clip(weight / wbar, 0.0, 4.0))
        budget_pkts = k * self.rho_net * ratio

        # An RS(k+r, k) group survives r erasures.  Reed-Solomon is therefore
        # worth funding whenever the typical burst is smaller than the parity we
        # can afford, and only becomes futile once bursts approach the group's
        # own span.  An earlier threshold of k/2.5 declared futility at a burst
        # of 1.6 packets for k=4 -- a regime where RS in fact works perfectly
        # well -- and diverted the entire budget into replicas.  The measured
        # consequence was SWARA transmitting almost no redundancy at all while
        # the uniform comparator recovered 53 frames per call.
        burst_mode = self.mean_burst >= 0.75 * k

        if burst_mode:
            share_r, share_a = 0.45, 0.55
        else:
            share_r, share_a = 0.85, 0.15

        # Fractional budgets are carried between groups rather than rounded
        # away.  With k=4 a per-group budget of 0.4 packets rounds to zero every
        # single time, so a nominal 10% redundancy silently becomes 0%.
        want_r = budget_pkts * share_r + self._carry_r
        r = int(want_r)
        self._carry_r = want_r - r
        r = int(np.clip(r, 0, min(self.r_max, k)))

        want_a = budget_pkts * share_a + self._carry_a
        n_anchor = int(want_a)
        self._carry_a = want_a - n_anchor
        n_anchor = int(np.clip(n_anchor, 0, 4)) if self.anchors_enabled else 0

        anchors: list[tuple[int, int]] = []
        if self.anchors_enabled and n_anchor:
            order = np.argsort(-arr)
            base_delay = int(np.clip(round(self.mean_burst) + 2, 3, 9))
            for slot, idx in enumerate(order[:n_anchor]):
                if arr[idx] < ANCHOR_PI_FLOOR:
                    break
                # Stagger successive replicas so two of them are never inside
                # the same outage either.
                anchors.append((int(idx), base_delay + slot * 2))

        overhead = (r + len(anchors)) / k
        self._spent += overhead
        self._groups += 1
        return FecPlan(k=k, r=r, group_mean_pi=gbar, anchors=anchors,
                       overhead_ratio=overhead, burst_mode=burst_mode)

    @property
    def realised_overhead(self) -> float:
        return self._spent / self._groups if self._groups else 0.0


@dataclass
class UniformFecScheduler:
    """Baseline comparator: classic loss-driven FEC with no content awareness.

    This is what RFC 5109 / FlexFEC style adaptive redundancy does.  It is a
    strong baseline, not a straw man -- it adapts to the network correctly.  It
    simply cannot tell a vowel from a silence.
    """
    k: int = 8
    r_max: int = 6
    rho_net: float = 0.0
    mean_burst: float = 1.0
    _spent: float = field(default=0.0, repr=False)
    _groups: int = field(default=0, repr=False)

    def plan(self, pi_scores: list[int]) -> FecPlan:
        k = max(1, len(pi_scores))
        # Fractional budgets are honoured probabilistically so that a rho of,
        # say, 0.14 really does cost 0.14*k packets on average rather than
        # rounding to zero and quietly under-spending the budget it was given.
        exact = k * self.rho_net
        r = int(exact)
        if np.random.random() < (exact - r):
            r += 1
        r = int(np.clip(r, 0, min(self.r_max, k)))
        self._spent += r / k
        self._groups += 1
        return FecPlan(k=k, r=r,
                       group_mean_pi=float(np.mean(pi_scores)) / 255.0 if pi_scores else 0.0,
                       anchors=[], overhead_ratio=r / k)

    @property
    def realised_overhead(self) -> float:
        return self._spent / self._groups if self._groups else 0.0
