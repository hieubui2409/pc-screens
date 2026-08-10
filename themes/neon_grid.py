"""neon-grid — the Tron theme (vertical).

A perspective grid floor scrolls toward the horizon at a speed driven by CPU
load, with a light cycle streaking down a random lane. Above it: glowing clock,
light-trail seconds, a per-core heat grid, four trail-bar meters with history
ribbons, and a network panel. Signature colours are Tron cyan/orange; --palette
recolours everything.

This file is also the reference for the other themes: flow layout from a
y-cursor (so 480x1920 and 480x960 both work), fonts probed against the canvas,
every cadence wall-clock based via FXBase, numbers on the sample clock.
"""

from __future__ import annotations

import math
import os
import time

from PIL import Image, ImageDraw, ImageFilter

from lianli88 import load_font
from fx import (FXBase, Palette, dens, human_rate, hz, load_mono, mix, scale,
                vitals)

TRON = Palette("tron",
               ((0, 229, 255), (255, 157, 46), (140, 255, 230), (96, 144, 255)),
               bg=(2, 6, 14), grid=(10, 30, 48), dim=(126, 156, 178))


class NeonGridView(FXBase):
    HZ_CYCLE = 0.30      # light-cycle runs per second
    FLOOR_ROWS = 9       # horizontal grid lines on the floor
    FLOOR_BASE = 0.55    # floor rows scrolled per second at idle...
    FLOOR_LOAD = 2.2     # ...plus this much at 100% CPU

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or TRON
        self._start(0x7120)
        self.host = os.uname().nodename.upper()[:14]

        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        size = int(min(w * 0.34, h * 0.14))
        while size > 14 and probe.textlength("00:00", font=load_mono(size)) > w * 0.84:
            size = int(size * 0.93)
        self.f_time = load_mono(size)
        self.f_big = load_font(max(16, int(size * 0.52)))
        self.f_label = load_mono(max(9, int(size * 0.155)))
        self.f_small = load_mono(max(8, int(size * 0.125)))
        self._bg = self._build_bg()
        self._clock_key = ""
        self._clock_strip = None

    # --- static layer ---------------------------------------------------------
    def _build_bg(self) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), self.pal.bg)
        d = ImageDraw.Draw(img)
        cy = self.pal.base("cpu")
        # faint sky grid, denser than the floor so the floor reads as ground
        step = max(22, self.w / 12)
        y = 0.0
        while y < self.h:
            d.line([(0, y), (self.w, y)], fill=self.pal.grid, width=1)
            y += step
        x = 0.0
        while x < self.w:
            d.line([(x, 0), (x, self.h)], fill=self.pal.grid, width=1)
            x += step
        # neon side rails with corner brackets — the arena wall
        m = self.w * 0.018
        d.line([(m, 0), (m, self.h)], fill=scale(cy, 0.55), width=2)
        d.line([(self.w - m, 0), (self.w - m, self.h)], fill=scale(cy, 0.55), width=2)
        ln = self.w * 0.10
        for cx, cyy, sx, sy in ((m, m, 1, 1), (self.w - m, m, -1, 1),
                                (m, self.h - m, 1, -1), (self.w - m, self.h - m, -1, -1)):
            d.line([(cx, cyy), (cx + ln * sx, cyy)], fill=cy, width=4)
            d.line([(cx, cyy), (cx, cyy + ln * sy)], fill=cy, width=4)
        return img

    # --- widgets --------------------------------------------------------------
    def _trail_bar(self, d, x, y, bw, bh, frac, base, hot):
        """Meter as a light-cycle trail: gradient tail, hard bright head."""
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=bh / 2,
                            fill=(8, 16, 26), outline=scale(base, 0.4))
        frac = max(0.0, min(1.0, frac))
        fw = max(bh, bw * frac)
        n = max(6, int(fw / 7))
        for i in range(n):
            t = i / n
            xx = x + fw * t
            d.rectangle([xx, y + 1.5, min(x + fw, xx + fw / n + 1), y + bh - 1.5],
                        fill=mix(scale(base, 0.18), base, t ** 1.6))
        # the head: a hot chip with a spark of exhaust behind it
        hx = x + fw
        d.rectangle([hx - bh * 0.7, y - bh * 0.28, hx + bh * 0.28, y + bh * 1.28],
                    fill=hot)
        if self.rng.random() < 0.6:
            self._sparks(d, hx, y + bh / 2, bh * 0.5, hot, dens(3))

    def _core_grid(self, d, x, y, avail, cores, base, hot):
        """One cell per CPU core, coloured by that core's load."""
        n = max(1, len(cores))
        cols = 10 if n > 10 else n
        rows = -(-n // cols)
        gap = avail * 0.012
        cw = (avail - gap * (cols - 1)) / cols
        chh = cw * 0.62
        for i, load in enumerate(cores):
            cx = x + (i % cols) * (cw + gap)
            cyy = y + (i // cols) * (chh + gap)
            f = max(0.0, min(1.0, load / 100))
            col = mix(scale(base, 0.22), base, f)
            d.rectangle([cx, cyy, cx + cw, cyy + chh], fill=col)
            if f > 0.8:                      # overdriven core flares hot
                d.rectangle([cx, cyy, cx + cw, cyy + chh], outline=hot, width=1)
        return rows * (chh + gap) - gap

    def _floor(self, img, d, y0, load_frac, base, alt, hot):
        """Scrolling perspective grid with a light cycle on a random lane."""
        h, w = self.h, self.w
        fh = h - y0
        vpx = w / 2

        # horizon glow
        d.line([(0, y0), (w, y0)], fill=hot, width=2)
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.line([(0, y0), (w, y0)], fill=tuple(base) + (120,), width=8)
        img.alpha_composite(glow)
        d = ImageDraw.Draw(img)

        # converging lane lines
        lanes = []
        for k in range(-6, 7):
            xb = vpx + k * (w * 0.17)
            d.line([(vpx, y0), (xb, h)],
                   fill=scale(base, 0.55 if k % 3 == 0 else 0.3),
                   width=2 if k % 3 == 0 else 1)
            lanes.append(xb)

        # scrolling cross lines — speed rides CPU load, eased so it glides
        speed = self.FLOOR_BASE + self.FLOOR_LOAD * load_frac
        phase = (self.t * hz(speed)) % 1.0
        for i in range(self.FLOOR_ROWS):
            z = ((i + phase) / self.FLOOR_ROWS)
            yy = y0 + fh * (z ** 2.4)
            d.line([(0, yy), (w, yy)],
                   fill=mix(self.pal.bg, base, 0.15 + 0.75 * z),
                   width=1 + int(z * 2.2))

        # the light cycle: one run per 1/HZ_CYCLE window, lane frozen per run
        run = self.wrng(self.HZ_CYCLE, salt=0xC1C)
        lane = lanes[run.randrange(2, len(lanes) - 2)]
        prog = (self.t * hz(self.HZ_CYCLE)) % 1.0
        zt = prog ** 2.2
        px = vpx + (lane - vpx) * zt
        py = y0 + fh * zt
        # exhaust trail back toward the horizon
        tail = []
        for k in range(7):
            zk = max(0.0, zt - k * 0.05)
            tail.append((vpx + (lane - vpx) * zk, y0 + fh * zk))
        d.line(tail, fill=alt, width=2)
        r = 2 + 5 * zt
        d.ellipse([px - r, py - r, px + r, py + r], fill=self.pal.hot("gpu"))
        self._sparks(d, px, py, r, alt, dens(2 + int(4 * zt)))

    # --- frame ----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        w, h = self.w, self.h
        img = self._bg.copy().convert("RGBA")
        d = ImageDraw.Draw(img)
        cy, cyh = self.col("cpu"), self.col("cpu", True)
        og, ogh = self.col("gpu"), self.col("gpu", True)
        aq = self.col("ram")
        pad = w * 0.085
        avail = w - pad * 2

        now = time.localtime()
        v = vitals()
        floor_y = h * 0.865

        # --- header ---
        y = h * 0.016 + self.w * 0.018
        self._pips(d, pad, y + self.f_small.size * 0.28, self.f_small.size * 0.38,
                   ogh, 0, dark=scale(self.pal.dim, 0.3))
        d.text((pad + self.f_small.size * 2.3, y), "GRID LINK", font=self.f_small, fill=og)
        hw = d.textlength(self.host, font=self.f_small)
        d.text((w - pad - hw, y), self.host, font=self.f_small, fill=self.pal.dim)
        y += self.f_small.size * 1.8
        d.line([(pad, y), (w - pad, y)], fill=scale(cy, 0.5), width=2)
        y += h * 0.012

        # --- clock ---
        # Glow blurred on a cached strip, once per minute — a full-canvas
        # GaussianBlur per frame costs more than the rest of the render.
        hhmm = time.strftime("%H:%M", now)
        if self._clock_key != hhmm:
            self._clock_key = hhmm
            tw = d.textlength(hhmm, font=self.f_time)
            strip = Image.new("RGBA", (w, int(self.f_time.size * 1.3)), (0, 0, 0, 0))
            sd = ImageDraw.Draw(strip)
            sd.text(((w - tw) / 2, 0), hhmm, font=self.f_time,
                    fill=tuple(cy) + (215,))
            strip = strip.filter(ImageFilter.GaussianBlur(self.f_time.size * 0.07))
            ImageDraw.Draw(strip).text(((w - tw) / 2, 0), hhmm,
                                       font=self.f_time, fill=self.pal.fg)
            self._clock_strip = strip
        img.alpha_composite(self._clock_strip, (0, int(y)))
        y += self.f_time.size * 1.14

        # seconds as a light trail across the minute (fractional -> glides)
        sec = time.time() % 60.0
        sbh = h * 0.010
        self._trail_bar(d, pad, y, avail, sbh, sec / 60.0, cy, cyh)
        y += sbh + self.f_small.size * 0.5
        d.text((pad, y), f"{int(sec):02d}s", font=self.f_small, fill=cy)
        date = time.strftime("%a %d/%m/%Y", now).upper()
        d.text((w - pad - d.textlength(date, font=self.f_small), y),
               date, font=self.f_small, fill=self.pal.dim)
        y += self.f_small.size * 1.9

        # --- per-core heat grid ---
        d.text((pad, y), f"CORES {len(v.per_core)}", font=self.f_label, fill=aq)
        ghz = f"{v.freq_ghz:.1f} GHZ"
        d.text((w - pad - d.textlength(ghz, font=self.f_label), y),
               ghz, font=self.f_label, fill=self.pal.dim)
        y += self.f_label.size * 1.5
        y += self._core_grid(d, pad, y, avail, v.per_core, cy, cyh) + h * 0.016

        # --- four trail-bar meters, sized to the space above the net panel ---
        gpu = v.gpu
        load1 = os.getloadavg()[0]
        rd, ru = human_rate(v.disk_rd)
        wr_, wu = human_rate(v.disk_wr)
        rows = [("CPU", v.load / 100, f"{v.load:.0f}%",
                 f"{v.temp:.0f}°C" if v.temp else "", "cpu",
                 f"LOADAVG {load1:.2f}  ·  {v.freq_ghz:.1f} GHZ")]
        if gpu:
            rows.append(("GPU", gpu["util"] / 100, f"{gpu['util']:.0f}%",
                         f"{gpu['temp']:.0f}°C {gpu['power']:.0f}W", "gpu",
                         f"VRAM {gpu['mem_used']/1024:.1f}/{gpu['mem_total']/1024:.0f}G"))
        rows.append(("RAM", v.mem_pct / 100, f"{v.mem_used:.1f}G",
                     f"of {v.mem_total:.0f}G", "ram",
                     f"{v.mem_pct:.0f}% COMMITTED"))
        rows.append(("SSD", v.disk_pct / 100, f"{v.disk_pct:.0f}%",
                     f"{v.nvme_temp:.0f}°C" if v.nvme_temp else "", "ssd",
                     f"R {rd}{ru}  ·  W {wr_}{wu}"))

        net_h = self.f_label.size * 3.4
        bh = h * 0.011
        slot = (floor_y - net_h - h * 0.02 - y) / len(rows)
        for label, frac, val, sub, key, detail in rows:
            base, hot = self.col(key), self.col(key, True)
            d.text((pad, y), label, font=self.f_label, fill=base)
            lw = d.textlength(label, font=self.f_label)
            self._pips(d, pad + lw + w * 0.02, y + self.f_label.size * 0.34,
                       self.f_label.size * 0.22, hot, 0,
                       dark=scale(self.pal.dim, 0.3))
            if sub:
                d.text((w - pad - d.textlength(sub, font=self.f_small),
                        y + self.f_label.size - self.f_small.size), sub,
                       font=self.f_small, fill=self.pal.dim)
            vy = y + self.f_label.size * 1.30
            # The value grows with the slot, so a tall canvas gets the huge
            # readouts; the ribbon fills the column to its right.
            f_val = load_font(max(16, int(min(slot * 0.40,
                                              self.f_time.size * 0.62))))
            f_val = self.fit_text(d, val, f_val, avail * 0.40, floor=16)
            d.text((pad, vy), val, font=f_val, fill=self.pal.fg)
            rx = pad + avail * 0.46
            rib_h = f_val.size * 1.02
            histo = self.hist(f"ng:{label}", frac, n=40)
            step = (avail * 0.54) / (len(histo) - 1)
            pts = [(rx + i * step, vy + rib_h - vv * rib_h * 0.94)
                   for i, vv in enumerate(histo)]
            # Opaque pre-mixed fill: FXBase.ribbon composites a full-canvas
            # RGBA layer per call, which at four calls a frame dominates the
            # budget. Same look on a dark ground at a fraction of the cost.
            d.polygon([(rx, vy + rib_h)] + pts + [(rx + avail * 0.54, vy + rib_h)],
                      fill=mix(self.pal.bg, base, 0.27))
            d.line(pts, fill=base, width=2)
            d.line([(rx, vy + rib_h), (rx + avail * 0.54, vy + rib_h)],
                   fill=scale(base, 0.45), width=1)
            by = vy + rib_h + bh * 1.2
            self._trail_bar(d, pad, by, avail, bh,
                            self.ease(label, frac), base, hot)
            # ticks under the bar
            for i in range(11):
                tx = pad + avail * i / 10
                on = i / 10 <= frac
                d.line([(tx, by + bh + 5), (tx, by + bh + (13 if i % 5 == 0 else 8))],
                       fill=base if on else scale(self.pal.dim, 0.4), width=2)
            # a tall canvas has room for a secondary telemetry line
            used = by + bh + 15 - y
            if slot - used > self.f_small.size * 2.4:
                d.text((pad, by + bh + self.f_small.size * 1.4), detail,
                       font=self.f_small, fill=self.pal.dim)
            y += slot

        # --- network panel ---
        d.line([(pad, y), (w - pad, y)], fill=scale(cy, 0.5), width=1)
        y += h * 0.008
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        d.text((pad, y), "▼ " + dv, font=self.f_label, fill=cy)
        d.text((pad + d.textlength("▼ " + dv, font=self.f_label) + 4,
                y + self.f_label.size - self.f_small.size),
               du, font=self.f_small, fill=self.pal.dim)
        upx = pad + avail * 0.52
        d.text((upx, y), "▲ " + uv, font=self.f_label, fill=og)
        d.text((upx + d.textlength("▲ " + uv, font=self.f_label) + 4,
                y + self.f_label.size - self.f_small.size),
               uu, font=self.f_small, fill=self.pal.dim)
        y += self.f_label.size * 1.45
        up = v.uptime
        d.text((pad, y), f"UP {up // 86400}D {up % 86400 // 3600:02d}H "
                         f"{up % 3600 // 60:02d}M", font=self.f_small,
               fill=self.pal.dim)
        pr = f"{v.procs} PROCS"
        d.text((w - pad - d.textlength(pr, font=self.f_small), y),
               pr, font=self.f_small, fill=self.pal.dim)

        # --- the floor ---
        self._floor(img, d, floor_y, self.ease("floor", v.load / 100), cy, og, cyh)

        self._scanline(img, colour=cy)
        return img.convert("RGB")


VIEWS = {"neon-grid": NeonGridView}
