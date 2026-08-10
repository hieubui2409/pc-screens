"""The 'electric' theme — arcs, sparks and electrodes riding every meter.

Two views share one visual language via FXBase:
  * system-electric — four dense metric tiles (Lian Li's default)
  * clock-electric  — clock + owner card + meters (Leopard's default)
"""

from __future__ import annotations

import math
import os
import time
from collections import deque

from PIL import Image, ImageDraw, ImageFilter

from lianli88 import load_font
from fx import (FXBase, Palette, PALETTES, dens, hz, load_mono, sample_serial,
                scale, stats, vitals)

DEFAULT_PALETTE = PALETTES["spectrum"]


class ElectricMonitorView(FXBase):
    """System monitor with electrical arcs discharging along each meter.

    Built for animation: the static grid/bracket layer is rendered once and
    copied per frame, so the per-frame cost is only the moving parts. Arc
    amplitude, branch count and flicker all scale with the metric, so a busy
    CPU visibly crackles harder than an idle one.
    """

    n_tiles = 4
    HIST = 48

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or DEFAULT_PALETTE
        self._start(0xC0FFEE)
        self.hist_: dict[str, deque] = {}
        self._hist_serial = -1

        aspect = w / max(1, h)
        self.cols = 4 if aspect >= 2.5 else (2 if aspect >= 0.75 else 1)
        self.rows = -(-self.n_tiles // self.cols)
        self.cw, self.ch = w / self.cols, h / self.rows

        self.f_label = load_mono(max(10, int(self.ch * 0.085)))
        self.f_value = load_font(max(20, int(self.ch * 0.30)))
        self.f_unit = load_mono(max(10, int(self.ch * 0.10)))
        self.f_sub = load_mono(max(9, int(self.ch * 0.072)))
        self._bg = self._build_bg()

    def _build_bg(self) -> Image.Image:
        """Static layer: drawn once, copied every frame."""
        img = self._grid_bg(max(24, self.cw / 10))
        d = ImageDraw.Draw(img)
        sep = scale(self.pal.dim, 0.35)
        for r in range(1, self.rows):
            d.line([(self.w * 0.03, r * self.ch), (self.w - self.w * 0.03, r * self.ch)],
                   fill=sep, width=2)
        for c in range(1, self.cols):
            d.line([(c * self.cw, self.ch * 0.08), (c * self.cw, self.h - self.ch * 0.08)],
                   fill=sep, width=2)
        return img

    def _tile(self, img, d, t, ox: float, oy: float) -> None:
        base, hot = self.col(t.key), self.col(t.key, True)
        pad = self.cw * 0.085
        x = ox + pad
        avail = self.cw - pad * 2
        pct = max(0.0, min(1.0, t.pct))

        hist = self.hist_.setdefault(t.key, deque([pct] * self.HIST, maxlen=self.HIST))
        if self._fresh:
            hist.append(pct)
        # The bar chases the sample; the digits above it snap to it.
        bar_pct = self.ease(t.key, pct)

        # Fixed vertical bands. Overlaying the ribbon on the readout looked like
        # a rendering fault rather than a design, so each element owns its strip.
        y_label = oy + self.ch * 0.07
        y_value = oy + self.ch * 0.17
        rib_y = oy + self.ch * 0.44
        rib_h = self.ch * 0.24
        bh = self.ch * 0.075
        by = oy + self.ch * 0.77

        # label + animated activity pips
        d.text((x, y_label), t.label, font=self.f_label, fill=base)
        lw = d.textlength(t.label, font=self.f_label)
        self._pips(d, x + lw + self.cw * 0.035, y_label + self.f_label.size * 0.34,
                   self.f_label.size * 0.20, hot, 0)

        # big readout, with the secondary reading right-aligned on its baseline
        f_val = self.f_value
        while d.textlength(t.value, font=f_val) > avail * 0.58 and f_val.size > 16:
            f_val = load_font(int(f_val.size * 0.92))
        d.text((x, y_value), t.value, font=f_val, fill=self.pal.fg)
        d.text((x + d.textlength(t.value, font=f_val) + self.cw * 0.025,
                y_value + f_val.size - self.f_unit.size * 1.15),
               t.unit, font=self.f_unit, fill=base)
        if t.sub:
            # Shrink (and if still cramped, drop) the secondary reading rather
            # than let it collide with the unit suffix on a wide value like "5.8".
            unit_end = (x + d.textlength(t.value, font=f_val) + self.cw * 0.025
                        + d.textlength(t.unit, font=self.f_unit))
            limit = ox + self.cw - pad
            f_sub = self.f_sub
            while (f_sub.size > 9
                   and limit - d.textlength(t.sub, font=f_sub) < unit_end + self.cw * 0.05):
                f_sub = load_mono(int(f_sub.size * 0.9))
            sw = d.textlength(t.sub, font=f_sub)
            if limit - sw > unit_end + self.cw * 0.02:
                d.text((limit - sw, y_value + f_val.size - f_sub.size * 1.15),
                       t.sub, font=f_sub, fill=self.pal.dim)

        # history ribbon in its own strip
        step = avail / (self.HIST - 1)
        pts = [(x + i * step, rib_y + rib_h - v * rib_h) for i, v in enumerate(hist)]
        self.ribbon(img, pts, base, baseline=rib_y + rib_h)
        d = ImageDraw.Draw(img)
        d.line([(x, rib_y + rib_h), (x + avail, rib_y + rib_h)],
               fill=scale(self.pal.dim, 0.42), width=2)

        # --- the meter, and the discharge riding it ---
        self._electric_bar(d, x, by, avail, bh, bar_pct, base, hot)

        # ticks under the meter
        for i in range(11):
            tx = x + avail * i / 10
            on = i / 10 <= bar_pct
            d.line([(tx, by + bh + 6), (tx, by + bh + (16 if i % 5 == 0 else 10))],
                   fill=base if on else scale(self.pal.dim, 0.5), width=2)

    def render(self) -> Image.Image:
        self._tick()
        img = self._bg.copy().convert("RGBA")
        d = ImageDraw.Draw(img)

        tiles = stats().tiles
        serial = sample_serial("stats")
        self._fresh = serial != self._hist_serial
        self._hist_serial = serial
        for i, t in enumerate(tiles[: self.cols * self.rows]):
            self._tile(img, d, t,
                       (i % self.cols) * self.cw, (i // self.cols) * self.ch)
            d = ImageDraw.Draw(img)

        self._scanline(img)
        return img.convert("RGB")


class ElectricClockView(FXBase):
    """Clock + owner card + meters in the same electric language as
    `system-electric`, so both panels read as one system.

    Shares FXBase with the Lian Li view: identical arcs, electrodes,
    sparks, pips and sweep. Only the content differs.
    """

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or DEFAULT_PALETTE
        self._start(0x5EED)
        opts = opts or {}
        self.owner_name = opts.get("owner_name") or ""
        self.owner_role = opts.get("owner_role") or ""
        self.owner_date = opts.get("owner_date") or ""
        self.host = os.uname().nodename.upper()[:14]

        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        size = int(min(w * 0.30, h * 0.20))
        while size > 14 and probe.textlength("00:00", font=load_mono(size)) > w * 0.86:
            size = int(size * 0.93)
        self.f_time = load_mono(size)
        self.f_date = load_mono(max(9, int(size * 0.20)))
        self.f_small = load_mono(max(8, int(size * 0.135)))
        self.f_name = load_font(max(11, int(size * 0.30)))
        self.f_meta = load_mono(max(8, int(size * 0.145)))
        self._bg = self._grid_bg(max(24, w / 8))

    def render(self) -> Image.Image:
        self._tick()
        w, h = self.w, self.h
        img = self._bg.copy().convert("RGBA")
        d = ImageDraw.Draw(img)
        cy_, cyh = self.col("cpu"), self.col("cpu", True)
        lm, lmh = self.col("gpu"), self.col("gpu", True)
        mg, mgh = self.col("ssd"), self.col("ssd", True)
        sep = scale(self.pal.dim, 0.35)
        pad = w * 0.09

        now = time.localtime()
        v = vitals()
        gpu = v.gpu

        # --- status strip ---
        ty = h * 0.05
        self._pips(d, pad, ty + self.f_small.size * 0.30, self.f_small.size * 0.38, lmh, 0)
        d.text((pad + self.f_small.size * 2.4, ty), "SYS ONLINE",
               font=self.f_small, fill=lm)
        hw = d.textlength(self.host, font=self.f_small)
        d.text((w - pad - hw, ty), self.host, font=self.f_small, fill=self.pal.dim)
        ry = ty + self.f_small.size * 1.9
        d.line([(pad, ry), (w - pad, ry)], fill=sep, width=2)

        # --- headline clock, wrapped in discharge ---
        hhmm = time.strftime("%H:%M", now)
        tw = d.textlength(hhmm, font=self.f_time)
        tx, tyy = (w - tw) / 2, h * 0.125
        self.glow_text(img, hhmm, self.f_time, (tx, tyy), cy_)
        d = ImageDraw.Draw(img)
        d.text((tx, tyy), hhmm, font=self.f_time, fill=self.pal.fg)
        dim = scale(cy_, 0.62)
        for edge, prob in ((0.0, 1.0), (1.0, 0.55)):
            if self.rng.random() > prob:
                continue
            ay = tyy + (self.f_time.size * 1.02 if edge else -h * 0.012)
            self._bolt(d, pad, ay, w - pad, ay, h * 0.011, dim, cy_, segs=9, width=1)

        # --- seconds as an electric track ---
        # Fractional, so the electrode glides across the minute instead of
        # stepping once a second. This is the one readout that should be
        # continuous: it is the clock, not a sampled measurement.
        sec = time.time() % 60.0
        sy = tyy + self.f_time.size * 1.16
        sbh = h * 0.024
        self._electric_bar(d, pad, sy, w - pad * 2, sbh, sec / 60.0, cy_, cyh)
        d.text((pad, sy + sbh * 1.9), f"{int(sec):02d}s", font=self.f_small, fill=cy_)
        lbl = time.strftime("%d/%m/%Y", now)
        d.text((w - pad - d.textlength(lbl, font=self.f_small), sy + sbh * 1.9),
               lbl, font=self.f_small, fill=self.pal.dim)

        # --- date ---
        dy = sy + sbh + h * 0.055
        self.spaced_text(d, time.strftime("%A", now).upper(), self.f_date, dy,
                         self.pal.fg, w * 0.012)

        # --- owner badge, brackets crackling ---
        by = dy + self.f_date.size * 2.0
        if self.owner_name or self.owner_role or self.owner_date:
            nw = d.textlength(self.owner_name, font=self.f_name)
            bx0, bx1 = (w - nw) / 2 - w * 0.075, (w + nw) / 2 + w * 0.075
            bh = self.f_name.size * 1.5
            for x0, s in ((bx0, 1), (bx1, -1)):
                d.line([(x0 + w * 0.022 * s, by), (x0, by), (x0, by + bh),
                        (x0 + w * 0.022 * s, by + bh)], fill=mg, width=5)
                if self.rng.random() < 0.5:
                    self._sparks(d, x0, by + bh * self.rng.random(), bh * 0.16, mgh, 3)
            d.text(((w - nw) / 2, by + bh * 0.5 - self.f_name.size * 0.62),
                   self.owner_name, font=self.f_name, fill=self.pal.fg)
            meta = "  //  ".join(x for x in (self.owner_role, self.owner_date) if x)
            if meta:
                f = self.f_meta
                while d.textlength(meta, font=f) > w * 0.9 and f.size > 8:
                    f = load_mono(int(f.size * 0.92))
                d.text(((w - d.textlength(meta, font=f)) / 2, by + bh + h * 0.012),
                       meta, font=f, fill=mg)
                by += h * 0.012 + f.size
            by += bh
        by += h * 0.05

        # --- electric meters, columns right-aligned on shared anchors ---
        fy = h - h * 0.055 - self.f_small.size
        rule_y = fy - self.f_small.size * 0.8
        rows = [("CPU", v.load / 100, f"{v.load:.0f}%",
                 f"{v.temp:.0f}°C" if v.temp else "", cy_, cyh)]
        if gpu:
            rows.append(("GPU", gpu["util"] / 100, f"{gpu['util']:.0f}%",
                         f"{gpu['temp']:.0f}°C", lm, lmh))
        rows.append(("MEM", v.mem_pct / 100, f"{v.mem_pct:.0f}%",
                     f"{v.mem_used:.0f}G", mg, mgh))

        mbh = h * 0.024
        col_gap = self.f_small.size * 1.1
        detail_w = max(d.textlength(r[3], font=self.f_small) for r in rows)
        pct_right = w - pad - detail_w - (col_gap if detail_w else 0)
        slot = max(self.f_small.size * 1.7 + mbh, (rule_y - h * 0.025 - by) / len(rows))

        for i, (label, pct, pct_txt, detail, base, hot) in enumerate(rows):
            my = by + i * slot
            d.text((pad, my), label, font=self.f_small, fill=base)
            lw = d.textlength(label, font=self.f_small)
            self._pips(d, pad + lw + w * 0.02, my + self.f_small.size * 0.34,
                       self.f_small.size * 0.30, hot, i, n=2)
            d.text((pct_right - d.textlength(pct_txt, font=self.f_small), my),
                   pct_txt, font=self.f_small, fill=self.pal.fg)
            if detail:
                d.text((w - pad - d.textlength(detail, font=self.f_small), my),
                       detail, font=self.f_small, fill=self.pal.dim)
            self._electric_bar(d, pad, my + self.f_small.size * 1.45,
                               w - pad * 2, mbh, self.ease(label, pct), base, hot)

        # --- footer ---
        d.line([(pad, rule_y), (w - pad, rule_y)], fill=sep, width=2)
        up = v.uptime
        d.text((pad, fy), f"UP {up // 86400}D {up % 86400 // 3600:02d}H {up % 3600 // 60:02d}M",
               font=self.f_small, fill=self.pal.dim)
        step = int(self.t * hz(self.HZ_MARK))
        marker = "".join("█" if (step + i) % 6 < 3 else "░" for i in range(6))
        d.text((w - pad - d.textlength(marker, font=self.f_small), fy),
               marker, font=self.f_small, fill=cy_)

        self._scanline(img)
        return img.convert("RGB")


VIEWS = {
    "system-electric": ElectricMonitorView,
    "clock-electric": ElectricClockView,
}
