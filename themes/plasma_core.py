"""plasma-core — fusion reactor control room (horizontal).

A molten plasma orb breathes inside a containment ring that doubles as the
CPU-load gauge. Embers rise off the core, heat-haze shimmers above it, and
thermal console panels flank it: this theme is about temperature, so hot
readouts flare white and blink. Built for 1920x480 and 960x480.

Signature palette is molten amber; --palette recolours everything.
"""

from __future__ import annotations

import math
import os
import random
import time

from PIL import Image, ImageDraw, ImageFilter

from lianli88 import load_font
from fx import (FXBase, Palette, dens, human_rate, hz, lighten, load_mono,
                mix, scale, vitals)

PLASMA = Palette("plasma",
                 ((255, 94, 46), (255, 177, 74), (255, 243, 196), (255, 120, 90)),
                 bg=(12, 5, 3), grid=(36, 19, 11),
                 fg=(255, 241, 226), dim=(186, 143, 112))

TEMP_WARN = 85.0     # °C — white-hot blink threshold
PCT_WARN = 90.0      # %  — same treatment for capacity gauges
CHART_LO, CHART_HI = 30.0, 100.0


class PlasmaCoreView(FXBase):
    HZ_BREATHE = 0.28    # slow core pulse, cycles/s
    HZ_FLICK = 9.0       # fast shallow flicker windows/s
    HZ_BOLT = 2.4        # discharge decision windows/s
    HZ_LAMP = 1.2        # header status lamp blink
    RODS = 20

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or PLASMA
        self._start(0xF7A5)
        self.host = os.uname().nodename.upper()[:14]
        self.wide = w / max(1, h) >= 3.0
        self._ct = 45.0          # last known temps, so the chart never drops
        self._gt = 45.0

        # --- geometry: header band / main band (core + consoles) / bottom band
        self.pad = max(10, w * 0.012)
        self.header_h = max(34, h * 0.105)
        bot_h = h * 0.295
        self.main_y0 = self.header_h + h * 0.016
        self.main_y1 = h - bot_h - h * 0.014
        main_h = self.main_y1 - self.main_y0
        self.ring_r = min(main_h * 0.40, w * 0.085)
        self.ring_w = max(5, int(self.ring_r * 0.10))
        tickpad = self.ring_r * 0.26
        gap = max(8, w * 0.006)

        # core is left-of-centre; at 960 the CPU console moves to the right side
        if self.wide:
            left_w = w * 0.165
            self.core_cx = self.pad + left_w + gap + tickpad + self.ring_r
            self.p_cpu = (self.pad, self.main_y0, self.pad + left_w, self.main_y1)
        else:
            self.core_cx = self.pad + tickpad + self.ring_r + 4
        self.core_cy = (self.main_y0 + self.main_y1) / 2

        rx0 = self.core_cx + self.ring_r + tickpad + gap
        cw = (w - self.pad - rx0 - gap * 2) / 3

        def colrect(i):
            x = rx0 + i * (cw + gap)
            return (x, self.main_y0, x + cw, self.main_y1)

        if self.wide:
            self.p_gpu, self.p_mem, self.p_net = colrect(0), colrect(1), colrect(2)
        else:
            self.p_cpu, self.p_gpu, self.p_mem = colrect(0), colrect(1), colrect(2)
            self.p_net = None            # net/sys rows fold into the mem panel

        by0 = h - bot_h + h * 0.008
        by1 = h - self.pad * 0.7
        split = self.pad + (w - 2 * self.pad) * (0.56 if self.wide else 0.52)
        self.p_chart = (self.pad, by0, split - gap / 2, by1)
        self.p_rods = (split + gap / 2, by0, w - self.pad, by1)

        # --- fonts (heights track h; both targets are 480 tall)
        self.f_clock = load_mono(max(14, int(self.header_h * 0.52)))
        self.f_title = load_mono(max(9, int(h * 0.027)))
        self.f_big = load_font(max(18, int(h * 0.112)))
        self.f_med = load_font(max(13, int(h * 0.060)))
        self.f_label = load_mono(max(8, int(h * 0.024)))
        self.f_small = load_mono(max(8, int(h * 0.021)))

        # --- orb sprites: three colour temperatures, blurred ONCE here.
        # Per frame we only Image.blend two of them and resize — no filtering.
        ds = max(96, int(self.ring_r * 2.9))
        c0, c1, c2 = (self.pal.base("cpu"), self.pal.base("gpu"),
                      self.pal.base("ram"))
        self.orbs = [
            self._make_orb(scale(c0, 0.50), lighten(c0, 0.22), ds),
            self._make_orb(scale(mix(c0, c1, 0.6), 0.62), lighten(c1, 0.35), ds),
            self._make_orb(lighten(c1, 0.35), lighten(mix(c1, c2, 0.6), 0.92), ds),
        ]
        self._bg = self._build_bg()

    # --- prebuilt assets ------------------------------------------------------
    def _make_orb(self, outer, inner, ds: int) -> Image.Image:
        """Radial-gradient plasma ball. ImageDraw overwrites alpha, so painting
        concentric discs from halo inward produces the ramp directly."""
        img = Image.new("RGBA", (ds, ds), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        R = ds / 2
        steps = 36
        for i in range(steps):
            t = i / (steps - 1)                  # 0 = halo edge, 1 = centre
            r = R * (1 - 0.93 * t)
            col = mix(outer, inner, min(1.0, t * 1.5))
            a = 255 if t > 0.62 else int(8 + 247 * (t / 0.62) ** 2.1)
            d.ellipse([R - r, R - r, R + r, R + r], fill=tuple(col) + (a,))
        return img.filter(ImageFilter.GaussianBlur(ds * 0.035))

    def _frame(self, d, rect, title, key) -> None:
        x0, y0, x1, y1 = rect
        base = self.pal.base(key)
        d.rounded_rectangle(rect, radius=6, outline=scale(base, 0.45), width=1)
        tw = d.textlength(title, font=self.f_title)
        d.rectangle([x0 + 9, y0 - 2, x0 + 19 + tw, y0 + 2], fill=self.pal.bg)
        d.text((x0 + 14, y0 - self.f_title.size * 0.55), title,
               font=self.f_title, fill=base)
        for cx_, sx in ((x0, 1), (x1, -1)):
            d.line([(cx_ + 1 * sx, y1 - 10), (cx_ + 1 * sx, y1 - 1),
                    (cx_ + 11 * sx, y1 - 1)], fill=scale(base, 0.8), width=2)

    def _chart_y(self, temp: float) -> float:
        x0, y0, x1, y1 = self.p_chart
        top, bot = y0 + self.f_title.size * 1.1, y1 - self.f_small.size * 1.6
        f = (min(CHART_HI, max(CHART_LO, temp)) - CHART_LO) / (CHART_HI - CHART_LO)
        return bot - f * (bot - top)

    def _build_bg(self) -> Image.Image:
        cpu = self.pal.base("cpu")
        img = self._grid_bg(max(24, self.h / 9))
        d = ImageDraw.Draw(img)

        # header band + double rule
        d.rectangle([0, 0, self.w, self.header_h], fill=mix(self.pal.bg, cpu, 0.05))
        d.line([(0, self.header_h), (self.w, self.header_h)],
               fill=scale(cpu, 0.60), width=2)
        d.line([(0, self.header_h + 3), (self.w, self.header_h + 3)],
               fill=scale(cpu, 0.25), width=1)
        hw = d.textlength(self.host, font=self.f_label)
        d.text((self.w - self.pad - hw, (self.header_h - self.f_label.size) / 2),
               self.host, font=self.f_label, fill=self.pal.dim)

        # console frames
        self._frame(d, self.p_cpu, "CPU CORE", "cpu")
        self._frame(d, self.p_gpu, "GPU", "gpu")
        self._frame(d, self.p_mem, "MEM · STORAGE" if self.p_net else
                    "MEM · STO · NET", "ram")
        if self.p_net:
            self._frame(d, self.p_net, "NETWORK · SYS", "ssd")

        # containment ring tick marks (static; the arc itself moves per frame)
        cx, cy = self.core_cx, self.core_cy
        for i in range(25):
            a = math.radians(120 + 300 * i / 24)
            major = i % 4 == 0
            r0 = self.ring_r + self.ring_w * 1.1
            r1 = r0 + (9 if major else 5)
            d.line([(cx + math.cos(a) * r0, cy + math.sin(a) * r0),
                    (cx + math.cos(a) * r1, cy + math.sin(a) * r1)],
                   fill=scale(cpu, 0.75 if major else 0.42), width=2 if major else 1)

        # thermal chart frame, warning zone above 80°C, gridlines
        self._frame(d, self.p_chart, "THERMAL HISTORY °C", "cpu")
        x0, y0, x1, y1 = self.p_chart
        gx0, gx1 = x0 + self.f_small.size * 2.6, x1 - 8
        warn = self.pal.base("ssd")
        yw = self._chart_y(80)
        ytop = self._chart_y(CHART_HI)
        d.rectangle([gx0, ytop, gx1, yw], fill=mix(self.pal.bg, warn, 0.13))
        xx = gx0
        while xx < gx1:                          # dashed 80° line
            d.line([(xx, yw), (min(gx1, xx + 6), yw)], fill=scale(warn, 0.8), width=1)
            xx += 12
        for tval in (40, 60, 80, 100):
            yy = self._chart_y(tval)
            if tval != 80:
                d.line([(gx0, yy), (gx1, yy)], fill=scale(self.pal.dim, 0.28), width=1)
            d.text((x0 + 5, yy - self.f_small.size * 0.5), f"{tval}",
                   font=self.f_small, fill=scale(self.pal.dim, 0.8))
        self._chart_box = (gx0, ytop, gx1, self._chart_y(CHART_LO))

        # fuel-rod slots
        self._frame(d, self.p_rods, f"FUEL RODS · {self.RODS} CORES", "gpu")
        x0, y0, x1, y1 = self.p_rods
        rail_y = y0 + self.f_title.size * 1.2
        d.line([(x0 + 8, rail_y), (x1 - 8, rail_y)], fill=scale(cpu, 0.55), width=2)
        span = (x1 - x0 - 20) / self.RODS
        self._rod_geom = (x0 + 10, rail_y + 3, span, y1 - 8 - (rail_y + 3))
        for i in range(self.RODS):
            rx = x0 + 10 + i * span + span * 0.22
            d.rectangle([rx, rail_y + 3, rx + span * 0.56, y1 - 8],
                        outline=scale(self.pal.dim, 0.30), width=1)
        return img.convert("RGBA")

    # --- per-frame helpers ----------------------------------------------------
    def _tcol(self, val, warn, key):
        """Thermal colour ladder; above `warn` it blinks white-hot at ~3 Hz
        wall-clock (deliberately not anim-scaled: it is an alarm)."""
        if val is None:
            return self.pal.dim
        if val >= warn:
            return lighten(self.pal.base(key), 0.92) if (self.t * 3.0) % 1.0 < 0.55 \
                else self.pal.hot(key)
        if val >= warn - 8:
            return self.pal.hot(key)
        return self.pal.fg

    def _rbolt(self, d, cx, cy, ang, r0, r1, amp, colour, hot, rng):
        """Radial discharge with perpendicular jitter (FXBase._bolt only
        jitters y, which flattens near-vertical strikes)."""
        ca, sa = math.cos(ang), math.sin(ang)
        pts = []
        segs = 7
        for i in range(segs + 1):
            t = i / segs
            rr = r0 + (r1 - r0) * t
            off = rng.uniform(amp * 0.4, amp) * (1 if i % 2 else -1) \
                * math.sin(math.pi * t)
            pts.append((cx + ca * rr - sa * off, cy + sa * rr + ca * off))
        d.line(pts, fill=colour, width=4, joint="curve")
        d.line(pts, fill=hot, width=2, joint="curve")

    def _ribbon(self, d, pts, base, baseline):
        """Area-filled history line. FXBase.ribbon composites a full-canvas
        RGBA layer per call; three of those per frame blew the 18 ms budget,
        and over a near-black panel a pre-mixed opaque fill looks the same."""
        d.polygon([(pts[0][0], baseline)] + list(pts) + [(pts[-1][0], baseline)],
                  fill=mix(self.pal.bg, base, 0.24))
        d.line(pts, fill=base, width=2)
        d.line([(pts[0][0], baseline), (pts[-1][0], baseline)],
               fill=scale(base, 0.45), width=1)

    def _lv(self, d, x0, x1, y, label, value, vcol, f=None):
        """Label left / value right row; returns next y."""
        f = f or self.f_label
        d.text((x0, y + f.size - self.f_label.size), label,
               font=self.f_label, fill=self.pal.dim)
        d.text((x1 - d.textlength(value, font=f), y), value, font=f, fill=vcol)
        return y + f.size * 1.30

    def _bignum(self, d, x, y, label, num, unit, ncol, extra=""):
        d.text((x, y), label, font=self.f_label, fill=self.pal.dim)
        y += self.f_label.size * 1.22
        d.text((x, y), num, font=self.f_big, fill=ncol)
        nx = x + d.textlength(num, font=self.f_big) + 6
        d.text((nx, y + self.f_big.size - self.f_med.size * 1.15), unit,
               font=self.f_med, fill=scale(ncol, 0.8) if ncol != self.pal.dim
               else ncol)
        if extra:
            d.text((nx, y + self.f_big.size - self.f_med.size * 1.15
                    - self.f_small.size * 1.3), extra,
                   font=self.f_small, fill=self.pal.dim)
        return y + self.f_big.size * 1.12

    # --- panels ---------------------------------------------------------------
    def _cpu_panel(self, img, d, v):
        x0, y0, x1, y1 = self.p_cpu
        x, xr = x0 + 12, x1 - 12
        base, hot = self.col("cpu"), self.col("cpu", True)
        y = y0 + self.f_title.size * 1.2
        tstr = f"{v.temp:.0f}" if v.temp is not None else "--"
        y = self._bignum(d, x, y, "CORE TEMP", tstr, "°C",
                         self._tcol(v.temp, TEMP_WARN, "cpu"),
                         extra=f"{v.freq_ghz:.1f} GHZ")
        y = self._lv(d, x, xr, y, "LOAD", f"{v.load:.0f}%",
                     self._tcol(v.load, PCT_WARN, "cpu"), self.f_med)
        bh = self.h * 0.020
        self.seg_bar(d, x, y, xr - x, bh, self.ease("cl", v.load / 100), 22,
                     base, scale(base, 0.18))
        y += bh + self.f_label.size * 0.8
        try:
            l1, l5, l15 = os.getloadavg()
        except OSError:
            l1 = l5 = l15 = 0.0
        y = self._lv(d, x, xr, y, "LOADAVG", f"{l1:.2f} {l5:.2f} {l15:.2f}",
                     self.pal.fg, self.f_small)
        # load history ribbon fills the leftover strip
        rh = max(14.0, y1 - 12 - y)
        if rh > 14:
            histo = self.hist("ld", v.load / 100, n=40)
            step = (xr - x) / (len(histo) - 1)
            pts = [(x + i * step, y + rh - f * rh * 0.92) for i, f in enumerate(histo)]
            self._ribbon(d, pts, base, y + rh)
        return d

    def _gpu_panel(self, d, v):
        x0, y0, x1, y1 = self.p_gpu
        x, xr = x0 + 12, x1 - 12
        base = self.col("gpu")
        y = y0 + self.f_title.size * 1.2
        g = v.gpu
        if not g:
            d.text((x, y), "OFFLINE", font=self.f_med, fill=self.pal.dim)
            return
        y = self._lv(d, x, xr, y, "UTIL", f"{g['util']:.0f}%",
                     self._tcol(g["util"], PCT_WARN, "gpu"), self.f_med)
        bh = self.h * 0.020
        self.seg_bar(d, x, y, xr - x, bh, self.ease("gu", g["util"] / 100), 22,
                     base, scale(base, 0.18))
        y += bh + self.f_label.size * 0.9
        y = self._bignum(d, x, y, "GPU TEMP", f"{g['temp']:.0f}", "°C",
                         self._tcol(g["temp"], TEMP_WARN, "gpu"))
        y = self._bignum(d, x, y, "POWER DRAW", f"{g['power']:.0f}", "W",
                         self.pal.fg)
        vr = g["mem_used"] / max(1.0, g["mem_total"])
        d.text((x, y), "VRAM", font=self.f_label, fill=self.pal.dim)
        vtxt = f"{g['mem_used'] / 1024:.1f}/{g['mem_total'] / 1024:.0f}G"
        d.text((xr - d.textlength(vtxt, font=self.f_label), y), vtxt,
               font=self.f_label, fill=self.pal.fg)
        y += self.f_label.size * 1.4
        if y + bh < y1 - 4:
            self.seg_bar(d, x, y, xr - x, bh, self.ease("gv", vr), 22,
                         base, scale(base, 0.18))

    def _mem_panel(self, d, v, with_net: bool):
        x0, y0, x1, y1 = self.p_mem
        base, ram_h = self.col("ram"), self.col("ram", True)
        ssd = self.col("ssd")
        # RAM tank: vertical level gauge on the panel's left edge
        tw = min(44, (x1 - x0) * 0.17)
        tx0, ty0 = x0 + 12, y0 + self.f_title.size * 1.5
        ty1 = y1 - 12
        d.rounded_rectangle([tx0, ty0, tx0 + tw, ty1], radius=4,
                            fill=mix(self.pal.bg, base, 0.10),
                            outline=scale(base, 0.55))
        lvl = self.ease("ram", v.mem_pct / 100)
        ly = ty1 - (ty1 - ty0 - 4) * lvl - 2
        fill_col = base if v.mem_pct < PCT_WARN else \
            self._tcol(v.mem_pct, PCT_WARN, "ram")
        d.rectangle([tx0 + 2, ly, tx0 + tw - 2, ty1 - 2], fill=fill_col)
        d.line([(tx0 + 2, ly), (tx0 + tw - 2, ly)], fill=ram_h, width=2)
        for q in (0.25, 0.5, 0.75):              # quarter marks
            qy = ty1 - (ty1 - ty0) * q
            d.line([(tx0, qy), (tx0 + 5, qy)], fill=scale(base, 0.7), width=1)

        x, xr = tx0 + tw + 12, x1 - 12
        y = y0 + self.f_title.size * 1.2
        y = self._lv(d, x, xr, y, "RAM", f"{v.mem_pct:.0f}%",
                     self._tcol(v.mem_pct, PCT_WARN, "ram"), self.f_med)
        y = self._lv(d, x, xr, y, "", f"{v.mem_used:.1f}/{v.mem_total:.0f}G",
                     self.pal.dim, self.f_small)
        d.line([(x, y), (xr, y)], fill=scale(self.pal.dim, 0.3), width=1)
        y += self.f_label.size * 0.6
        y = self._lv(d, x, xr, y, "SSD", f"{v.disk_pct:.0f}%",
                     self._tcol(v.disk_pct, PCT_WARN, "ssd"), self.f_med)
        bh = self.h * 0.016
        self.seg_bar(d, x, y, xr - x, bh, self.ease("dk", v.disk_pct / 100), 18,
                     ssd, scale(ssd, 0.18))
        y += bh + self.f_label.size * 0.7
        nv = f"{v.nvme_temp:.0f}°C" if v.nvme_temp is not None else "--"
        y = self._lv(d, x, xr, y, "NVME", nv,
                     self._tcol(v.nvme_temp, 70, "ssd"), self.f_label)
        rd, ru = human_rate(v.disk_rd)
        wr, wu = human_rate(v.disk_wr)
        y = self._lv(d, x, xr, y, "R/W", f"{rd}{ru} · {wr}{wu}",
                     self.pal.fg, self.f_small)
        y = self._lv(d, x, xr, y, "USED",
                     f"{v.disk_used:.0f}/{v.disk_total:.0f}G",
                     self.pal.dim, self.f_small)
        if with_net:                             # 960: net/sys fold in here
            dv, du = human_rate(v.net_down)
            uv, uu = human_rate(v.net_up)
            y = self._lv(d, x, xr, y, "▼▲", f"{dv}{du} · {uv}{uu}",
                         self.pal.fg, self.f_small)
            up = v.uptime
            y = self._lv(d, x, xr, y, "SYS",
                         f"{v.procs}P {up // 86400}D{up % 86400 // 3600:02d}H",
                         self.pal.dim, self.f_small)
        return x, xr, y, y1

    def _net_panel(self, img, d, v):
        x0, y0, x1, y1 = self.p_net
        x, xr = x0 + 12, x1 - 12
        dn, up_c = self.col("ssd"), self.col("gpu")
        y = y0 + self.f_title.size * 1.2
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        y = self._lv(d, x, xr, y, "▼ DOWN", f"{dv} {du}", dn, self.f_med)
        # downlink ribbon, log-normalised so idle traffic still registers
        rh = self.f_med.size * 1.0
        histo = self.hist("nd", min(1.0, math.log10(max(1.0, v.net_down)) / 8.0),
                          n=40)
        step = (xr - x) / (len(histo) - 1)
        pts = [(x + i * step, y + rh - f * rh * 0.9) for i, f in enumerate(histo)]
        self._ribbon(d, pts, dn, y + rh)
        y += rh + self.f_label.size * 0.6
        y = self._lv(d, x, xr, y, "▲ UP", f"{uv} {uu}", up_c, self.f_med)
        y = self._lv(d, x, xr, y, "TOTAL",
                     f"RX {v.net_rx_total:.0f}G · TX {v.net_tx_total:.0f}G",
                     self.pal.dim, self.f_small)
        d.line([(x, y), (xr, y)], fill=scale(self.pal.dim, 0.3), width=1)
        y += self.f_label.size * 0.6
        y = self._lv(d, x, xr, y, "PROCS", f"{v.procs}", self.pal.fg, self.f_med)
        up = v.uptime
        y = self._lv(d, x, xr, y, "UPTIME",
                     f"{up // 86400}D {up % 86400 // 3600:02d}H "
                     f"{up % 3600 // 60:02d}M", self.pal.fg, self.f_label)
        # activity pips fill the leftover strip at the panel foot
        if y + self.f_label.size * 1.6 < y1:
            d.text((x, y1 - 14 - self.f_small.size), "LINK", font=self.f_small,
                   fill=self.pal.dim)
            self._pips(d, x + self.f_small.size * 3.4, y1 - 12 - self.f_small.size,
                       self.f_small.size * 0.6, self.col("ssd", True), 0,
                       dark=scale(self.pal.dim, 0.3))
        return d

    # --- the reactor ----------------------------------------------------------
    def _core(self, img, d, v, load_f, eload):
        cx, cy = self.core_cx, self.core_cy
        base, hot = self.col("cpu"), self.col("cpu", True)

        # heat haze: wavy lines drifting above the core (cheap shimmer)
        for j in range(3):
            yl = self.main_y0 + 10 + j * 10
            amp = 2.2 + j * 1.1
            ph = self.t * hz(0.55 + 0.2 * j) * math.tau
            pts = [(xx, yl + math.sin(xx * 0.045 + ph + j * 2.1) * amp)
                   for xx in range(int(cx - self.ring_r * 1.05),
                                   int(cx + self.ring_r * 1.05), 9)]
            if len(pts) > 1:
                d.line(pts, fill=mix(self.pal.bg, self.col("gpu"), 0.34 - j * 0.07),
                       width=1)

        # orb: colour temperature follows CPU temp, size breathes + swells
        temp = v.temp if v.temp is not None else 40 + 30 * load_f
        etemp = self.ease("tempf", max(0.0, min(1.0, (temp - 42) / 53)))
        flick = (self.wrng(self.HZ_FLICK, salt=5).random() - 0.5) * 0.036
        pulse = 1 + 0.05 * math.sin(self.t * math.tau * hz(self.HZ_BREATHE)) + flick
        r_core = self.ring_r * 0.52 * (1 + 0.13 * eload) * pulse
        u = etemp * 2.0
        if u < 1.0:
            spr = Image.blend(self.orbs[0], self.orbs[1], u)
        else:
            spr = Image.blend(self.orbs[1], self.orbs[2], min(1.0, u - 1.0))
        dd = max(8, int(r_core * 4.4))
        spr = spr.resize((dd, dd), Image.BILINEAR)
        img.alpha_composite(spr, (int(cx - dd / 2), int(cy - dd / 2)))
        d = ImageDraw.Draw(img)

        # containment ring = CPU load gauge, gap at the bottom for the readout
        self.arc_gauge(d, cx, cy, self.ring_r, eload, base, width=self.ring_w,
                       a0=120, a1=420, track=mix(self.pal.bg, base, 0.30))
        a = math.radians(120 + 300 * eload)
        ex, ey = cx + math.cos(a) * self.ring_r, cy + math.sin(a) * self.ring_r
        er = self.ring_w * 0.85
        d.ellipse([ex - er * 1.7, ey - er * 1.7, ex + er * 1.7, ey + er * 1.7],
                  outline=base, width=2)
        d.ellipse([ex - er, ey - er, ex + er, ey + er], fill=hot)
        self._sparks(d, ex, ey, er, hot, dens(2 + int(load_f * 5)))

        # radial discharges core→ring; probability rides the load
        win = self.wrng(self.HZ_BOLT, salt=0xB0)
        if win.random() < 0.16 + 0.74 * load_f:
            for _ in range(dens(1 + int(load_f * 2.5))):
                self._rbolt(d, cx, cy, win.uniform(0, math.tau),
                            r_core * 0.5, self.ring_r - self.ring_w * 0.6,
                            self.ring_r * 0.11, scale(base, 0.7), hot, win)

        txt = f"CPU {v.load:.0f}%"
        d.text((cx - d.textlength(txt, font=self.f_label) / 2,
                cy + self.ring_r * 0.86 - self.f_label.size * 0.4),
               txt, font=self.f_label, fill=self.pal.fg)

        # embers: deterministic per-ember phase; x jitter frozen per its cycle,
        # count follows GPU power draw
        pwr_f = min(1.0, (v.gpu.get("power", 0.0) / 280.0)) if v.gpu \
            else load_f * 0.5
        n = min(30, dens(16 + 14 * pwr_f))
        emb = lighten(self.col("gpu"), 0.3)
        rise = cy - (self.header_h + 8)
        for i in range(n):
            sp = hz(0.12 + 0.10 * ((i * 0.61) % 1.0))
            phase = (self.t * sp + i * 0.37) % 1.0
            k = int(self.t * sp + i * 0.37)      # this ember's own cycle index
            rng = random.Random(self.seed ^ (k * 0x9E3779B1) ^ (i * 0x85EBCA6B))
            jx = rng.uniform(-0.8, 0.8) * r_core
            drift = rng.uniform(-0.35, 0.35)
            xx = cx + jx + math.sin(phase * math.pi) * jx * drift \
                + math.sin(phase * 6.0 + i) * 5 * phase
            yy = cy - r_core * 0.2 - phase * rise
            fade = (1.0 - phase) ** 1.5
            s = 0.8 + 2.2 * (1.0 - phase)
            d.ellipse([xx - s, yy - s, xx + s, yy + s],
                      fill=mix(self.pal.bg, emb, 0.25 + 0.75 * fade))
        return d

    def _chart(self, d, v):
        gx0, gy0, gx1, gy1 = self._chart_box
        ct = v.temp if v.temp is not None else self._ct
        gt = v.gpu.get("temp", self._gt) if v.gpu else self._gt
        self._ct, self._gt = ct, gt
        for key, val, ck in (("ct", ct, "cpu"), ("gt", gt, "gpu")):
            histo = self.hist(key, val, n=64)
            step = (gx1 - gx0) / (len(histo) - 1)
            pts = [(gx0 + i * step, self._chart_y(t)) for i, t in enumerate(histo)]
            d.line(pts, fill=self.pal.base(ck), width=2)
        # legend with live values, right-aligned inside the plot
        lx = gx1 - 6
        for i, (name, val, ck) in enumerate((("CPU", ct, "cpu"), ("GPU", gt, "gpu"))):
            s = f"{name} {val:.0f}°"
            lw = d.textlength(s, font=self.f_small)
            d.text((lx - lw, gy0 + 3 + i * self.f_small.size * 1.25), s,
                   font=self.f_small, fill=self._tcol(val, TEMP_WARN, ck))

    def _rods(self, d, v):
        rx, ry, span, rh = self._rod_geom
        base, hot = self.col("cpu"), self.col("cpu", True)
        cores = v.per_core or [v.load]
        for i in range(self.RODS):
            ld = max(0.0, min(1.0, cores[i % len(cores)] / 100))
            depth = self.ease(f"rod{i}", ld)     # insertion glides, glow snaps
            x = rx + i * span + span * 0.22
            wdt = span * 0.56
            hgt = rh * (0.16 + 0.84 * depth)
            col = mix(scale(base, 0.30), lighten(base, 0.15 * ld), ld)
            d.rectangle([x + 1, ry + 1, x + wdt - 1, ry + hgt], fill=col)
            d.line([(x + 1, ry + hgt), (x + wdt - 1, ry + hgt)],
                   fill=hot if ld > 0.5 else lighten(col, 0.3), width=2)
            if ld > 0.85 and (self.t * 3.0) % 1.0 < 0.55:
                d.rectangle([x, ry, x + wdt, ry + hgt + 1], outline=hot, width=1)

    def _header(self, d, v):
        yc = (self.header_h - self.f_label.size) / 2
        # status lamp + text; overheat turns it into a 3 Hz alarm
        overheat = v.temp is not None and v.temp >= TEMP_WARN
        lamp_r = self.f_label.size * 0.42
        lx, ly = self.pad + lamp_r, self.header_h / 2
        if overheat:
            on = (self.t * 3.0) % 1.0 < 0.55
            lamp, txt, tcol = self.col("ssd", True), "CORE OVERHEAT", \
                self.col("ssd", on)
        else:
            on = (self.t * hz(self.HZ_LAMP)) % 1.0 < 0.62
            lamp, txt, tcol = self.col("ram"), "REACTOR ONLINE", self.col("cpu")
        d.ellipse([lx - lamp_r, ly - lamp_r, lx + lamp_r, ly + lamp_r],
                  fill=lamp if on else mix(self.pal.bg, lamp, 0.25),
                  outline=scale(lamp, 0.7))
        d.text((lx + lamp_r * 2.2, yc), txt, font=self.f_label, fill=tcol)

        now = time.localtime()
        clock = time.strftime("%H:%M:%S", now)
        tw = d.textlength(clock, font=self.f_clock)
        cx = (self.w - tw) / 2
        d.text((cx, (self.header_h - self.f_clock.size * 1.25) / 2), clock,
               font=self.f_clock, fill=self.pal.fg)
        date = time.strftime("%a %d/%m/%Y", now).upper()
        d.text((cx + tw + self.f_label.size * 1.4, yc), date,
               font=self.f_label, fill=self.pal.dim)
        # host is static (in bg); a sample-marker strip fills the left gap
        step = int(self.t * hz(self.HZ_MARK))
        marker = "".join("▮" if (step + i) % 5 < 2 else "▯" for i in range(5))
        mx = lx + lamp_r * 2.2 + d.textlength("CORE OVERHEAT  ", font=self.f_label)
        if mx + self.f_small.size * 6 < cx:
            d.text((mx, yc), marker, font=self.f_small, fill=scale(self.col("gpu"), 0.8))

    # --- frame ----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        v = vitals()
        img = self._bg.copy()
        d = ImageDraw.Draw(img)
        load_f = max(0.0, min(1.0, v.load / 100))
        eload = self.ease("load", load_f)

        d = self._core(img, d, v, load_f, eload)
        d = self._cpu_panel(img, d, v)
        self._gpu_panel(d, v)
        mx, mxr, my, my1 = self._mem_panel(d, v, with_net=self.p_net is None)
        # RAM history ribbon fills whatever the mem panel has left over
        rh = my1 - 12 - my
        if rh > 16:
            histo = self.hist("mh", v.mem_pct / 100, n=40)
            step = (mxr - mx) / (len(histo) - 1)
            pts = [(mx + i * step, my + rh - f * rh * 0.9)
                   for i, f in enumerate(histo)]
            self._ribbon(d, pts, self.col("ram"), my + rh)
        if self.p_net:
            d = self._net_panel(img, d, v)
        self._chart(d, v)
        self._rods(d, v)
        self._header(d, v)

        self._scanline(img, colour=self.col("cpu"))
        return img.convert("RGB")


VIEWS = {"plasma-core": PlasmaCoreView}
