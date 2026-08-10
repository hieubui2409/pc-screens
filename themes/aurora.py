"""aurora — northern-lights sky over glass instrument cards (vertical).

Slow colour curtains drift behind a static starfield; every readout sits on a
semi-transparent glass card so the sky stays visible through the panel. Calm
styling but dense content: clock, date, host, uptime, CPU/GPU/RAM/SSD/NET
cards with sparklines, a per-core dot matrix and breathing ribbon separators.

Perf contract: the blurred aurora blobs are built ONCE at quarter resolution
in __init__; per frame they are only blended/resized while small, then the
quarter layer is upscaled once. The glass card layer is fully static. The
only per-frame blur is the clock glow, done on a narrow strip, not the canvas.
"""

from __future__ import annotations

import math
import os
import random
import time

from PIL import Image, ImageDraw, ImageFilter

from lianli88 import load_font
from fx import (FXBase, Palette, human_rate, load_mono, mix, scale, vitals)

AURORA = Palette("aurora",
                 ((34, 224, 165), (79, 107, 255), (180, 79, 255), (120, 220, 255)),
                 bg=(5, 7, 14), grid=(14, 22, 38), dim=(150, 166, 192))

WARM = (255, 138, 74)          # blob tint target when the CPU runs hot


class AuroraView(FXBase):

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or AURORA
        self._start(0xA0208A)
        self.host = os.uname().nodename.upper()[:14]

        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        size = int(min(w * 0.32, h * 0.12))
        self.clock_sp = size * 0.10
        while size > 14 and (probe.textlength("00:00", font=load_mono(size))
                             + 4 * size * 0.10) > w * 0.84:
            size = int(size * 0.93)
            self.clock_sp = size * 0.10
        self.f_time = load_mono(size)
        self.f_date = load_mono(max(10, int(size * 0.16)))
        self.date_sp = w * 0.006
        while (self.f_date.size > 10 and
               probe.textlength("WEDNESDAY · 28 SEPTEMBER 2026", font=self.f_date)
               + 28 * self.date_sp > w * 0.92):
            self.f_date = load_mono(int(self.f_date.size * 0.93))
        self.f_label = load_mono(max(10, int(w * 0.032)))
        self.f_small = load_mono(max(9, int(w * 0.026)))
        self.f_tiny = load_mono(max(8, int(w * 0.022)))

        # --- flow layout, computed once: the glass layer depends on it ---
        self.pad = w * 0.055
        y = h * 0.012
        self.y_top = y
        y += self.f_small.size * 1.9
        self.y_clock = y
        y += self.f_time.size * 1.24
        self.y_date = y
        y += self.f_date.size * 2.0
        self.y_info = y
        y += self.f_small.size * 1.9 + h * 0.006

        self.kinds = ["cpu"]
        if vitals().gpu:
            self.kinds.append("gpu")
        self.kinds += ["ram", "ssd", "net"]
        weights = {"cpu": 1.30, "gpu": 1.02, "ram": 0.88, "ssd": 0.95, "net": 0.95}
        self.sep_h = max(10, h * 0.013)
        region = h - h * 0.012 - y - self.sep_h * (len(self.kinds) - 1)
        total_w = sum(weights[k] for k in self.kinds)
        self.rects, self.sep_yc = [], []
        for k in self.kinds:
            ch = region * weights[k] / total_w
            self.rects.append((self.pad, y, w - self.pad, y + ch))
            y += ch
            self.sep_yc.append(y + self.sep_h / 2)
            y += self.sep_h
        self.sep_yc.pop()

        self._accent = {"cpu": "cpu", "gpu": "gpu", "ram": "ram", "ssd": "ssd"}
        self._bg = self._build_bg()
        self._build_blobs()
        self.glass = self._build_glass()

    # --- static layers --------------------------------------------------------
    def _build_bg(self) -> Image.Image:
        img = Image.new("RGBA", (self.w, self.h), tuple(self.pal.bg) + (255,))
        d = ImageDraw.Draw(img)
        rng = random.Random(0x57A2)
        self.stars = []
        for _ in range(60):
            x, y = rng.uniform(0, self.w), rng.uniform(0, self.h)
            r = rng.uniform(0.5, 1.5)
            self.stars.append((x, y, r))
            d.ellipse([x - r, y - r, x + r, y + r],
                      fill=scale(self.pal.fg, rng.uniform(0.30, 0.75)))
        return img

    def _blob(self, bw: int, bh: int, col, peak: int = 108) -> Image.Image:
        """Radial-gradient blob: stepped ellipses smoothed by one init-time blur."""
        im = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        dd = ImageDraw.Draw(im)
        steps = 20
        for i in range(steps):
            f = 1 - i / steps
            a = int(peak * ((i + 1) / steps) ** 1.7)
            dd.ellipse([bw * (1 - f) / 2, bh * (1 - f) / 2,
                        bw - bw * (1 - f) / 2, bh - bh * (1 - f) / 2],
                       fill=tuple(col) + (a,))
        return im.filter(ImageFilter.GaussianBlur(min(bw, bh) / 5))

    def _build_blobs(self) -> None:
        # Quarter resolution keeps the per-frame upscale the only full-canvas
        # cost of the whole sky.
        self.qw, self.qh = max(1, self.w // 4), max(1, self.h // 4)
        qw, qh = self.qw, self.qh
        specs = [  # (w, h, accent idx, centre y frac, drift periods s)
            (1.7, 0.30, 0, 0.10, 29.0, 23.0, 33.0),
            (1.4, 0.26, 1, 0.38, 37.0, 19.0, 26.0),
            (1.6, 0.24, 2, 0.66, 18.0, 31.0, 40.0),
            (1.2, 0.20, 3, 0.90, 26.0, 40.0, 21.0),
        ]
        self.blobs = []
        for fw, fh, ci, fy, px, py, ps in specs:
            bw, bh = max(4, int(qw * fw)), max(4, int(qh * fh))
            col = self.pal.base(ci)
            cool = self._blob(bw, bh, col)
            warm = self._blob(bw, bh, mix(col, WARM, 0.6))
            self.blobs.append((cool, warm, qw * 0.5, qh * fy,
                               px, py, qw * 0.35, qh * 0.03, ps))

    def _build_glass(self) -> Image.Image:
        layer = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        rad = self.w * 0.035
        for kind, (x0, y0, x1, y1) in zip(self.kinds, self.rects):
            acc = self._kcol(kind)
            d.rounded_rectangle([x0, y0, x1, y1], radius=rad,
                                fill=(255, 255, 255, 22),
                                outline=tuple(scale(acc, 0.85)) + (95,), width=1)
            d.line([(x0 + rad, y0 + 2), (x1 - rad, y0 + 2)],
                   fill=(255, 255, 255, 38), width=1)
        return layer

    def _kcol(self, kind: str, hot: bool = False):
        if kind == "net":
            return mix(self.pal.base(1), self.pal.base(3), 0.5)
        return self.col(self._accent[kind], hot)

    # --- per-frame widgets ----------------------------------------------------
    def _base(self, v) -> Image.Image:
        """bg + sky + glass, rebuilt on a 3 Hz wall clock.

        The full-canvas upscale/composites are the frame budget's biggest cost,
        and the blobs drift over 15-40 s periods — refreshing the composed base
        3x/s moves their ~100 px-soft edges about 12 px per step, below what a
        blurred gradient can show, while roughly halving the mean frame time.
        """
        c = int(self.t * 3)
        if getattr(self, "_base_c", None) != c:
            img = self._bg.copy()
            self._sky(img, v)
            img.alpha_composite(self.glass)
            self._base_c, self._base_img = c, img
        return self._base_img.copy()

    def _glow(self, hhmm: str) -> Image.Image:
        """Clock glow strip, cached per minute — the only blur in the theme."""
        if getattr(self, "_glow_key", None) != hhmm:
            gp = int(self.f_time.size * 0.20)
            strip = Image.new("RGBA", (self.w, self.f_time.size + gp * 3),
                              (0, 0, 0, 0))
            sd = ImageDraw.Draw(strip)
            self.spaced_text(sd, hhmm, self.f_time, gp,
                             tuple(self.pal.base(0)) + (200,), self.clock_sp)
            strip = strip.filter(ImageFilter.GaussianBlur(self.f_time.size * 0.085))
            self._glow_key, self._glow_img, self._glow_pad = hhmm, strip, gp
        return self._glow_img

    def _sky(self, img: Image.Image, v) -> None:
        q = Image.new("RGBA", (self.qw, self.qh), (0, 0, 0, 0))
        wf = 0.0 if v.temp is None else max(0.0, min(1.0, (v.temp - 45) / 35))
        for i, (cool, warm, bx, by, px, py, ax, ay, ps) in enumerate(self.blobs):
            b = Image.blend(cool, warm, wf * 0.75) if wf > 0.02 else cool
            k = 1.0 + 0.07 * math.sin(self.t * math.tau / ps + i * 1.7)
            b = b.resize((max(2, int(b.width * k)), max(2, int(b.height * k))),
                         Image.BILINEAR)
            ox = int(bx + math.sin(self.t * math.tau / px + i) * ax - b.width / 2)
            oy = int(by + math.cos(self.t * math.tau / py + i * 2.1) * ay - b.height / 2)
            q.paste(b, (ox, oy), b)
        img.alpha_composite(q.resize((self.w, self.h), Image.BILINEAR))

    def _twinkle(self, d) -> None:
        # Twinkles draw above the cached base, so only sky stars clear of the
        # cards may flare — a bright dot on top of a card reads as dirt.
        if not hasattr(self, "_open"):
            self._open = [s for s in self.stars
                          if not any(y0 - 3 < s[1] < y1 + 3
                                     for _, y0, _, y1 in self.rects)]
        if not self._open:
            return
        rw = self.wrng(1.1, salt=33)
        for idx in rw.sample(range(len(self._open)), min(6, len(self._open))):
            x, y, r = self._open[idx]
            rr = r + 1.0
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=self.pal.fg)

    def _sep(self, d, yc: float, i: int) -> None:
        """Breathing aurora ribbon between two cards."""
        x0, x1 = self.pad, self.w - self.pad
        col = self.pal.base(i % 4)
        amp = self.sep_h * 0.32 * (0.55 + 0.45 * math.sin(self.t * math.tau / 9.0 + i * 1.3))
        ph = self.t * math.tau / 17.0 + i * 2.0
        n = 44
        pts = [(x0 + (x1 - x0) * t / n,
                yc + amp * math.sin(t / n * 3 * math.tau + ph)) for t in range(n + 1)]
        d.line([(px, py + 2) for px, py in pts], fill=scale(col, 0.35), width=2)
        d.line(pts, fill=scale(col, 0.85), width=1)

    def _big(self, d, x, y, txt, unit, f_val, accent) -> float:
        d.text((x, y), txt, font=f_val, fill=self.pal.fg)
        tx = x + d.textlength(txt, font=f_val) + 4
        d.text((tx, y + f_val.size - self.f_label.size * 1.15),
               unit, font=self.f_label, fill=accent)
        return tx + d.textlength(unit, font=self.f_label)

    def _spark(self, ld, x, y, sw, sh, dq, base, mx: float = 1.0) -> None:
        # Framed as a chart (gridlines + border) so a low flat metric reads as
        # headroom, not as an empty patch of card.
        for fr in (0.25, 0.50, 0.75):
            yy = y + sh - fr * sh * 0.92
            ld.line([(x, yy), (x + sw, yy)], fill=(255, 255, 255, 22), width=1)
        for i in range(1, 4):
            xx = x + sw * i / 4
            ld.line([(xx, y), (xx, y + sh)], fill=(255, 255, 255, 14), width=1)
        step = sw / (len(dq) - 1)
        pts = [(x + i * step, y + sh - min(1.0, vv / mx) * sh * 0.92)
               for i, vv in enumerate(dq)]
        ld.polygon([(x, y + sh)] + pts + [(x + sw, y + sh)],
                   fill=tuple(base) + (58,))
        ld.line(pts, fill=tuple(base) + (235,), width=2)
        ld.rectangle([x, y, x + sw, y + sh],
                     outline=tuple(scale(base, 0.5)) + (110,), width=1)
        px, py = pts[-1]
        ld.ellipse([px - 3, py - 3, px + 3, py + 3], fill=tuple(base) + (255,))

    def _soft_bar(self, ld, x, y, bw, bh, frac, base, n: int = 26) -> None:
        frac = max(0.0, min(1.0, frac))
        seg = bw / n
        for i in range(n):
            lit = (i + 0.5) / n <= frac
            ld.rectangle([x + i * seg, y, x + i * seg + seg * 0.72, y + bh],
                         fill=tuple(base) + (225,) if lit else (255, 255, 255, 26))

    def _dots(self, ld, x0, x1, yc, cores, base, hot) -> None:
        """20-dot core matrix; brightness follows each core's load."""
        if not cores:
            return
        cellw = (x1 - x0) / 10
        r = min(cellw * 0.30, self.f_label.size * 0.55)
        for i in range(20):
            f = max(0.0, min(1.0, cores[i * len(cores) // 20] / 100))
            cx = x0 + (i % 10) * cellw + cellw / 2
            cy = yc + (cellw * 0.60 if i >= 10 else -cellw * 0.60)
            if f > 0.55:
                rr = r * 1.9
                ld.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                           fill=tuple(base) + (int(70 * f),))
            col = mix(scale(base, 0.35), hot, f)
            ld.ellipse([cx - r, cy - r, cx + r, cy + r],
                       fill=tuple(col) + (int(50 + 205 * f),))

    # --- cards ----------------------------------------------------------------
    def _label_row(self, d, x, xr, y, label, accent, right, rcol=None):
        d.text((x, y), label, font=self.f_label, fill=accent)
        self._pips(d, x + d.textlength(label, font=self.f_label) + self.w * 0.02,
                   y + self.f_label.size * 0.32, self.f_label.size * 0.22,
                   self.pal.fg, 0, dark=scale(self.pal.dim, 0.3))
        if right:
            f = self.fit_text(d, right, self.f_small, (xr - x) * 0.55, mono=True)
            d.text((xr - d.textlength(right, font=f),
                    y + self.f_label.size - f.size), right,
                   font=f, fill=rcol or self.pal.dim)
        return y + self.f_label.size * 1.45

    def _val_font(self, ch: int):
        return load_font(max(18, int(min(ch * 0.33, self.w * 0.17,
                                         self.f_time.size * 0.75))))

    def _tcol(self, t):
        if t is None:
            return self.pal.dim
        return mix(self.pal.dim, WARM, max(0.0, min(1.0, (t - 60) / 25)))

    def _card_cpu(self, d, ld, rect, v):
        x0, y0, x1, y1 = rect
        cp = self.w * 0.045
        x, xr = x0 + cp, x1 - cp
        base, hot = self._kcol("cpu"), self._kcol("cpu", True)
        ch = y1 - y0
        y = self._label_row(d, x, xr, y0 + cp * 0.75, "CPU", base,
                            f"{v.freq_ghz:.2f} GHZ")
        f_val = self._val_font(ch)
        self._big(d, x, y, f"{v.load:.0f}", "%", f_val, base)
        self._dots(ld, x + (xr - x) * 0.52, xr, y + f_val.size * 0.58,
                   v.per_core, base, hot)
        y += f_val.size * 1.14
        t = f"TEMP {v.temp:.0f}°C" if v.temp is not None else "TEMP —"
        d.text((x, y), t, font=self.f_small, fill=self._tcol(v.temp))
        l1, l5, l15 = os.getloadavg()
        la = f"LOAD {l1:.2f} {l5:.2f} {l15:.2f}"
        d.text((xr - d.textlength(la, font=self.f_small), y),
               la, font=self.f_small, fill=self.pal.dim)
        y += self.f_small.size * 1.8
        sh = y1 - cp * 0.8 - y
        if sh >= 16:
            self._spark(ld, x, y, xr - x, sh,
                        self.hist("au:cpu", v.load / 100, n=40), base)

    def _card_gpu(self, d, ld, rect, v):
        x0, y0, x1, y1 = rect
        cp = self.w * 0.045
        x, xr = x0 + cp, x1 - cp
        base = self._kcol("gpu")
        ch = y1 - y0
        g = v.gpu
        if not g:
            self._label_row(d, x, xr, y0 + cp * 0.75, "GPU", base, "OFFLINE")
            return
        y = self._label_row(d, x, xr, y0 + cp * 0.75, "GPU", base,
                            f"VRAM {g['mem_used']/1024:.1f}/{g['mem_total']/1024:.0f}G")
        f_val = self._val_font(ch)
        self._big(d, x, y, f"{g['util']:.0f}", "%", f_val, base)
        ty = y + f_val.size * 0.52 - self.f_label.size * 1.05
        for line, col in ((f"{g['temp']:.0f}°C", self._tcol(g["temp"])),
                          (f"{g['power']:.0f} W", self.pal.dim)):
            d.text((xr - d.textlength(line, font=self.f_label), ty),
                   line, font=self.f_label, fill=col)
            ty += self.f_label.size * 1.2
        y += f_val.size * 1.10
        bh = max(5, ch * 0.045)
        self._soft_bar(ld, x, y, xr - x, bh,
                       self.ease("au:vram", g["mem_used"] / max(1, g["mem_total"])),
                       base)
        y += bh + self.f_small.size * 0.8
        sh = y1 - cp * 0.8 - y
        if sh >= 16:
            self._spark(ld, x, y, xr - x, sh,
                        self.hist("au:gpu", g["util"] / 100, n=40), base)

    def _card_ram(self, d, ld, rect, v):
        x0, y0, x1, y1 = rect
        cp = self.w * 0.045
        x, xr = x0 + cp, x1 - cp
        base = self._kcol("ram")
        ch = y1 - y0
        y = self._label_row(d, x, xr, y0 + cp * 0.75, "RAM", base,
                            f"{v.mem_pct:.0f}%", rcol=self.pal.fg)
        f_val = self._val_font(ch)
        vx = self._big(d, x, y, f"{v.mem_used:.1f}", "G", f_val, base)
        d.text((vx + 8, y + f_val.size - self.f_small.size * 1.2),
               f"/ {v.mem_total:.0f}G", font=self.f_small, fill=self.pal.dim)
        y += f_val.size * 1.08
        bh = max(5, ch * 0.05)
        self._soft_bar(ld, x, y, xr - x, bh,
                       self.ease("au:ram", v.mem_pct / 100), base, n=28)
        y += bh + self.f_small.size * 0.55
        if y1 - cp * 0.8 - y >= self.f_small.size * 0.9:
            d.text((x, y), f"FREE {v.mem_total - v.mem_used:.1f}G",
                   font=self.f_small, fill=self.pal.dim)
            pr = f"{v.procs} PROCS"
            d.text((xr - d.textlength(pr, font=self.f_small), y),
                   pr, font=self.f_small, fill=self.pal.dim)
            y += self.f_small.size * 1.8
        sh = y1 - cp * 0.8 - y
        if sh >= 16:
            self._spark(ld, x, y, xr - x, sh,
                        self.hist("au:ram", v.mem_pct / 100, n=40), base)

    def _card_ssd(self, d, ld, rect, v):
        x0, y0, x1, y1 = rect
        cp = self.w * 0.045
        x, xr = x0 + cp, x1 - cp
        base = self._kcol("ssd")
        ch = y1 - y0
        nt = f"{v.nvme_temp:.0f}°C" if v.nvme_temp is not None else ""
        y = self._label_row(d, x, xr, y0 + cp * 0.75, "SSD", base, nt,
                            rcol=self._tcol(v.nvme_temp))
        f_val = self._val_font(ch)
        self._big(d, x, y, f"{v.disk_pct:.0f}", "%", f_val, base)
        rv, ru = human_rate(v.disk_rd)
        wv, wu = human_rate(v.disk_wr)
        ty = y + f_val.size * 0.52 - self.f_small.size * 1.1
        for line, col in ((f"R {rv} {ru}", base),
                          (f"W {wv} {wu}", scale(base, 0.7))):
            d.text((xr - d.textlength(line, font=self.f_small), ty),
                   line, font=self.f_small, fill=col)
            ty += self.f_small.size * 1.3
        y += f_val.size * 1.08
        bh = max(5, ch * 0.05)
        self._soft_bar(ld, x, y, xr - x, bh,
                       self.ease("au:ssd", v.disk_pct / 100), base, n=28)
        y += bh + self.f_small.size * 0.55
        if y1 - cp * 0.8 - y >= self.f_small.size * 0.9:
            d.text((x, y), f"{v.disk_used:.0f} / {v.disk_total:.0f} GB",
                   font=self.f_small, fill=self.pal.dim)
            y += self.f_small.size * 1.8
        sh = y1 - cp * 0.8 - y
        if sh >= 16:
            dq = self.hist("au:ssdio", v.disk_rd + v.disk_wr, n=40)
            self._spark(ld, x, y, xr - x, sh, dq, base,
                        mx=max(max(dq) * 1.15, 1.0))

    def _card_net(self, d, ld, rect, v):
        x0, y0, x1, y1 = rect
        cp = self.w * 0.045
        x, xr = x0 + cp, x1 - cp
        base = self._kcol("net")
        ch = y1 - y0
        y = self._label_row(d, x, xr, y0 + cp * 0.75, "NET", base,
                            f"RX {v.net_rx_total:.1f}G · TX {v.net_tx_total:.1f}G")
        f_rate = load_font(max(16, int(min(ch * 0.24, self.w * 0.10))))
        cols = ((x, "▼", human_rate(v.net_down), self.pal.base(0)),
                (x + (xr - x) * 0.52, "▲", human_rate(v.net_up), self.pal.base(2)))
        for cx, sym, (val, unit), col in cols:
            d.text((cx, y + f_rate.size * 0.5 - self.f_label.size * 0.5),
                   sym, font=self.f_label, fill=col)
            tx = cx + self.f_label.size * 1.3
            d.text((tx, y), val, font=f_rate, fill=self.pal.fg)
            d.text((tx + d.textlength(val, font=f_rate) + 4,
                    y + f_rate.size - self.f_small.size * 1.15),
                   unit, font=self.f_small, fill=self.pal.dim)
        y += f_rate.size * 1.35
        sh = y1 - cp * 0.8 - y
        if sh >= 16:
            dq = self.hist("au:net", v.net_down, n=40)
            self._spark(ld, x, y, xr - x, sh, dq, self.pal.base(0),
                        mx=max(max(dq) * 1.15, 1.0))

    # --- frame ----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        v = vitals()
        w = self.w
        img = self._base(v)
        d = ImageDraw.Draw(img)
        self._twinkle(d)
        # One shared overlay for every translucent element: composited once,
        # instead of one full-canvas layer per sparkline.
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)

        now = time.localtime()
        pad = self.pad

        # --- header ---
        y = self.y_top
        self._pips(d, pad, y + self.f_small.size * 0.30, self.f_small.size * 0.36,
                   self.pal.base(2), 0, dark=scale(self.pal.dim, 0.3))
        d.text((pad + self.f_small.size * 2.3, y), "AURORA",
               font=self.f_small, fill=self.pal.base(2))
        d.text((w - pad - d.textlength(self.host, font=self.f_small), y),
               self.host, font=self.f_small, fill=self.pal.dim)

        # clock glow strip (cached per minute), crisp digits on top
        hhmm = time.strftime("%H:%M", now)
        img.alpha_composite(self._glow(hhmm), (0, int(self.y_clock - self._glow_pad)))
        d = ImageDraw.Draw(img)
        self.spaced_text(d, hhmm, self.f_time, self.y_clock,
                         self.pal.fg, self.clock_sp)

        self.spaced_text(d, time.strftime("%A · %d %B %Y", now).upper(),
                         self.f_date, self.y_date, self.pal.dim, self.date_sp)

        up = v.uptime
        d.text((pad, self.y_info),
               f"UP {up // 86400}D {up % 86400 // 3600:02d}H {up % 3600 // 60:02d}M",
               font=self.f_small, fill=self.pal.dim)
        sec = f"{int(time.time() % 60):02d}s"
        d.text((w - pad - d.textlength(sec, font=self.f_small), self.y_info),
               sec, font=self.f_small, fill=self.pal.base(0))

        # --- glass cards + ribbon separators ---
        painters = {"cpu": self._card_cpu, "gpu": self._card_gpu,
                    "ram": self._card_ram, "ssd": self._card_ssd,
                    "net": self._card_net}
        for kind, rect in zip(self.kinds, self.rects):
            painters[kind](d, ld, rect, v)
        for i, yc in enumerate(self.sep_yc):
            self._sep(d, yc, i)

        img.alpha_composite(lay)
        self._scanline(img, colour=self.pal.base(3))
        return img.convert("RGB")


VIEWS = {"aurora": AuroraView}
