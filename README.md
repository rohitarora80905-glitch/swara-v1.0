# SWARA — Secure Wideband Adaptive Real-time Audio over UDP

A **working voice chat application** over UDP with mutual user authentication,
plus the research evaluation that motivated its design.

Real UDP sockets. Real ADPCM speech coding. Real X25519 / ChaCha20-Poly1305
cryptography. Audio goes in one end and comes out the other.

---

## Quick start — a real call between two terminals

```bash
python3 server.py --add-user alice --password secret1
python3 server.py --add-user bob   --password secret2
python3 server.py --port 9000                      # terminal 1

python3 client.py --user bob   --password secret2 --wait        # terminal 2
python3 client.py --user alice --password secret1 --call bob    # terminal 3
```

With `sounddevice` installed (`pip install sounddevice`) this uses your
microphone and speakers. Without it, it falls back to generated speech so the
application still runs on a machine with no sound card.

### One-command demonstration

```bash
python3 demo.py                                                    # clean network
python3 demo.py --seconds 30 --loss 0.15 --burst 4 --jitter 12 --compare
```

The `--compare` run performs the same call three times — SWARA, conventional
uniform FEC, and unprotected UDP — and prints a like-for-like table.

### Measured, 25 s call, 15% loss, mean burst 4, 12 ms jitter
*(identical channel realisation per arm -- see the fairness note below)*

| Arm | Perceptual damage | Frames unrecoverable | Wire rate | Auth failures |
|---|---|---|---|---|
| Unprotected UDP | 10.8% | 16.4% | 86.8 kbit/s | 0 |
| Uniform FEC | 9.1% | 14.5% | 125.9 kbit/s | 0 |
| **SWARA** | **7.9%** | 14.6% | **109.6 kbit/s** | 0 |

Perceptual damage weights every lost frame by how audible its loss was.  On raw
frame counts SWARA matches uniform FEC; perceptually it is ahead of both, on
13% less bandwidth than uniform.  Single runs are noisy: for a stable figure use
`--seconds 30 --repeat 3`.

**Fairness of the A/B.** The impairment injector keeps two independent random
streams -- one for the Gilbert-Elliott loss chain, one for jitter -- so an arm
that sends more packets (more jitter draws) cannot perturb the loss pattern it
is being measured against.  Every arm therefore faces the identical sequence of
good and bad periods.

**Reliable signalling.** Call placement retransmits with a fixed request id and
the server answers duplicates idempotently (the same `call_ready` / `incoming`
pair, never a second call) -- SIP-style INVITE retransmission, so call setup
succeeds even at 15% packet loss.

### Research evaluation (simulation, for the paper)

```bash
python3 experiments/run_all.py       # ~3 min, regenerates all 8 figures
```

---

## Web console

```bash
pip install -r requirements.txt
python api_server.py        # HTTP on 8000, an HTTPS share link on 8443, the UDP relay on 9000
# HTTP only: python -m uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** (on Windows, `start_console.bat` does both steps).

**No terminal?** Double-click `SWARA Server.bat` (or run `pythonw launcher.py`) to open the
*SWARA Server Control* window: start / stop / restart the server, change the web and relay
ports, see status, the LAN address for other laptops, and the live server log.
The same process hosts the real UDP relay on `udp://0.0.0.0:9000`, so CLI clients
can connect to it while the console watches. Swagger docs live at `/docs`.

| Page | What it does |
|---|---|
| **Softphone** | Sign in to a relay with your microphone and call another signed-in person — on this computer, on a friend's computer running the same console, or from a phone via the HTTPS share link. Live MOS, loss after repair, FEC / replica / concealment counts, mid-call network impairment, mute, hang up, and a call report. |
| Live Call Lab | A real instrumented call (relay + SPARK + AEAD over UDP) streamed to the browser: per-frame fate map, live charts, listen live, change the network mid-call, compare sent vs received audio. Can use your own recorded voice. |
| A/B Benchmark | The same call under SWARA, uniform FEC and no protection over an identical channel, with charts, a table and audio for each arm. |
| Network Simulator | The offline engine (`swara/engine.py`) on demand, with named profiles and a loss sweep. |
| Research Results | Interactive versions of all eight experiments, the paper figures, and a button to regenerate them. |
| Protocol toolkit | Perceptual analyser, Reed–Solomon playground, PI-AFEC scheduler, E-model and NAVIK explorer, SPARK handshake, packet anatomy, SETU overlay. |
| Relay & Users | Hosted relay status, online users, active calls, log; register and delete users. |

Environment: `SWARA_RELAY_PORT` (default 9000), `SWARA_RELAY_AUTOSTART=0` to not start the relay;
`python api_server.py --https-port 0` turns the HTTPS listener off.
Runtime files (lab credentials, uploaded clips, the self-signed certificate) go in `runtime/`.

### A live call between two computers, all in the browser

1. **Host (your PC):** double-click `SWARA Server.bat`. The window starts the console, the UDP
   relay (`udp:9000`) and shows an HTTPS share link such as `https://192.168.1.20:8443/`. Allow
   Python through the Windows firewall when asked. Open the console → **Softphone** → sign in
   (for example `alice` / `secret1`).
2. **Friend, option A — nothing to install:** on the same Wi-Fi, open the host's **https://** share
   link on a phone or laptop, accept the certificate warning once (the certificate is self-signed),
   go to **Softphone**, leave the relay as `127.0.0.1:9000` (that is the host's relay, because the
   call runs inside the host) and sign in as `bob` / `secret2`.
   **Friend, option B — real PC-to-PC UDP:** run the same console on the friend's PC
   (`SWARA Server.bat`), open `http://localhost:8000` → **Softphone**, set the relay address to the
   host's IP, e.g. `192.168.1.20:9000`, and sign in as `bob`.
3. Either side clicks **Call** next to the other's name; incoming calls are answered automatically.
   Earphones on both sides avoid feedback. Drag the network sliders during the call to show the
   protection working, or sign in with *Loss protection: None* to hear the difference.

Register more users on **Relay & Users** — the registry is the host's `users.json`. Browsers allow
the microphone only on `localhost` or over HTTPS, which is why other devices must use the share link.

## Modules

| File | Responsibility |
|---|---|
| `client.py` | **The application.** Login, call setup, capture, playback |
| `server.py` | Registration, SPARK authentication, presence, media relay |
| `demo.py` | Self-contained end-to-end demonstration and A/B harness |
| `api_server.py` | FastAPI app: web console, REST API, hosted relay |
| `webapp/` | Console back end: instrumented live calls, relay manager, toolkit wrappers |
| `static/` | Console front end (HTML/CSS/JS, Chart.js vendored for offline use) |
| `swara/session.py` | Live pipeline — four threads: send, receive, playout, control |
| `swara/codec.py` | IMA-ADPCM, **per-frame state reset** so one loss cannot cascade |
| `swara/audio_io.py` | Microphone / speaker, WAV, and synthetic backends |
| `swara/net.py` | UDP endpoint, DSCP marking, time-based impairment injector |
| `swara/perceptual.py` | PI analyser — energy, voicing, spectral flux, onset |
| `swara/fec.py` | Reed–Solomon over GF(2⁸) (verified MDS); PI-AFEC scheduler |
| `swara/auth.py` | SPARK handshake, key schedule, AEAD record layer, migration |
| `swara/controller.py` | NAVIK joint optimiser; congestion vs link-loss discrimination |
| `swara/receiver.py` | Network state estimation; R-factor-optimal jitter buffer |
| `swara/emodel.py` | ITU-T G.107 quality model, perceptually weighted loss |
| `swara/overlay.py` | SETU distance-vector relay selection |
| `swara/channel.py` | Gilbert–Elliott model used by the offline evaluation |
| `swara/engine.py` | Offline harness: SWARA vs uniform FEC vs baseline |

## Command-line options worth knowing

```
--protect swara|uniform|none      which protection scheme (the A/B switch)
--audio live|wav|synthetic        where audio comes from and goes to
--loss 0.15 --burst 4             inject impairment locally, for the demo
--extra-delay 40 --jitter 15
--narrowband                      8 kHz mode, 33 kbit/s instead of 65
```

## Design decisions worth defending in a viva

1. **Per-frame codec state reset.** Standard ADPCM carries predictor state
   across frames; one lost packet then corrupts everything after it. We reset
   per frame and send a 3-byte preamble. Slightly less efficient, vastly more
   robust. *A codec designed for a file is the wrong codec for a network.*

2. **Playout depth bounds protection.** Parity is emitted only when a group
   closes, and a replica must beat its own frame's deadline. Both are therefore
   limited by buffer depth. Paying for redundancy the buffer is too shallow to
   use is pure waste — so depth and protection are decided together, not
   separately.

3. **Congestion is not the same as loss.** Naive AIMD backs off on any loss,
   which is right on a wired path and wrong on a wireless one. We back off only
   when loss is accompanied by a *rising delay trend*.

4. **The relay cannot hear the call.** Media keys are derived from a call
   secret delivered to both parties; the server forwards datagrams verbatim.

5. **Time-based channel model.** The impairment advances on a wall clock, not
   per packet. Stepping per packet gave three arms configured for 15% loss
   measured rates of 9.5%, 12.3% and 12.5% — an artefact of the instrument that
   would have contaminated every comparison.

## Honest limitations

- Simulation MOS figures come from the G.107 model, not human listeners.
- The research corpus is generated speech, not recorded human speech.
- Live calls use real UDP, but impairment is injected locally rather than
  encountered in the wild.
- At mean burst ≥ 10 packets every repair mechanism fails and redundancy
  becomes wasted bandwidth.
- SPARK composes well-analysed primitives but has no formal security proof.
