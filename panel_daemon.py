#!/usr/bin/env python3
"""
Live dashboards on both in-case screens.

Streams a rendered theme continuously to:
  * Lian Li 8.8" Universal Screen   (1cbe:a088, 480x1920 native / 1920x480 shown)
  * Jungle Leopard / HONGTAI        (33c3:7788, 960x480)

Continuous streaming is not just for live data: the HONGTAI firmware has a
real-time-play timeout and blanks the panel if no frame arrives, which is why a
single one-shot image shows briefly and then the screen goes dark. Keeping a
frame cadence going holds both panels awake.

The themes live in themes/ (one file each, auto-registered); the shared
palette / animation / telemetry machinery lives in fx.py.

Usage:
    .venv/bin/python panel_daemon.py --view-lianli system-electric \
        --view-jungle clock-electric --fps-lianli 19 --fps-jungle 17
    .venv/bin/python panel_daemon.py --palette ember --anim intense
"""

from __future__ import annotations

import argparse
import pathlib
import signal
import sys
import threading
import time
import tomllib

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

try:
    import psutil  # noqa: F401  (fx needs it; fail early with a clear message)
except ImportError as exc:  # pragma: no cover
    sys.exit(f"missing dependency: {exc}")

import fx
from argb import LedRing
from lianli88 import UniversalScreen, encode_jpeg as lianli_encode
from jungle import JungleLeopard, encode_jpeg as jungle_encode
from themes import VIEWS


class PanelWorker(threading.Thread):
    """One thread per panel: render, encode, push, and reconnect on failure."""

    def __init__(self, name: str, fps: float, stop: threading.Event,
                 view: str = "system-electric", rotate: int = 0,
                 brightness: int | None = None,
                 palette: fx.Palette | None = None, opts: dict | None = None):
        super().__init__(name=name, daemon=True)
        self.panel = name
        self.interval = 1.0 / max(0.2, fps)
        self.stop = stop
        self.view_name = view
        self.rotate = rotate % 360
        self.brightness = brightness
        self.palette = palette
        self.opts = opts or {}
        self.dev = None
        self.view = None
        self.frames = 0
        self.errors = 0

    def _connect(self) -> bool:
        try:
            if self.panel == "lianli":
                self.dev = UniversalScreen()
                self.dev.initialize()
                fb_w, fb_h = self.dev.screen.width, self.dev.screen.height
            else:
                self.dev = JungleLeopard()
                self.dev.refresh_info()
                fb_w, fb_h = self.dev.width, self.dev.height

            # Re-applied on every (re)connect: the panels come back at their own
            # stored default after a power cycle or replug.
            if self.brightness is not None:
                self.dev.set_brightness(self.brightness)

            # A quarter turn swaps the canvas we draw on: render at the size the
            # viewer sees, then rotate that to fill the panel's framebuffer.
            if self.rotate in (90, 270):
                vw, vh = fb_h, fb_w
            else:
                vw, vh = fb_w, fb_h
            self.view = VIEWS[self.view_name](vw, vh, self.palette, self.opts)

            print(f"[{self.panel}] connected — panel {fb_w}x{fb_h}, "
                  f"canvas {vw}x{vh}, rotate {self.rotate}°, "
                  f"view '{self.view_name}'", flush=True)
            return True
        except (SystemExit, Exception) as e:
            print(f"[{self.panel}] connect failed: {e}", flush=True)
            self.dev = None
            return False

    def run(self) -> None:
        while not self.stop.is_set():
            if self.dev is None and not self._connect():
                self.stop.wait(5.0)
                continue
            deadline = time.monotonic()
            while not self.stop.is_set():
                try:
                    img = self.view.render()
                    if self.rotate:
                        img = img.rotate(self.rotate, expand=True)
                    # Rotation already applied here, so the encoders get 0.
                    if self.panel == "lianli":
                        self.dev.send_jpeg(lianli_encode(img, self.dev.screen, rotate=0))
                    else:
                        self.dev.send_jpeg(jungle_encode(
                            img, self.dev.width, self.dev.height, self.dev.max_jpeg_kb))
                    self.frames += 1
                except Exception as e:
                    self.errors += 1
                    print(f"[{self.panel}] frame failed: {e}", flush=True)
                    try:
                        self.dev.close()
                    except Exception:
                        pass
                    self.dev = None
                    break
                deadline += self.interval
                delay = deadline - time.monotonic()
                if delay > 0:
                    self.stop.wait(delay)
                else:
                    deadline = time.monotonic()
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass


DEFAULTS = {
    "global": {"fps": 2.0, "readout_hz": 2.0, "anim": "normal", "palette": ""},
    "owner": {"name": "", "role": "", "date": ""},
    "lianli": {"view": "system-electric", "rotate": 90, "fps": 0.0,
               "brightness": -1, "palette": ""},
    "jungle": {"view": "clock-electric", "rotate": 0, "fps": 0.0,
               "brightness": -1, "palette": ""},
    "led": {"enabled": True, "fps": 24.0, "brightness": 0.6,
            "offset": 0, "reverse": False, "style": "sweep", "layout": "ring",
            "skew_left": 0},
    "case_led": {"enabled": False, "fps": 10.0, "brightness": 0.7,
                 "min_devices": 1},
}


def load_config(path: str | None) -> dict:
    """Layered settings: DEFAULTS <- config file <- CLI flags.

    The file is TOML (stdlib tomllib, no extra dependency). Without --config,
    the first existing candidate wins; running with no file at all is fine.
    """
    candidates = ([pathlib.Path(path)] if path else [
        pathlib.Path.home() / ".config" / "pc-screens" / "config.toml",
        pathlib.Path(__file__).resolve().parent / "config.toml",
    ])
    cfg = {sect: dict(vals) for sect, vals in DEFAULTS.items()}
    for cand in candidates:
        if cand.is_file():
            with open(cand, "rb") as f:
                loaded = tomllib.load(f)
            for sect, vals in loaded.items():
                if sect not in cfg:
                    raise SystemExit(f"{cand}: unknown section [{sect}]")
                for k, val in vals.items():
                    if k not in cfg[sect]:
                        raise SystemExit(f"{cand}: unknown key {sect}.{k}")
                    cfg[sect][k] = val
            print(f"config: {cand}", flush=True)
            break
        if path:
            raise SystemExit(f"--config {path}: not found")
    return cfg


class LedWorker(threading.Thread):
    """Streams frames to the ARGB ring around the Lian Li panel.

    The ring is a picture frame, so it follows the theme shown inside it:
    each tick asks the active view for `led_frame()` — palette gradient, load
    sparks, or whatever that theme overrides it with. Reconnects on failure
    like the panel workers, and blanks the ring on shutdown.
    """

    def __init__(self, stop: threading.Event, view_source, fps: float,
                 brightness: float):
        super().__init__(name="ledring", daemon=True)
        self.stop = stop
        self.view_source = view_source          # () -> view instance or None
        self.interval = 1.0 / max(1.0, fps)
        self.brightness = max(0.0, min(1.0, brightness))
        self.ring = None

    def run(self) -> None:
        while not self.stop.is_set():
            if self.ring is None:
                try:
                    self.ring = LedRing()
                    print("[ledring] connected — 60 LEDs", flush=True)
                except Exception as e:
                    print(f"[ledring] connect failed: {e}", flush=True)
                    self.stop.wait(5.0)
                    continue
            view = self.view_source()
            try:
                if view is not None:
                    frame = [tuple(int(c * self.brightness) for c in rgb)
                             for rgb in view.led_frame(self.ring.count)]
                    self.ring.send_frame(frame)
            except Exception as e:
                print(f"[ledring] frame failed: {e}", flush=True)
                try:
                    self.ring.close()
                except Exception:
                    pass
                self.ring = None
                continue
            self.stop.wait(self.interval)
        if self.ring:
            self.ring.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Live dashboards on both in-case screens",
        epilog="views: " + ", ".join(sorted(VIEWS)) + "  —  every flag can also "
               "be set in config.toml (flags win; see config.example.toml)")
    p.add_argument("--config", metavar="FILE",
                   help="TOML config file (default: ~/.config/pc-screens/"
                        "config.toml, then config.toml next to this script)")
    p.add_argument("--fps", type=float,
                   help="frames per second for both panels (default 2)")
    p.add_argument("--fps-lianli", type=float,
                   help="override fps for the Lian Li panel (animated views want 8-19)")
    p.add_argument("--fps-jungle", type=float,
                   help="override fps for the Jungle Leopard panel")
    p.add_argument("--readout-hz", type=float, metavar="HZ",
                   help="how often the numbers are allowed to change, independent "
                        "of fps (default 2; 1 is calmer still)")
    p.add_argument("--only", choices=["lianli", "jungle"], help="drive just one panel")
    p.add_argument("--view-lianli", choices=sorted(VIEWS),
                   help="theme for the Lian Li panel")
    p.add_argument("--view-jungle", choices=sorted(VIEWS),
                   help="theme for the Jungle Leopard panel")
    p.add_argument("--palette", metavar="NAME",
                   help="colour palette for both panels: "
                        + ", ".join(fx.PALETTES) + ", or custom:#hex,#hex,#hex,#hex "
                        "(default: each theme's own signature palette)")
    p.add_argument("--palette-lianli", metavar="NAME",
                   help="palette override for the Lian Li panel only")
    p.add_argument("--palette-jungle", metavar="NAME",
                   help="palette override for the Jungle Leopard panel only")
    p.add_argument("--anim", choices=["calm", "normal", "intense"],
                   help="animation intensity: scales every cadence and particle "
                        "count, still decoupled from fps (default normal)")
    p.add_argument("--rotate-lianli", type=int, choices=[0, 90, 180, 270],
                   help="quarter turns counter-clockwise (default 90)")
    p.add_argument("--rotate-jungle", type=int, choices=[0, 90, 180, 270],
                   help="quarter turns counter-clockwise (default 0)")
    p.add_argument("--brightness-lianli", type=int, metavar="0-100",
                   help="backlight for the Lian Li panel (default: leave alone)")
    p.add_argument("--brightness-jungle", type=int, metavar="0-100",
                   help="backlight for the Jungle Leopard panel (default: leave alone)")
    owner = p.add_argument_group("owner card (shown by the clock themes)")
    owner.add_argument("--owner-name", help='e.g. "Lucas Bui"')
    owner.add_argument("--owner-role", help='e.g. "Senior AI Engineer"')
    owner.add_argument("--owner-date", help='e.g. "24/09/1997"')
    args = p.parse_args()

    try:
        cfg = load_config(args.config)
    except SystemExit as e:
        p.error(str(e))

    def pick(cli, sect, key):
        return cli if cli is not None else cfg[sect][key]

    if pick(args.view_lianli, "lianli", "view") not in VIEWS:
        p.error(f"lianli view {cfg['lianli']['view']!r} unknown")
    if pick(args.view_jungle, "jungle", "view") not in VIEWS:
        p.error(f"jungle view {cfg['jungle']['view']!r} unknown")

    fx.set_readout_hz(float(pick(args.readout_hz, "global", "readout_hz")))
    fx.set_anim(pick(args.anim, "global", "anim"))
    try:
        pal = {}
        for name, flag in (("lianli", args.palette_lianli),
                           ("jungle", args.palette_jungle)):
            spec = (flag or args.palette or cfg[name]["palette"]
                    or cfg["global"]["palette"])
            pal[name] = fx.parse_palette(spec) if spec else None
    except ValueError as e:
        p.error(str(e))

    fx.prime_counters()

    opts = {"owner_name": pick(args.owner_name, "owner", "name"),
            "owner_role": pick(args.owner_role, "owner", "role"),
            "owner_date": pick(args.owner_date, "owner", "date")}
    view_cfg = {}
    fps = {}
    for name in ("lianli", "jungle"):
        bright = pick(getattr(args, f"brightness_{name}"), name, "brightness")
        view_cfg[name] = (pick(getattr(args, f"view_{name}"), name, "view"),
                          int(pick(getattr(args, f"rotate_{name}"), name, "rotate")),
                          None if bright in (-1, None) else int(bright))
        fps[name] = float(getattr(args, f"fps_{name}") or args.fps
                          or cfg[name]["fps"] or cfg["global"]["fps"])
    led_cfg = cfg["led"]
    try:
        fx.set_led_layout(led_cfg["offset"], led_cfg["reverse"],
                          led_cfg["style"], led_cfg["layout"],
                          led_cfg["skew_left"])
    except ValueError as e:
        p.error(str(e))
    panels = ["lianli", "jungle"] if not args.only else [args.only]
    stop = threading.Event()
    workers = [PanelWorker(n, fps[n], stop, *view_cfg[n], pal[n], opts)
               for n in panels]

    # The ring frames the Lian Li panel, so it mirrors that panel's theme
    # (falling back to the Leopard's if only that one is driven).
    by_name = {w.panel: w for w in workers}
    lead = by_name.get("lianli") or by_name.get("jungle")
    if led_cfg["enabled"]:
        workers.append(LedWorker(stop, lambda: lead.view,
                                 float(led_cfg["fps"]),
                                 float(led_cfg["brightness"])))

    # Case-wide sync is optional twice over: off by default in config, and a
    # missing openrgb-python or SDK server must never take the screens down.
    case_cfg = cfg["case_led"]
    if case_cfg["enabled"]:
        try:
            from case_led import CaseLedWorker
            workers.append(CaseLedWorker(stop, lambda: lead.view,
                                         float(case_cfg["fps"]),
                                         float(case_cfg["brightness"]),
                                         int(case_cfg["min_devices"])))
        except ImportError as e:
            print(f"[caseled] disabled — {e}", flush=True)

    def shutdown(*_):
        print("\nstopping…", flush=True)
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for w in workers:
        w.start()
    try:
        while not stop.is_set():
            stop.wait(10.0)
            if not stop.is_set():
                print("  " + "  ".join(
                    f"{w.panel}: {w.frames} frames, {w.errors} err"
                    for w in workers if isinstance(w, PanelWorker)), flush=True)
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
