"""Case-wide LED sync via the OpenRGB SDK server.

The screens set the look; the case follows. This worker mirrors the active
theme's palette onto every RGB device OpenRGB exposes (GPU, motherboard
ARGB headers driving the case fans, ...):

  * addressable zones (fans/strips) breathe the palette gradient slowly and
    throw hot sparks whose count rides CPU load — the "aurora" ring effect,
    case-sized;
  * single-zone devices (the GPU block) sit on the primary accent and warm
    toward white as the GPU gets hot.

Rates go through fx.hz() so --anim scales the case exactly like the panels.
Requires `openrgb --server` (see systemd/openrgb-server.service) and the
openrgb-python package; panel_daemon degrades gracefully without either.
"""

from __future__ import annotations

import math
import random
import subprocess
import threading
import time

from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

import fx


class CaseLedWorker(threading.Thread):
    """Streams palette-synced frames to every OpenRGB device."""

    RPS = 0.04            # gradient revolutions per second across a zone
    HZ_SPARK = 6.0        # spark re-rolls per second
    RECONNECT_S = 15.0    # OpenRGB server may start after us

    def __init__(self, stop: threading.Event, view_source, fps: float,
                 brightness: float, min_devices: int = 1):
        super().__init__(name="caseled", daemon=True)
        self.stop = stop
        self.view_source = view_source
        self.interval = 1.0 / max(1.0, fps)
        self.brightness = max(0.0, min(1.0, brightness))
        self.min_devices = max(1, int(min_devices))
        self.client = None
        self._t0 = time.monotonic()
        self._scan_retries = 0

    def _connect(self) -> bool:
        try:
            self.client = OpenRGBClient(name="pc-screens")
            if not self.client.devices:
                # The server enumerates hardware for a while after it starts;
                # an empty list means "come back later", not "nothing to do".
                self.client.disconnect()
                self.client = None
                print("[caseled] server has no devices yet — retrying",
                      flush=True)
                return False
            # Detection is flaky and takes a while after server start — the
            # client may connect mid-scan and see a partial list. First give
            # the scan time and re-pull the list over the same connection;
            # only if it stays short do we bounce the server (i2c GPUs
            # genuinely miss ~1 scan in 3 here).
            for _ in range(4):
                if (len(self.client.devices) >= self.min_devices
                        or self.stop.is_set()):
                    break
                self.stop.wait(8.0)
                self.client.update()
            if (len(self.client.devices) < self.min_devices
                    and self._scan_retries < 3):
                self._scan_retries += 1
                print(f"[caseled] scan found {len(self.client.devices)}"
                      f"/{self.min_devices} devices — restarting server "
                      f"(attempt {self._scan_retries}/3)", flush=True)
                self.client.disconnect()
                self.client = None
                subprocess.run(["systemctl", "--user", "restart",
                                "openrgb-server.service"], timeout=30)
                return False
            for dev in self.client.devices:
                try:
                    dev.set_mode("Direct")
                except Exception:
                    pass          # non-addressable device: colours still apply
            names = ", ".join(d.name.strip() for d in self.client.devices)
            print(f"[caseled] connected — {names}", flush=True)
            return True
        except Exception as e:
            print(f"[caseled] connect failed: {e}", flush=True)
            self.client = None
            return False

    def _frame(self, dev, pal, t: float, v) -> None:
        load = v.load / 100
        n = len(dev.leds)
        if n <= 0:
            return
        if n == 1 or dev.type.name == "GPU":
            # Single point of colour: primary accent, warming with GPU heat.
            heat = 0.0
            if v.gpu:
                heat = max(0.0, min(1.0, (v.gpu["temp"] - 40) / 45))
            c = fx.mix(pal.base("cpu"), fx.lighten(pal.base("cpu"), 0.9), heat)
            colors = [c] * n
        else:
            breathe = 0.55 + 0.25 * math.sin(t * math.tau / 5.0)
            rot = t * fx.hz(self.RPS)
            acc = pal.accents
            colors = []
            for i in range(n):
                seg = ((i / n + rot) % 1.0) * len(acc)
                c = fx.mix(acc[int(seg) % len(acc)],
                           acc[(int(seg) + 1) % len(acc)], seg - int(seg))
                colors.append(fx.scale(c, breathe))
            rng = random.Random((id(dev) & 0xFFFF)
                                ^ (int(t * fx.hz(self.HZ_SPARK)) * 0x9E3779B1))
            for _ in range(fx.dens(1 + load * 5)):
                colors[rng.randrange(n)] = fx.lighten(pal.base("cpu"), 0.85)
        dev.set_colors([RGBColor(*(int(ch * self.brightness) for ch in c))
                        for c in colors], fast=True)

    def run(self) -> None:
        while not self.stop.is_set():
            if self.client is None and not self._connect():
                self.stop.wait(self.RECONNECT_S)
                continue
            view = self.view_source()
            pal = view.pal if view is not None else fx.PALETTES["spectrum"]
            t = time.monotonic() - self._t0
            v = fx.vitals()
            try:
                for dev in self.client.devices:
                    self._frame(dev, pal, t, v)
            except Exception as e:
                print(f"[caseled] frame failed: {e}", flush=True)
                try:
                    self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                continue
            self.stop.wait(self.interval)
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
