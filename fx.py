"""Shared foundation for every panel theme.

Three things live here, in dependency order:

  * data     — one process-wide sample clock (`hold`) and the telemetry the
               themes draw (`vitals`, `stats`), so both panels always show the
               same numbers and no theme ever reads a sensor per frame.
  * palette  — named colour sets plus `custom:#hex,...`, so recolouring every
               theme is one CLI flag instead of ten edits.
  * FXBase   — the animation clock (wall-time, decoupled from fps) and the
               drawing primitives the themes share: bolts, sparks, glow text,
               arc gauges, sparklines, particle helpers.

Themes import from here only; nothing here imports a theme.
"""

from __future__ import annotations

import math
import os
import random
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import psutil
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from lianli88 import load_font

# =============================================================================
# Animation intensity — the --anim flag.
# =============================================================================
# One factor scales every cadence and particle count. It multiplies rates that
# are already wall-clock based, so it changes how busy the motion is without
# touching fps or smoothness.
ANIM_SPEED = 1.0

_ANIM_LEVELS = {"calm": 0.5, "normal": 1.0, "intense": 1.6}


def set_anim(level: str) -> None:
    global ANIM_SPEED
    ANIM_SPEED = _ANIM_LEVELS[level]


def hz(rate: float) -> float:
    """A cadence in events/second, scaled by --anim."""
    return rate * ANIM_SPEED


def dens(n: float) -> int:
    """A particle/branch count, scaled by --anim (never below 1)."""
    return max(1, round(n * ANIM_SPEED))


# The hardware doesn't report where LED index 0 sits on the bezel or which way
# the chain winds, so both are calibrated from config ([led] offset/reverse).
LED_OFFSET = 0
LED_REVERSE = False
LED_STYLE = "sweep"     # sweep: dark ring, only the scanline echo lights
                        # aurora: palette gradient + load sparks + echo
LED_LAYOUT = "ring"     # ring: one chain around the perimeter
                        # mirror: two chains, each running down one vertical
                        #         edge (first half right, second half left)
LED_SKEW_LEFT = 0       # shift one edge only, in whole LEDs: positive lights
LED_SKEW_RIGHT = 0      # it that much sooner. Needed when the bezel's real
                        # corner LED counts differ from the ideal ring by one,
                        # which skews the two edges against each other.


def set_led_layout(offset: int, reverse: bool, style: str = "sweep",
                   layout: str = "ring", skew_left: int = 0,
                   skew_right: int = 0) -> None:
    global LED_OFFSET, LED_REVERSE, LED_STYLE, LED_LAYOUT
    global LED_SKEW_LEFT, LED_SKEW_RIGHT
    if style not in ("sweep", "aurora"):
        raise ValueError(f"unknown led style {style!r} (sweep or aurora)")
    if layout not in ("ring", "mirror"):
        raise ValueError(f"unknown led layout {layout!r} (ring or mirror)")
    LED_OFFSET, LED_REVERSE = int(offset), bool(reverse)
    LED_STYLE, LED_LAYOUT = style, layout
    LED_SKEW_LEFT, LED_SKEW_RIGHT = int(skew_left), int(skew_right)


# =============================================================================
# Colour helpers + palettes
# =============================================================================

def lighten(c, f: float = 0.55):
    """Push a colour toward white — the 'hot' core of any glowing element."""
    return tuple(min(255, int(v + (255 - v) * f)) for v in c)


def scale(c, f: float):
    return tuple(max(0, min(255, int(v * f))) for v in c)


def mix(a, b, f: float):
    return tuple(int(av + (bv - av) * f) for av, bv in zip(a, b))


@dataclass(frozen=True)
class Palette:
    """Four metric accents plus the neutrals every theme needs.

    `accents` maps to cpu/gpu/ram/ssd in order; hot cores and dimmed halos are
    derived rather than stored, so a custom palette is just four hex codes.
    """

    name: str
    accents: tuple          # 4 base colours (cpu, gpu, ram, ssd)
    bg: tuple = (4, 6, 10)
    grid: tuple = (20, 30, 42)
    fg: tuple = (238, 245, 252)
    dim: tuple = (150, 166, 184)

    _KEYS = ("cpu", "gpu", "ram", "ssd")

    def base(self, key) -> tuple:
        i = key if isinstance(key, int) else self._KEYS.index(key)
        return self.accents[i % len(self.accents)]

    def hot(self, key) -> tuple:
        return lighten(self.base(key))

    def halo(self, key) -> tuple:
        return scale(self.base(key), 0.62)


PALETTES = {
    "spectrum": Palette("spectrum",
                        ((86, 156, 255), (118, 214, 128), (214, 152, 96), (198, 120, 220))),
    "cyan":     Palette("cyan",
                        ((79, 216, 255), (120, 164, 255), (170, 240, 255), (66, 130, 230)),
                        bg=(3, 7, 14), grid=(14, 32, 46)),
    "ember":    Palette("ember",
                        ((255, 94, 46), (255, 177, 74), (255, 214, 120), (226, 74, 104)),
                        bg=(10, 5, 3), grid=(40, 22, 14)),
    "violet":   Palette("violet",
                        ((182, 92, 255), (255, 113, 197), (140, 128, 255), (226, 160, 255)),
                        bg=(8, 4, 14), grid=(30, 18, 46)),
    "lime":     Palette("lime",
                        ((61, 255, 136), (186, 255, 100), (255, 225, 74), (84, 226, 190)),
                        bg=(3, 10, 6), grid=(14, 38, 24)),
    "ice":      Palette("ice",
                        ((159, 196, 255), (127, 178, 255), (208, 226, 255), (98, 150, 236)),
                        bg=(5, 8, 16), grid=(22, 32, 50)),
    "mono":     Palette("mono",
                        ((200, 208, 218), (176, 186, 198), (222, 228, 236), (150, 160, 172)),
                        bg=(6, 7, 9), grid=(26, 29, 34)),
}


def parse_palette(spec: str) -> Palette:
    """A named palette, or `custom:#rrggbb,...` with 1-4 accents."""
    if spec in PALETTES:
        return PALETTES[spec]
    if spec.startswith("custom:"):
        cols = []
        for part in spec[len("custom:"):].split(","):
            part = part.strip().lstrip("#")
            if len(part) != 6:
                raise ValueError(f"bad hex colour {part!r} in --palette")
            cols.append(tuple(int(part[i:i + 2], 16) for i in (0, 2, 4)))
        if not cols:
            raise ValueError("custom palette needs at least one colour")
        while len(cols) < 4:
            cols.append(cols[len(cols) % len(cols)])
        return Palette("custom", tuple(cols))
    raise ValueError(f"unknown palette {spec!r} "
                     f"(use one of {', '.join(PALETTES)} or custom:#hex,...)")


# =============================================================================
# Fonts
# =============================================================================

MONO_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
)
_mono_cache: dict[int, object] = {}


def load_mono(size: int):
    """Monospace face, cached — themes re-request the same sizes every frame."""
    if size not in _mono_cache:
        for path in MONO_FONTS:
            if os.path.exists(path):
                _mono_cache[size] = ImageFont.truetype(path, size)
                break
        else:
            _mono_cache[size] = load_font(size)
    return _mono_cache[size]


# =============================================================================
# Readout cadence — one sample clock for the whole process.
# =============================================================================
# Frame rate buys smoothness, not update frequency. Digits redrawn 19x/s are
# unreadable, and psutil.cpu_percent over a 50ms window is mostly noise anyway —
# a 500ms window is the number a person can actually act on. So every readout is
# sampled on this clock and held in between, while the animation keeps running
# at full frame rate on top of it.
READOUT_HZ = 2.0

_hold_lock = threading.Lock()
_held: dict[str, tuple[float, int, object]] = {}   # key -> (taken_at, serial, value)


def set_readout_hz(v: float) -> None:
    global READOUT_HZ
    READOUT_HZ = max(0.2, v)


def hold(key: str, produce, hz: float | None = None):
    """Return `produce()`, recomputed at most `hz` times a second.

    `hz` defaults to READOUT_HZ, read at call time so --readout-hz applies.
    The cache is process-wide, so both panels also agree on the numbers they
    are showing at any given moment instead of sampling a few ms apart.
    """
    hz = READOUT_HZ if hz is None else hz
    now = time.monotonic()
    with _hold_lock:
        hit = _held.get(key)
        if hit is not None and now - hit[0] < 1.0 / hz:
            return hit[2]
    value = produce()
    with _hold_lock:
        hit = _held.get(key)
        serial = hit[1] + 1 if hit else 0
        _held[key] = (now, serial, value)
    return value


def sample_serial(key: str) -> int:
    """How many times `key` has been resampled.

    Lets a view push to its history exactly once per sample rather than once
    per frame — otherwise a 48-point sparkline covers 2.5s at 19 fps and just
    smears, instead of the 24s of real history it was drawn to show.
    """
    with _hold_lock:
        hit = _held.get(key)
        return hit[1] if hit else -1


# =============================================================================
# Telemetry
# =============================================================================

_gpu_lock = threading.Lock()
_gpu_cache: tuple[float, dict] = (0.0, {})


def read_gpu() -> dict:
    """nvidia-smi is ~50ms, far too slow to call per sample — cache for 2s."""
    global _gpu_cache
    with _gpu_lock:
        ts, data = _gpu_cache
        if time.monotonic() - ts < 2.0:
            return data
    result: dict = {}
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run(
                [exe, "--query-gpu=temperature.gpu,utilization.gpu,memory.used,"
                      "memory.total,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3).stdout.strip().splitlines()
            if out:
                f = [p.strip() for p in out[0].split(",")]
                result = {"temp": float(f[0]), "util": float(f[1]),
                          "mem_used": float(f[2]), "mem_total": float(f[3]),
                          "power": float(f[4]) if f[4] not in ("", "[N/A]") else 0.0}
        except (subprocess.SubprocessError, ValueError, IndexError):
            result = {}
    with _gpu_lock:
        _gpu_cache = (time.monotonic(), result)
    return result


def cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    for chip in ("coretemp", "k10temp", "zenpower", "acpitz"):
        for s in temps.get(chip, []):
            if s.label in ("Package id 0", "Tctl", "Tdie", "") or not s.label:
                return s.current
    for entries in temps.values():
        if entries:
            return entries[0].current
    return None


def nvme_temp() -> float | None:
    try:
        for s in psutil.sensors_temperatures().get("nvme", []):
            if s.label in ("Composite", ""):
                return s.current
    except Exception:
        pass
    return None


class _Rate:
    """Per-second rate from a monotonically increasing counter."""

    def __init__(self):
        self.prev: dict[str, tuple[float, float]] = {}

    def __call__(self, key: str, total: float) -> float:
        now = time.monotonic()
        prev = self.prev.get(key)
        self.prev[key] = (now, total)
        if not prev or now <= prev[0]:
            return 0.0
        return max(0.0, (total - prev[1]) / (now - prev[0]))


_rate = _Rate()


def human_rate(bps: float) -> tuple[str, str]:
    for unit, div in (("GB/s", 2**30), ("MB/s", 2**20), ("KB/s", 2**10)):
        if bps >= div:
            return f"{bps / div:.1f}", unit
    return f"{bps:.0f}", "B/s"


@dataclass
class Vitals:
    """Everything a dense theme wants to show, sampled together on one clock."""

    load: float = 0.0
    per_core: list = field(default_factory=list)
    freq_ghz: float = 0.0
    temp: float | None = None
    gpu: dict = field(default_factory=dict)
    mem_pct: float = 0.0
    mem_used: float = 0.0          # GiB
    mem_total: float = 0.0         # GiB
    disk_pct: float = 0.0
    disk_used: float = 0.0         # GiB
    disk_total: float = 0.0        # GiB
    nvme_temp: float | None = None
    net_down: float = 0.0          # B/s
    net_up: float = 0.0            # B/s
    net_rx_total: float = 0.0      # GiB since boot
    net_tx_total: float = 0.0
    disk_rd: float = 0.0           # B/s
    disk_wr: float = 0.0
    procs: int = 0
    uptime: int = 0


def read_vitals() -> Vitals:
    # Single source for cpu_percent: it measures since its own last call, so
    # two independent samplers would each see half a window and disagree.
    # (The total and per-core calls keep separate state inside psutil.)
    mem = psutil.virtual_memory()
    freq = psutil.cpu_freq()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    try:
        io = psutil.disk_io_counters()
        rd, wr = _rate("rd", io.read_bytes), _rate("wr", io.write_bytes)
    except Exception:
        rd = wr = 0.0
    return Vitals(
        load=psutil.cpu_percent(interval=None),
        per_core=psutil.cpu_percent(interval=None, percpu=True),
        freq_ghz=freq.current / 1000 if freq else 0.0,
        temp=cpu_temp(),
        gpu=read_gpu(),
        mem_pct=mem.percent, mem_used=mem.used / 2**30, mem_total=mem.total / 2**30,
        disk_pct=disk.percent, disk_used=disk.used / 2**30, disk_total=disk.total / 2**30,
        nvme_temp=nvme_temp(),
        net_down=_rate("rx", net.bytes_recv), net_up=_rate("tx", net.bytes_sent),
        net_rx_total=net.bytes_recv / 2**30, net_tx_total=net.bytes_sent / 2**30,
        disk_rd=rd, disk_wr=wr,
        procs=len(psutil.pids()),
        uptime=int(time.time() - psutil.boot_time()),
    )


def vitals() -> Vitals:
    return hold("vitals", read_vitals)


def prime_counters() -> None:
    """First cpu_percent/rate calls return 0 — call once at startup."""
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)
    read_vitals()


@dataclass
class Tile:
    key: str
    label: str
    value: str
    unit: str
    pct: float          # 0..1, drives the bar
    sub: str = ""


@dataclass
class Stats:
    tiles: list[Tile] = field(default_factory=list)


def collect() -> Stats:
    """The classic CPU/GPU/RAM/SSD four-tile summary, on the sample clock."""
    tiles: list[Tile] = []
    v = vitals()
    tiles.append(Tile("cpu", "CPU", f"{v.load:.0f}", "%", v.load / 100,
                      f"{v.temp:.0f}°C" if v.temp else
                      (f"{v.freq_ghz:.1f} GHz" if v.freq_ghz else "")))
    if v.gpu:
        tiles.append(Tile("gpu", "GPU", f"{v.gpu['util']:.0f}", "%",
                          v.gpu["util"] / 100,
                          f"{v.gpu['temp']:.0f}°C  {v.gpu['power']:.0f}W"))
    tiles.append(Tile("ram", "RAM", f"{v.mem_used:.1f}", "GB",
                      v.mem_pct / 100, f"of {v.mem_total:.0f} GB"))
    tiles.append(Tile("ssd", "SSD", f"{v.disk_pct:.0f}", "%", v.disk_pct / 100,
                      f"{v.nvme_temp:.0f}°C" if v.nvme_temp
                      else f"{v.disk_used:.0f} GB"))
    return Stats(tiles)


def stats() -> Stats:
    return hold("stats", collect)


# =============================================================================
# FXBase — animation clock + shared drawing primitives
# =============================================================================

class FXBase:
    """Base for every animated theme.

    Subclasses call `self._start(seed)` in __init__ and `self._tick()` at the
    top of render(); they provide `self.w/h` and `self.pal` (a Palette).

    Every moving part runs on wall-clock time, not on the frame counter, so
    raising the frame rate makes the motion smoother rather than faster, and
    each element gets the rate that actually suits it. The named cadences
    below are the house defaults; themes override or add their own.
    """

    HZ_ARC = 10.5       # lightning re-strikes per second
    HZ_PIP = 2.5        # activity-dot steps per second
    HZ_MARK = 3.0       # footer block-marker steps per second
    HZ_PULSE = 0.9      # electrode breathe cycles per second
    RPS_SPOKE = 0.25    # electrode spoke revolutions per second
    SWEEP_S = 5.0       # seconds for the scanline to cross the panel
    EASE_S = 0.30       # bar glide time constant

    def _start(self, seed: int) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self._t0 = time.monotonic()
        self.t = self.dt = 0.0
        self.frame = 0
        self._eased: dict[str, float] = {}

    def _tick(self) -> None:
        """Advance the animation clock. Call once at the top of render().

        Re-seeding the RNG from the arc index is what holds each discharge for
        its whole 1/HZ_ARC window: same seed, same bolt, so a strike persists
        across frames and then snaps to a new one, which is what lightning
        looks like. It also stops the on/off arcs from strobing at frame rate.
        """
        self.frame += 1
        now = time.monotonic() - self._t0
        self.dt = max(0.0, now - self.t)
        self.t = now
        self.arc = int(now * hz(self.HZ_ARC))
        self.rng.seed(self.seed ^ (self.arc * 0x9E3779B1))

    def cycle(self, rate: float, salt: int = 0) -> int:
        """Index of the current window of a `rate`/s cadence (anim-scaled)."""
        return int(self.t * hz(rate)) + salt

    def wrng(self, rate: float, salt: int = 0) -> random.Random:
        """RNG frozen for the current window of a `rate`/s cadence.

        Same window, same stream — this is how a theme holds a random layout
        (a lightning bolt, a glyph column, a star twinkle) steady across
        frames and then snaps it, instead of strobing at frame rate.
        """
        return random.Random(self.seed ^ (self.cycle(rate, salt) * 0x9E3779B1)
                             ^ (salt * 0x85EBCA6B))

    def ease(self, key: str, target: float) -> float:
        """Frame-rate-independent glide toward `target`.

        The digits are allowed to step on the sample clock, but a bar jumping
        twice a second reads as a twitch — so the fill chases the sample while
        the number itself snaps to it.
        """
        prev = self._eased.get(key)
        if prev is None or self.dt <= 0.0:
            self._eased[key] = target
            return target
        val = prev + (target - prev) * (1.0 - math.exp(-self.dt / self.EASE_S))
        self._eased[key] = val
        return val

    # --- palette shorthands ---
    def col(self, key, hot: bool = False):
        return self.pal.hot(key) if hot else self.pal.base(key)

    # --- the ARGB ring around the Lian Li panel --------------------------------
    RPS_RING = 0.055      # gradient revolutions per second
    HZ_RING_SPARK = 8.0   # spark re-rolls per second
    LED_SWEEP = True      # ring echoes the scanline sweeping the panel
    LED_SWEEP_ZONE = 0.14  # half-height of the echo zone, fraction of panel

    def _led_positions(self, n: int) -> list:
        """Normalized (x, y) of each ring LED on the panel's bezel.

        The ring physically frames the panel, so effects that track on-screen
        motion need to know where each LED sits. Modelled as n LEDs spread
        evenly around the canvas rectangle, clockwise from the top-left corner;
        LED_OFFSET/LED_REVERSE (config [led]) calibrate the real index origin
        and winding, which the hardware doesn't report.
        """
        key = (n, LED_OFFSET, LED_REVERSE, LED_LAYOUT,
               LED_SKEW_LEFT, LED_SKEW_RIGHT)
        cached = getattr(self, "_led_pos", None)
        if cached and cached[0] == key:
            return cached[1]
        w, h = float(self.w), float(self.h)
        pos = []
        for k in range(n):
            i = ((n - k if LED_REVERSE else k) + LED_OFFSET) % n
            if LED_LAYOUT == "mirror":
                half = n // 2
                if i < half:                # first chain: right edge, down
                    pos.append((1.0, i / max(1, half - 1)))
                else:                       # second chain: left edge, down
                    pos.append((0.0, (i - half) / max(1, n - half - 1)))
                continue
            per = 2 * (w + h)
            s = i / n * per
            if s < w:                       # top edge, left -> right
                pos.append((s / w, 0.0))
            elif s < w + h:                 # right edge, down
                pos.append((1.0, (s - w) / h))
            elif s < 2 * w + h:             # bottom edge, right -> left
                pos.append((1.0 - (s - w - h) / w, 1.0))
            else:                           # left edge, up
                pos.append((0.0, 1.0 - (s - 2 * w - h) / h))
        if LED_SKEW_LEFT or LED_SKEW_RIGHT:
            # One chain step on a vertical edge, in y units.
            pitch = 2 * (w + h) / (n * h)
            skew = {0.0: LED_SKEW_LEFT, 1.0: LED_SKEW_RIGHT}
            pos = [(x, min(1.0, max(0.0, y - skew.get(x, 0) * pitch)))
                   for x, y in pos]
        self._led_pos = (key, pos)
        return pos

    def led_frame(self, n: int) -> list:
        """One frame for the LED ring, in this theme's palette.

        Called from the LED worker thread, so time is taken from the wall clock
        directly (self.t freezes if the panel disconnects and render() stops).
        Two styles ([led] style): "sweep" keeps the ring dark and lights only
        the LEDs level with the on-screen scanline; "aurora" is a slow palette
        gradient with load sparks. Themes can override for a stronger signature.
        """
        t = time.monotonic() - self._t0
        if LED_STYLE == "sweep" and self.LED_SWEEP:
            return self._led_sweep(t, n)
        return self._led_aurora(t, n)

    LED_LEAD = 0.06       # ramp-in just ahead of the sweep line
    LED_TAIL = 0.20       # fast fade-out behind it

    def _led_sweep(self, t: float, n: int) -> list:
        """Dark ring; only the LEDs beside the scanline glow, fading fast.

        Same clock base as _scanline, so the pair of bright dots on the bezel
        rides exactly level with the band crossing the panel.
        """
        sweep_y = (t / self.SWEEP_S) % 1.0
        base = self.pal.base("cpu")
        hot = lighten(base, 0.85)
        out = [(0, 0, 0)] * n
        for i, (_, y) in enumerate(self._led_positions(n)):
            d = sweep_y - y            # > 0 once the line has passed this LED
            if -self.LED_LEAD <= d <= 0:
                k = (1.0 + d / self.LED_LEAD) ** 2
                out[i] = scale(hot, k * 0.85)
            elif 0 < d <= self.LED_TAIL:
                k = (1.0 - d / self.LED_TAIL) ** 2
                out[i] = scale(mix(base, hot, k), k * 0.85)
        return out

    def _led_aurora(self, t: float, n: int) -> list:
        """Palette gradient breathing around the ring + load sparks + echo."""
        v = vitals()
        load = v.load / 100
        breathe = 0.60 + 0.18 * math.sin(t * math.tau / 4.0)
        rot = t * hz(self.RPS_RING)
        acc = self.pal.accents
        out = []
        for i in range(n):
            seg = ((i / n + rot) % 1.0) * len(acc)
            c = mix(acc[int(seg) % len(acc)], acc[(int(seg) + 1) % len(acc)],
                    seg - int(seg))
            out.append(scale(c, breathe))
        if self.LED_SWEEP:
            sweep_y = (t / self.SWEEP_S) % 1.0
            hot = lighten(self.pal.base("cpu"), 0.9)
            for i, (_, y) in enumerate(self._led_positions(n)):
                boost = 1.0 - abs(y - sweep_y) / self.LED_SWEEP_ZONE
                if boost > 0:
                    out[i] = mix(out[i], hot, min(1.0, boost * 1.6))
        rng = random.Random(self.seed ^ (int(t * hz(self.HZ_RING_SPARK))
                                         * 0x9E3779B1) ^ 0x51ED)
        for _ in range(dens(1 + load * 6)):
            out[rng.randrange(n)] = lighten(self.pal.base("cpu"), 0.85)
        return out

    # --- text -----------------------------------------------------------------
    def glow_text(self, img: Image.Image, text: str, font, xy, colour,
                  alpha: int = 215, blur: float | None = None) -> None:
        """Bloom pass behind a headline, then the crisp glyphs go on top."""
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(xy, text, font=font, fill=tuple(colour) + (alpha,))
        layer = layer.filter(ImageFilter.GaussianBlur(
            blur if blur is not None else max(2, font.size * 0.07)))
        img.alpha_composite(layer)

    def spaced_text(self, d, text: str, font, y: float, colour, spacing: float,
                    w: float | None = None) -> None:
        """Letter-spaced line, centred on the canvas (or a given width)."""
        w = self.w if w is None else w
        total = sum(d.textlength(ch, font=font) + spacing for ch in text) - spacing
        x = (w - total) / 2
        for ch in text:
            d.text((x, y), ch, font=font, fill=colour)
            x += d.textlength(ch, font=font) + spacing

    def fit_text(self, d, text: str, font, max_w: float, floor: int = 9,
                 mono: bool = False):
        """Shrink a font until `text` fits `max_w`."""
        loader = load_mono if mono else load_font
        while d.textlength(text, font=font) > max_w and font.size > floor:
            font = loader(int(font.size * 0.92))
        return font

    # --- gauges & graphs ------------------------------------------------------
    def arc_gauge(self, d, cx, cy, r, frac, base, width=8,
                  a0=120, a1=420, track=None, ticks=0, tick_col=None):
        """Circular-arc meter from angle a0 to a1 (degrees, PIL convention)."""
        box = [cx - r, cy - r, cx + r, cy + r]
        if track:
            d.arc(box, a0, a1, fill=track, width=width)
        frac = max(0.0, min(1.0, frac))
        if frac > 0:
            d.arc(box, a0, a0 + (a1 - a0) * frac, fill=base, width=width)
        for i in range(ticks):
            a = math.radians(a0 + (a1 - a0) * i / (ticks - 1))
            r0, r1 = r + width * 0.9, r + width * 1.6
            d.line([(cx + math.cos(a) * r0, cy + math.sin(a) * r0),
                    (cx + math.cos(a) * r1, cy + math.sin(a) * r1)],
                   fill=tick_col or track or base, width=2)

    def seg_bar(self, d, x, y, w, h, frac, n, on, off, gap_frac=0.25):
        """Meter as n discrete segments — the LED-row look."""
        frac = max(0.0, min(1.0, frac))
        seg = w / n
        for i in range(n):
            lit = (i + 0.5) / n <= frac
            d.rectangle([x + i * seg, y, x + i * seg + seg * (1 - gap_frac), y + h],
                        fill=on if lit else off)

    def ribbon(self, img, pts, base, area_alpha=70, width=2,
               baseline: float | None = None):
        """History polyline with a translucent area fill under it."""
        if baseline is None:
            baseline = max(p[1] for p in pts)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon(
            [(pts[0][0], baseline)] + list(pts) + [(pts[-1][0], baseline)],
            fill=tuple(base) + (area_alpha,))
        img.alpha_composite(layer)
        d = ImageDraw.Draw(img)
        d.line(pts, fill=base, width=width)
        return d

    # --- history on the sample clock ------------------------------------------
    def hist(self, key: str, value: float, n: int = 48) -> deque:
        """Per-theme history that appends once per vitals sample, not per frame.

        Serial-tracked per key, so a theme can keep any number of histories and
        each one still gets exactly one point per sample.
        """
        if not hasattr(self, "_hists"):
            self._hists: dict[str, deque] = {}
            self._hist_serials: dict[str, int] = {}
        serial = sample_serial("vitals")
        dq = self._hists.get(key)
        if dq is None:
            dq = self._hists[key] = deque([value] * n, maxlen=n)
            self._hist_serials[key] = serial
        elif self._hist_serials.get(key) != serial:
            self._hist_serials[key] = serial
            dq.append(value)
        return dq

    # --- backgrounds & overlays -----------------------------------------------
    def _grid_bg(self, step: float) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), self.pal.bg)
        d = ImageDraw.Draw(img)
        y = 0.0
        while y < self.h:
            d.line([(0, y), (self.w, y)], fill=self.pal.grid, width=1)
            y += step
        x = 0.0
        while x < self.w:
            d.line([(x, 0), (x, self.h)], fill=self.pal.grid, width=1)
            x += step
        return img

    def _scanline(self, img, colour=(150, 210, 255)):
        """Translucent band sweeping down the panel, one pass per SWEEP_S."""
        band = max(6, int(self.h * 0.05))
        pos = int(((self.t / self.SWEEP_S) % 1.0) * (self.h + band)) - band
        strip = Image.new("RGBA", (self.w, band), (0, 0, 0, 0))
        sd = ImageDraw.Draw(strip)
        for row in range(band):
            a = int(46 * math.sin(math.pi * row / band))
            sd.line([(0, row), (self.w, row)], fill=tuple(colour) + (a,))
        img.alpha_composite(strip, (0, max(0, pos)))

    def _pips(self, d, x, y, size, colour, off, n=3, dark=(44, 60, 78)):
        """Row of activity dots stepping at HZ_PIP, independent of frame rate."""
        step = int(self.t * hz(self.HZ_PIP))
        for i in range(n):
            on = (step + off + i) % n == 0
            d.ellipse([x + i * size * 1.9, y, x + i * size * 1.9 + size, y + size],
                      fill=colour if on else dark)

    # --- electric primitives (the house 'electric' language) ------------------
    def _bolt(self, d, x0, y0, x1, y1, amp, colour, hot, segs=18, width=3):
        """Jagged discharge from (x0,y0) to (x1,y1), drawn core-over-halo."""
        # Alternating the offset sign each segment gives hard zigzag corners.
        # A pure random walk per point reads as a smooth waveform, not a spark.
        pts = []
        for i in range(segs + 1):
            t = i / segs
            taper = math.sin(math.pi * t)          # pinned at both ends
            sign = 1 if i % 2 else -1
            off = self.rng.uniform(amp * 0.5, amp) * sign * taper
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t + off))
        d.line(pts, fill=colour, width=width + 4, joint="curve")
        d.line(pts, fill=hot, width=max(1, width), joint="curve")
        return pts

    def _branches(self, d, pts, amp, colour, n):
        for _ in range(n):
            i = self.rng.randrange(2, max(3, len(pts) - 2))
            bx, by = pts[i]
            d.line([(bx, by),
                    (bx + self.rng.uniform(-amp, amp) * 1.6,
                     by + self.rng.uniform(-amp, amp) * 2.2)],
                   fill=colour, width=2)

    def _sparks(self, d, x, y, r, colour, n):
        for _ in range(n):
            a = self.rng.uniform(0, math.tau)
            dist = self.rng.uniform(r * 0.4, r * 2.4)
            s = self.rng.uniform(1.0, 2.4)
            px, py = x + math.cos(a) * dist, y + math.sin(a) * dist
            d.ellipse([px - s, py - s, px + s, py + s], fill=colour)

    TRACK = (26, 34, 46)

    def _electric_bar(self, d, x, y, avail, bh, pct, base, hot):
        """Meter track + fill + arcs hugging it + the leading electrode."""
        d.rounded_rectangle([x, y, x + avail, y + bh], radius=bh / 2, fill=self.TRACK)
        fill_w = max(avail * pct, bh)          # keep a visible cap even at 0%
        d.rounded_rectangle([x, y, x + fill_w, y + bh], radius=bh / 2, fill=base)

        cy = y + bh / 2
        dim = scale(base, 0.62)
        ax0, ax1 = x + bh * 0.5, x + fill_w - bh * 0.4

        if ax1 - ax0 > bh:
            for edge in (-1, 1):
                if edge > 0 and self.rng.random() > 0.55:
                    continue
                self._bolt(d, ax0, cy + edge * bh * 0.62, ax1, cy + edge * bh * 0.62,
                           bh * 0.30, dim, base,
                           segs=max(4, int((ax1 - ax0) / 30)), width=1)
            amp = bh * (0.30 + 0.60 * pct)
            bolt = self._bolt(d, ax0, cy, ax1, cy, amp, base, hot,
                              segs=max(5, int((ax1 - ax0) / 26)),
                              width=2 if pct < 0.6 else 3)
            self._branches(d, bolt, amp, hot, dens(1 + int(pct * 3)))

        if fill_w < avail - bh and self.rng.random() < 0.5:
            reach = min(avail - fill_w, bh * self.rng.uniform(2.0, 6.0))
            self._bolt(d, x + fill_w, cy, x + fill_w + reach, cy,
                       bh * 0.55, dim, hot, segs=5, width=1)

        ex = x + fill_w
        pulse = 0.80 + 0.20 * math.sin(self.t * math.tau * hz(self.HZ_PULSE))
        r = bh * 0.42 * pulse
        for k in range(4):
            a = self.t * math.tau * hz(self.RPS_SPOKE) + k * math.tau / 4
            d.line([(ex + math.cos(a) * r * 1.5, cy + math.sin(a) * r * 1.5),
                    (ex + math.cos(a) * r * 2.1, cy + math.sin(a) * r * 2.1)],
                   fill=base, width=2)
        d.ellipse([ex - r * 1.7, cy - r * 1.7, ex + r * 1.7, cy + r * 1.7],
                  outline=base, width=2)
        d.ellipse([ex - r, cy - r, ex + r, cy + r], fill=hot)
        self._sparks(d, ex, cy, bh * 0.42, hot, dens(2 + int(pct * 6)))
        return fill_w
