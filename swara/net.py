"""
Real UDP transport.

This is an actual `socket.socket(AF_INET, SOCK_DGRAM)`.  Datagrams leave the
machine.  Nothing here is simulated.

One optional feature: a local impairment injector.  For the demonstration we
need to be able to make a healthy LAN behave like a congested 4G link on
demand, and the honest place to do that is at the sending socket, where it is
clearly labelled as artificial rather than buried in the protocol.  The
impairment uses the same Gilbert-Elliott model as the offline evaluation, so
the demo and the measurements are describing the same channel.
"""

from __future__ import annotations

import heapq
import random
import socket
import threading
import time


class Impairment:
    """Gilbert-Elliott loss plus added delay, applied to outgoing datagrams.

    The channel state advances on a WALL-CLOCK tick, not once per packet sent.
    That choice is deliberate and it matters twice over.

    Physically it is more truthful: a radio fade or a queue overflow lasts for a
    DURATION.  It does not last for a number of packets.  A sender that
    transmits twice as many packets during a 100 ms fade loses twice as many
    packets, not the same number.

    Methodologically it is what makes an A/B comparison valid.  Stepping the
    chain per packet means a configuration that transmits redundancy walks the
    Markov chain further per unit time and therefore experiences a measurably
    different channel from the configuration it is being compared against.  We
    hit exactly that: three arms configured for 15% loss measured 9.5%, 12.3%
    and 12.5%.  Any quality difference read off those runs would have been
    partly an artefact of the instrument.
    """

    TICK_MS = 20.0        # one frame time

    def __init__(self, seed: int = 1234) -> None:
        self.enabled = False
        self.loss_rate = 0.0
        self.mean_burst = 1.0
        self.extra_delay_ms = 0.0
        self.jitter_ms = 0.0
        self._bad = False
        # Two INDEPENDENT streams.  The Gilbert-Elliott chain must advance
        # only with wall-clock time; if it shared an RNG with the jitter
        # draws, an arm that sends more packets (more jitter draws) would
        # perturb the loss realisation of the very chain it is being
        # measured against, and "identical seed" would no longer mean
        # "identical channel".  That is exactly the instrument artefact the
        # A/B comparison exists to remove.
        self._rng = random.Random(seed)                # loss chain only
        self._rng_jitter = random.Random(seed ^ 0x5A5A5A5A)  # delay only
        self._last_tick = time.monotonic()
        self._recompute()

    def _recompute(self) -> None:
        p = min(max(self.loss_rate, 0.0), 0.95)
        b = max(1.0, self.mean_burst)
        if p <= 0.0:
            self.p_gb, self.p_bg = 0.0, 1.0
        else:
            self.p_bg = 1.0 / b
            self.p_gb = min(1.0, self.p_bg * p / (1.0 - p))

    def configure(self, loss_rate=None, mean_burst=None, extra_delay_ms=None,
                  jitter_ms=None, enabled=None) -> None:
        if loss_rate is not None: self.loss_rate = float(loss_rate)
        if mean_burst is not None: self.mean_burst = float(mean_burst)
        if extra_delay_ms is not None: self.extra_delay_ms = float(extra_delay_ms)
        if jitter_ms is not None: self.jitter_ms = float(jitter_ms)
        if enabled is not None: self.enabled = bool(enabled)
        self._recompute()

    def should_drop(self) -> bool:
        if not self.enabled or self.loss_rate <= 0:
            return False
        now = time.monotonic()
        ticks = int((now - self._last_tick) * 1000.0 / self.TICK_MS)
        if ticks > 0:
            self._last_tick += ticks * self.TICK_MS / 1000.0
            for _ in range(min(ticks, 500)):
                if self._bad:
                    if self._rng.random() < self.p_bg:
                        self._bad = False
                elif self._rng.random() < self.p_gb:
                    self._bad = True
        return self._bad

    def delay_s(self) -> float:
        if not self.enabled:
            return 0.0
        d = self.extra_delay_ms
        if self.jitter_ms > 0:
            d += abs(self._rng_jitter.gauss(0.0, self.jitter_ms))
        return d / 1000.0

    def reset(self, seed: int | None = None) -> None:
        """Restart the channel realisation from a known state.

        Calling this at the start of each arm of an A/B comparison makes every
        arm experience the IDENTICAL sequence of good and bad periods.  Without
        it each arm draws a different realisation, and with a mean burst of 4
        packets a 25-second call contains only about 47 bursts -- few enough
        that the difference between arms is dominated by which bursts happened
        to land where, not by the protection scheme under test.
        """
        base = 1234 if seed is None else seed
        self._rng = random.Random(base)
        self._rng_jitter = random.Random(base ^ 0x5A5A5A5A)
        self._bad = False
        self._last_tick = time.monotonic()

    def describe(self) -> str:
        if not self.enabled:
            return "clean"
        return (f"loss {self.loss_rate*100:.0f}% burst {self.mean_burst:.1f} "
                f"+{self.extra_delay_ms:.0f}ms jitter {self.jitter_ms:.0f}ms")


class UdpEndpoint:
    """A bound UDP socket with counters and optional impairment."""

    def __init__(self, bind_host: str = "0.0.0.0", bind_port: int = 0,
                 impairment: Impairment | None = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:      # 1 MB buffers so a burst of packets is not dropped by the OS
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        except OSError:
            pass
        self.sock.bind((bind_host, bind_port))
        self.impairment = impairment or Impairment()
        self.tx_packets = self.tx_bytes = 0
        self.rx_packets = self.rx_bytes = 0
        self.dropped_by_impairment = 0
        self._lock = threading.Lock()
        self._delay_q: list = []
        self._dq_lock = threading.Lock()
        self._dq_wake = threading.Event()
        self._dq_thread = None
        self._dq_seq = 0
        self._closing = False

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    @property
    def address(self) -> tuple:
        return self.sock.getsockname()

    def set_dscp(self, dscp: int) -> None:
        """Mark the IP header's DiffServ field -- the cross-layer hook."""
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, (dscp & 0x3F) << 2)
        except OSError:
            pass          # not permitted on every platform; harmless if refused

    def sendto(self, data: bytes, addr: tuple) -> None:
        if self.impairment.should_drop():
            with self._lock:
                self.dropped_by_impairment += 1
            return
        d = self.impairment.delay_s()
        if d > 0:
            self._enqueue_delayed(data, addr, d)
        else:
            self._raw_send(data, addr)

    # -- delayed delivery ------------------------------------------------
    # A single scheduler thread with a heap, NOT one threading.Timer per
    # packet.  With a timer per packet the operating system's scheduling
    # overhead grows with the number of packets sent, which silently punishes
    # exactly the configurations that transmit redundancy -- an artefact of the
    # measurement apparatus that would look like a property of the protocol.
    def _enqueue_delayed(self, data: bytes, addr: tuple, delay_s: float) -> None:
        with self._dq_lock:
            heapq.heappush(self._delay_q, (time.monotonic() + delay_s, self._dq_seq, data, addr))
            self._dq_seq += 1
            if self._dq_thread is None:
                self._dq_thread = threading.Thread(target=self._delay_pump, daemon=True)
                self._dq_thread.start()
            self._dq_wake.set()

    def _delay_pump(self) -> None:
        while not self._closing:
            with self._dq_lock:
                # The wake flag is consumed HERE, every iteration, not only when
                # the queue runs dry.  Clearing it only on an empty queue left it
                # permanently set during a call (there is always a packet in
                # flight), so wait() returned at once and this thread spun at
                # full speed, starving the media threads of the GIL.  Measured
                # cost of that spin: a 100-400 ms spread in one-way delay that
                # the controller read as congestion.  A set() that races this
                # clear() is harmless -- the queue head is re-examined below.
                self._dq_wake.clear()
                if not self._delay_q:
                    wait = 0.05
                else:
                    wait = max(0.0, self._delay_q[0][0] - time.monotonic())
            if wait > 0:
                self._dq_wake.wait(min(wait, 0.05))
            now = time.monotonic()
            due = []
            with self._dq_lock:
                while self._delay_q and self._delay_q[0][0] <= now:
                    _, _, d, a = heapq.heappop(self._delay_q)
                    due.append((d, a))
            for d, a in due:
                self._raw_send(d, a)

    def _raw_send(self, data: bytes, addr: tuple) -> None:
        try:
            self.sock.sendto(data, addr)
            with self._lock:
                self.tx_packets += 1
                self.tx_bytes += len(data)
        except OSError:
            pass

    def recvfrom(self, timeout: float | None = 0.5):
        self.sock.settimeout(timeout)
        try:
            data, addr = self.sock.recvfrom(4096)
        except (socket.timeout, OSError):
            return None, None
        with self._lock:
            self.rx_packets += 1
            self.rx_bytes += len(data)
        return data, addr

    def close(self) -> None:
        self._closing = True
        self._dq_wake.set()
        try:
            self.sock.close()
        except OSError:
            pass
