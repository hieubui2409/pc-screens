"""holo-ring — JARVIS hologram HUD (vertical).

A concentric four-ring gauge cluster (CPU/GPU/MEM/SSD) with a counter-rotating
dashed orbit and callout lines, a rotating radar sweep with frozen-per-window
contacts, bracket-framed telemetry cells, and an occasional slice-offset glitch
— the hologram artefact. Signature colours are hologram cyan/teal; --palette
recolours everything.

Layout follows neon_grid: a y-budget computed once in __init__ (so 480x1920
and 480x960 both work), fonts probed against the canvas, the static hex-grid /
reticle layer prebuilt, every cadence wall-clock based, numbers on the sample
clock. A tall canvas earns a second info tier of ribbon meters.
"""

from __future__ import annotations

import math
import os
import time

from PIL import Image, ImageDraw, ImageFilter

from lianli88 import load_font
from fx import (FXBase, Palette, human_rate, hz, load_mono, mix, scale,
                vitals)

HOLO = Palette("holo",
               ((53, 224, 255), (143, 247, 216), (120, 180, 255), (216, 248, 255)),
               bg=(3, 8, 13), grid=(11, 30, 41), dim=(122, 158, 176))

# ring index, PIL angle of the callout anchor — two upper, two lower corners
CALLOUTS = (("cpu", "CPU", 0, 213), ("gpu", "GPU", 1, 327),
            ("ram", "MEM", 2, 147), ("ssd", "SSD", 3, 33))

CELL_LABELS = ("CPU TEMP", "GPU TEMP", "NVME", "VRAM",
               "NET ▼", "NET ▲", "LOADAVG", "PROCS", "UPTIME", "DISK I/O")


class HoloRingView(FXBase):
    RPS_ORBIT = -0.07    # dashed orbit counter-rotates against the sweep
    RPS_ORBIT2 = 0.05
    RPS_SWEEP = 0.32     # radar revolutions per second
    HZ_BLIP = 0.45       # contacts re-plotted per ~2s window
    HZ_GLITCH = 0.55     # glitch windows per second (mostly empty)
    GLITCH_P = 0.13
    A0, A1 = 120, 420    # gauge arc span, gap at the bottom

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or HOLO
        self._start(0x1101)
        self.host = os.uname().nodename.upper()[:14]

        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        size = int(min(w * 0.30, h * 0.105))
        while size > 14 and probe.textlength("00:00", font=load_mono(size)) > w * 0.80:
            size = int(size * 0.93)
        self.f_time = load_mono(size)
        self.f_label = load_mono(max(10, int(size * 0.15)))
        self.f_small = load_mono(max(9, int(size * 0.115)))
        self.f_tiny = load_mono(max(8, int(size * 0.095)))
        self.f_cell = load_mono(max(10, int(size * 0.135)))
        self.f_center = load_font(max(18, int(size * 0.55)))

        # --- vertical budget: header/clock down, data grid up, circles between
        self.pad = pad = w * 0.075
        g = h * 0.012
        y = h * 0.013
        self.y_hdr = y
        y += self.f_small.size * 1.85 + h * 0.008
        self.y_clock = y
        y += self.f_time.size * 1.10
        self.sec_h = max(4, int(h * 0.005))
        self.y_sec = y + h * 0.004
        self.y_date = self.y_sec + self.sec_h + self.f_small.size * 0.45
        y0 = self.y_date + self.f_small.size * 1.5 + g * 0.5

        self.row_h = self.f_cell.size * 2.3
        self.rp = self.row_h + h * 0.004
        self.y_data = h - h * 0.014 - 5 * self.rp
        block_top = self.y_data - self.f_label.size * 1.8
        avail = block_top - y0

        R0, Rr0 = w * 0.37, w * 0.25
        cpad, rpad = w * 0.085, self.f_small.size * 2.2
        need = 2 * (R0 + cpad) + g + 2 * Rr0 + rpad
        self.tall = avail - need >= h * 0.11
        if self.tall:
            self.R, self.Rr = w * 0.40, w * 0.28
            raw = avail - 2 * (self.R + cpad) - (2 * self.Rr + rpad) - 2 * g
            self.tier2_h = min(raw, h * 0.345)
            extra = (raw - self.tier2_h) / 2
        else:
            self.tier2_h = 0.0
            if need > avail:   # short canvas: shrink both circles to fit
                f = (avail - g - 2 * cpad - rpad) / (2 * R0 + 2 * Rr0)
                self.R, self.Rr = R0 * f, Rr0 * f
                extra = 0.0
            else:
                self.R, self.Rr = R0, Rr0
                extra = (avail - need) / 2
        self.ccx, self.ccy = w / 2, y0 + self.R + cpad
        yb = y0 + 2 * (self.R + cpad) + g + extra
        self.rcx = pad + self.Rr + w * 0.012
        self.rcy = yb + self.Rr
        self.y_tier2 = yb + 2 * self.Rr + rpad + g + extra

        self.rw = max(4, int(w * 0.019))                  # ring stroke
        self.step = self.rw + max(3, int(w * 0.011))      # ring pitch
        self.r_core = self.R - 3 * self.step - self.rw - w * 0.055

        # telemetry cells: 2 cols x 5 rows, value positions kept for render
        cw = (w - 2 * pad - w * 0.04) / 2
        self.cells = []
        for i in range(10):
            cx = pad + (i % 2) * (cw + w * 0.04)
            cy = self.y_data + (i // 2) * self.rp
            self.cells.append((cx, cy, cw))
        self._bg = self._build_bg()

    # --- static layer ---------------------------------------------------------
    def _hex_grid(self, d) -> None:
        a = max(14, self.w / 16)
        col = mix(self.pal.bg, self.pal.grid, 0.6)
        dx, dy = a * math.sqrt(3), a * 1.5
        row = 0
        yy = -a
        while yy < self.h + a:
            off = (dx / 2) if row % 2 else 0.0
            xx = -a + off
            while xx < self.w + a:
                pts = [(xx + a * math.sin(math.radians(60 * k)),
                        yy - a * math.cos(math.radians(60 * k))) for k in range(6)]
                d.polygon(pts, outline=col)
                xx += dx
            yy += dy
            row += 1

    def _soft_glow(self, img, cx, cy, r, colour, alpha) -> None:
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(lay).ellipse([cx - r, cy - r, cx + r, cy + r],
                                    fill=tuple(colour) + (alpha,))
        img.alpha_composite(lay.filter(ImageFilter.GaussianBlur(r * 0.55)))

    def _build_bg(self) -> Image.Image:
        w, h, pad = self.w, self.h, self.pad
        img = Image.new("RGBA", (w, h), tuple(self.pal.bg) + (255,))
        d = ImageDraw.Draw(img)
        self._hex_grid(d)
        cy = self.pal.base("cpu")
        # halos behind the two circles — blurred once here, never per frame
        self._soft_glow(img, self.ccx, self.ccy, self.R * 0.95, cy, 26)
        self._soft_glow(img, self.rcx, self.rcy, self.Rr * 0.95,
                        self.pal.base("ram"), 22)
        d = ImageDraw.Draw(img)

        # frame: side rails + corner brackets
        m = w * 0.012
        for x in (m, w - m):
            d.line([(x, 0), (x, h)], fill=scale(cy, 0.45), width=2)
        ln = w * 0.09
        for bx, by, sx, sy in ((m, m, 1, 1), (w - m, m, -1, 1),
                               (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
            d.line([(bx, by), (bx + ln * sx, by)], fill=cy, width=3)
            d.line([(bx, by), (bx, by + ln * sy)], fill=cy, width=3)

        # header rule, seconds track + 5s ticks
        ry = self.y_hdr + self.f_small.size * 1.5
        d.line([(pad, ry), (w - pad, ry)], fill=scale(cy, 0.5), width=2)
        sy = self.y_sec + self.sec_h / 2
        d.line([(pad, sy), (w - pad, sy)], fill=scale(cy, 0.30), width=1)
        for i in range(13):
            tx = pad + (w - 2 * pad) * i / 12
            d.line([(tx, sy - 3), (tx, sy + 3)], fill=scale(cy, 0.45), width=1)

        # cluster reticle: 60-tick ring, guide circles, crosshair
        cx, cc = self.ccx, self.ccy
        for i in range(60):
            a = math.radians(i * 6)
            r0 = self.R + w * 0.055
            r1 = r0 + (w * 0.022 if i % 5 == 0 else w * 0.011)
            d.line([(cx + math.cos(a) * r0, cc + math.sin(a) * r0),
                    (cx + math.cos(a) * r1, cc + math.sin(a) * r1)],
                   fill=scale(cy, 0.55 if i % 5 == 0 else 0.32), width=1)
        for r in (self.R + w * 0.05, self.r_core):
            d.ellipse([cx - r, cc - r, cx + r, cc + r],
                      outline=scale(self.pal.dim, 0.35))
        for a in (0, 90):
            aa = math.radians(a)
            for s in (-1, 1):
                d.line([(cx + math.cos(aa) * self.r_core * 0.25 * s,
                         cc + math.sin(aa) * self.r_core * 0.25 * s),
                        (cx + math.cos(aa) * self.r_core * 0.55 * s,
                         cc + math.sin(aa) * self.r_core * 0.55 * s)],
                       fill=scale(self.pal.dim, 0.4), width=1)

        # radar dial: outer ring, range rings, crosshair, 30° bearing ticks
        rb = self.pal.base("ram")
        rx, rc, rr = self.rcx, self.rcy, self.Rr
        d.ellipse([rx - rr, rc - rr, rx + rr, rc + rr],
                  outline=scale(rb, 0.7), width=2)
        for f in (0.33, 0.66):
            q = rr * f
            d.ellipse([rx - q, rc - q, rx + q, rc + q], outline=scale(rb, 0.32))
        d.line([(rx - rr, rc), (rx + rr, rc)], fill=scale(rb, 0.30))
        d.line([(rx, rc - rr), (rx, rc + rr)], fill=scale(rb, 0.30))
        for i in range(12):
            a = math.radians(i * 30)
            d.line([(rx + math.cos(a) * rr, rc + math.sin(a) * rr),
                    (rx + math.cos(a) * (rr + w * 0.016),
                     rc + math.sin(a) * (rr + w * 0.016))],
                   fill=scale(rb, 0.6), width=1)

        # telemetry section rule + bracket-framed cells with static labels
        ty = self.y_data - self.f_label.size * 1.7
        d.text((pad, ty), "TELEMETRY", font=self.f_label, fill=cy)
        lw = d.textlength("TELEMETRY", font=self.f_label)
        d.line([(pad + lw + w * 0.03, ty + self.f_label.size * 0.55),
                (w - pad, ty + self.f_label.size * 0.55)],
               fill=scale(cy, 0.4), width=1)
        bc = scale(self.pal.dim, 0.55)
        for i, (cxx, cyy, cw) in enumerate(self.cells):
            k = min(10, cw * 0.07)
            for ex, ey, sx, sy_ in ((cxx, cyy, 1, 1), (cxx + cw, cyy, -1, 1),
                                    (cxx, cyy + self.row_h, 1, -1),
                                    (cxx + cw, cyy + self.row_h, -1, -1)):
                d.line([(ex, ey), (ex + k * sx, ey)], fill=bc, width=1)
                d.line([(ex, ey), (ex, ey + k * sy_)], fill=bc, width=1)
            d.text((cxx + cw * 0.05, cyy + self.row_h * 0.12),
                   CELL_LABELS[i], font=self.f_tiny, fill=self.pal.dim)
        return img

    # --- widgets --------------------------------------------------------------
    def _clock_img(self, hhmm: str) -> Image.Image:
        """Glowed clock, rebuilt once a minute — a full-canvas GaussianBlur per
        frame is ~17ms at 480x1920, which alone would blow the render budget."""
        cached = getattr(self, "_clock_cache", None)
        if cached and cached[0] == hhmm:
            return cached[1]
        f = self.f_time
        m = int(f.size * 0.30)
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        tw = int(probe.textlength(hhmm, font=f))
        lay = Image.new("RGBA", (tw + 2 * m, int(f.size * 1.3) + 2 * m),
                        (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        ld.text((m, m), hhmm, font=f, fill=tuple(self.col("cpu")) + (215,))
        lay = lay.filter(ImageFilter.GaussianBlur(max(2, f.size * 0.07)))
        ImageDraw.Draw(lay).text((m, m), hhmm, font=f, fill=self.pal.fg)
        self._clock_cache = (hhmm, lay)
        return lay

    def _ribbon_local(self, img, x, y, bw, bh, histo, base):
        """fx.ribbon allocates a full-canvas layer per call; four of those per
        frame cost ~9ms at 480x1920, so this one works in its own box."""
        lay = Image.new("RGBA", (int(bw) + 2, int(bh) + 2), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        stp = bw / (len(histo) - 1)
        pts = [(1 + j * stp, 1 + bh - vv * bh * 0.94)
               for j, vv in enumerate(histo)]
        ld.polygon([(pts[0][0], bh + 1)] + pts + [(pts[-1][0], bh + 1)],
                   fill=tuple(base) + (60,))
        ld.line(pts, fill=base, width=2)
        img.alpha_composite(lay, (int(x) - 1, int(y) - 1))

    def _dash_ring(self, d, cx, cy, r, n, span, colour, rot, width=2):
        box = [cx - r, cy - r, cx + r, cy + r]
        for i in range(n):
            a = rot + i * 360 / n
            d.arc(box, a, a + span, fill=colour, width=width)

    def _cluster(self, d, v):
        w, cx, cy = self.w, self.ccx, self.ccy
        fracs = {"cpu": v.load / 100,
                 "gpu": (v.gpu.get("util", 0.0)) / 100,
                 "ram": v.mem_pct / 100, "ssd": v.disk_pct / 100}
        vals = {"cpu": v.load, "gpu": v.gpu.get("util", 0.0),
                "ram": v.mem_pct, "ssd": v.disk_pct}

        # counter-rotating dashed orbits, one outside, one inside the rings
        rot = self.t * 360 * hz(abs(self.RPS_ORBIT))
        self._dash_ring(d, cx, cy, self.R + w * 0.038, 48, 3.4,
                        scale(self.pal.base("cpu"), 0.7), -rot)
        self._dash_ring(d, cx, cy, self.R - 3 * self.step - self.rw - w * 0.026,
                        30, 5.0, scale(self.pal.base("ram"), 0.55),
                        self.t * 360 * hz(self.RPS_ORBIT2), width=1)

        Re = min(self.R + w * 0.10, w * 0.46)
        for key, label, k, theta in CALLOUTS:
            base, hot = self.col(key), self.col(key, True)
            rk = self.R - k * self.step
            fr = self.ease(f"ring:{key}", fracs[key])
            self.arc_gauge(d, cx, cy, rk, fr, base, width=self.rw,
                           a0=self.A0, a1=self.A1, track=scale(base, 0.20))
            ae = math.radians(self.A0 + (self.A1 - self.A0) * max(0.003, fr))
            ex, ey = cx + math.cos(ae) * rk, cy + math.sin(ae) * rk
            rr = self.rw * 0.55
            d.ellipse([ex - rr, ey - rr, ex + rr, ey + rr], fill=hot)

            # callout: radial connector to an elbow, then out to the edge
            th = math.radians(theta)
            ax, ay = cx + math.cos(th) * rk, cy + math.sin(th) * rk
            bx, by = cx + math.cos(th) * Re, cy + math.sin(th) * Re
            left = math.cos(th) < 0
            tx = self.pad if left else w - self.pad
            d.line([(ax, ay), (bx, by), (tx, by)], fill=scale(base, 0.75), width=1)
            d.ellipse([ax - 2.5, ay - 2.5, ax + 2.5, ay + 2.5], fill=hot)
            pct = f"{vals[key]:.0f}%"
            ty = by - self.f_label.size * 1.35
            if left:
                d.text((tx, ty + self.f_label.size - self.f_small.size),
                       label, font=self.f_small, fill=base)
                d.text((tx + d.textlength(label, font=self.f_small) + 5, ty),
                       pct, font=self.f_label, fill=self.pal.fg)
            else:
                pw = d.textlength(pct, font=self.f_label)
                d.text((tx - pw, ty), pct, font=self.f_label, fill=self.pal.fg)
                lwx = d.textlength(label, font=self.f_small)
                d.text((tx - pw - lwx - 5,
                        ty + self.f_label.size - self.f_small.size),
                       label, font=self.f_small, fill=base)

        # per-core dots on their own ring, intensity by that core's load
        cores = v.per_core or [0.0]
        n = len(cores)
        base, hot = self.col("cpu"), self.col("cpu", True)
        for i, load in enumerate(cores):
            a = math.radians(-90 + i * 360 / n)
            px = cx + math.cos(a) * self.r_core
            py = cy + math.sin(a) * self.r_core
            f = max(0.0, min(1.0, load / 100))
            rr = w * 0.007 + w * 0.005 * f
            d.ellipse([px - rr, py - rr, px + rr, py + rr],
                      fill=mix(scale(base, 0.22), hot, f))

        # centre readout — the number snaps on the sample clock
        val = f"{v.load:.0f}%"
        f_c = self.fit_text(d, val, self.f_center, self.r_core * 1.7, floor=14)
        vw = d.textlength(val, font=f_c)
        d.text((cx - vw / 2, cy - f_c.size * 0.78), val, font=f_c,
               fill=self.pal.fg)
        sub = "CPU LOAD"
        d.text((cx - d.textlength(sub, font=self.f_tiny) / 2,
                cy + f_c.size * 0.36), sub, font=self.f_tiny, fill=self.pal.dim)
        ghz = f"{v.freq_ghz:.1f} GHZ"
        d.text((cx - d.textlength(ghz, font=self.f_tiny) / 2,
                cy + f_c.size * 0.36 + self.f_tiny.size * 1.25),
               ghz, font=self.f_tiny, fill=base)

    def _radar(self, img, d, v):
        w, rx, rc, rr = self.w, self.rcx, self.rcy, self.Rr
        base, hot = self.pal.base("ram"), self.pal.hot("ram")

        # contacts frozen per ~2s window; count rides procs + net activity
        rb = self.wrng(self.HZ_BLIP, salt=0xB11)
        blips = [(rb.uniform(0, 360), rb.uniform(0.18, 0.88)) for _ in range(4)]
        n = max(2, min(4, 2 + v.procs // 400
                       + (1 if v.net_down > 2 ** 20 else 0)))

        s = int(2 * rr + 4)
        lay = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        box = [2, 2, s - 2, s - 2]
        sweep = (self.t * hz(self.RPS_SWEEP) * 360) % 360
        for k in range(9):               # trailing wedge, alpha decays behind
            a1 = sweep - k * 7
            ld.pieslice(box, a1 - 7, a1, fill=tuple(base) + (int(66 * (1 - k / 9)),))
        ea = math.radians(sweep)
        ld.line([(s / 2, s / 2),
                 (s / 2 + math.cos(ea) * (rr - 2), s / 2 + math.sin(ea) * (rr - 2))],
                fill=tuple(hot) + (210,), width=2)
        for i, (ba, bf) in enumerate(blips[:n]):
            delta = (sweep - ba) % 360
            inten = math.exp(-delta / 70)
            px = s / 2 + math.cos(math.radians(ba)) * bf * (rr - 6)
            py = s / 2 + math.sin(math.radians(ba)) * bf * (rr - 6)
            q = 2.5 + 2.5 * inten
            ld.ellipse([px - q, py - q, px + q, py + q],
                       fill=tuple(hot) + (int(90 + 165 * inten),))
            if inten > 0.12:             # echo ring expands as the sweep ages
                q2 = q + delta * 0.09
                ld.ellipse([px - q2, py - q2, px + q2, py + q2],
                           outline=tuple(base) + (int(150 * inten),))
        img.alpha_composite(lay, (int(rx - rr - 2), int(rc - rr - 2)))
        d = ImageDraw.Draw(img)
        d.ellipse([rx - 3, rc - 3, rx + 3, rc + 3], fill=hot)

        # contact list column, values frozen with the same window RNG
        x0 = rx + rr + w * 0.05
        y = rc - rr + self.f_tiny.size * 0.2
        d.text((x0, y), "SCAN", font=self.f_label, fill=base)
        self._pips(d, x0 + d.textlength("SCAN", font=self.f_label) + w * 0.02,
                   y + self.f_label.size * 0.34, self.f_label.size * 0.22, hot, 1,
                   dark=scale(self.pal.dim, 0.3))
        y += self.f_label.size * 1.6
        d.text((x0, y), f"CONTACTS {n}", font=self.f_tiny, fill=self.pal.dim)
        y += self.f_tiny.size * 1.55
        for i, (ba, bf) in enumerate(blips[:n]):
            d.text((x0, y), f"TGT{i + 1} {int(ba):03d}° R{int(bf * 99):02d}",
                   font=self.f_tiny, fill=mix(self.pal.dim, base, 0.5))
            y += self.f_tiny.size * 1.35
        y += self.f_tiny.size * 0.5
        d.text((x0, y), "SWP .32R/S", font=self.f_tiny, fill=self.pal.dim)
        y += self.f_tiny.size * 1.5
        d.text((x0, y), "LNK", font=self.f_tiny, fill=base)
        bw = self.w - self.pad - x0 - d.textlength("LNK ", font=self.f_tiny) - 4
        self.seg_bar(d, x0 + d.textlength("LNK ", font=self.f_tiny) + 4,
                     y + self.f_tiny.size * 0.15, bw, self.f_tiny.size * 0.6,
                     self.ease("lnk", min(1.0, v.net_down / (10 * 2 ** 20))),
                     10, base, scale(base, 0.18))
        step = int(self.t * hz(self.HZ_MARK))
        marker = "".join("█" if (step + i) % 6 < 3 else "░" for i in range(6))
        d.text((x0, rc + rr - self.f_tiny.size * 1.2), marker,
               font=self.f_tiny, fill=base)
        return d

    def _tier2(self, img, d, v):
        """Tall canvas only: four ribbon meters in the leftover mid space."""
        w, pad = self.w, self.pad
        avail = w - 2 * pad
        dv, du = human_rate(v.net_down)
        gpu = v.gpu
        # LOAD not CPU% here — the cluster centre already owns that number
        ncore = max(1, len(v.per_core))
        l1 = os.getloadavg()[0]
        rows = [("LOAD", min(1.0, l1 / ncore), f"{l1:.2f}", "cpu",
                 f"{ncore} CORES"),
                ("GPU", gpu.get("util", 0.0) / 100,
                 f"{gpu['util']:.0f}%" if gpu else "--", "gpu",
                 f"{gpu['power']:.0f}W" if gpu else ""),
                ("MEM", v.mem_pct / 100, f"{v.mem_used:.1f}G", "ram",
                 f"OF {v.mem_total:.0f}G"),
                ("NET", min(1.0, v.net_down / (40 * 2 ** 20)), f"{dv}{du[0]}",
                 "ssd", "DOWNLINK")]
        slot = self.tier2_h / len(rows)
        for i, (label, frac, val, key, sub) in enumerate(rows):
            base, hot = self.col(key), self.col(key, True)
            y = self.y_tier2 + i * slot
            d.text((pad, y), label, font=self.f_label, fill=base)
            self._pips(d, pad + d.textlength(label, font=self.f_label) + w * 0.02,
                       y + self.f_label.size * 0.34, self.f_label.size * 0.22,
                       hot, i, dark=scale(self.pal.dim, 0.3))
            if sub:
                d.text((w - pad - d.textlength(sub, font=self.f_tiny), y),
                       sub, font=self.f_tiny, fill=self.pal.dim)
            f_val = load_font(max(16, int(slot * 0.34)))
            f_val = self.fit_text(d, val, f_val, avail * 0.36, floor=14)
            vy = y + self.f_label.size * 1.25
            d.text((pad, vy), val, font=f_val, fill=self.pal.fg)
            rx = pad + avail * 0.42
            rib_h = slot * 0.42
            histo = self.hist(f"hr:{label}", frac, n=40)
            self._ribbon_local(img, rx, vy, avail * 0.58, rib_h, histo, base)
            d = ImageDraw.Draw(img)
            d.line([(rx, vy + rib_h), (rx + avail * 0.58, vy + rib_h)],
                   fill=scale(base, 0.45), width=1)
            self.seg_bar(d, pad, y + slot * 0.80, avail, slot * 0.09,
                         self.ease(f"t2:{label}", frac), 30,
                         base, scale(base, 0.16))
        return d

    # --- frame ----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        w, h, pad = self.w, self.h, self.pad
        img = self._bg.copy()
        d = ImageDraw.Draw(img)
        cy, cyh = self.col("cpu"), self.col("cpu", True)
        tl = self.col("gpu")

        now = time.localtime()
        v = vitals()

        # --- header strip ---
        y = self.y_hdr
        self._pips(d, pad, y + self.f_small.size * 0.30, self.f_small.size * 0.38,
                   self.col("gpu", True), 0, dark=scale(self.pal.dim, 0.3))
        d.text((pad + self.f_small.size * 2.4, y), "SYS ONLINE",
               font=self.f_small, fill=tl)
        d.text((w - pad - d.textlength(self.host, font=self.f_small), y),
               self.host, font=self.f_small, fill=self.pal.dim)

        # --- clock + fractional-seconds track ---
        hhmm = time.strftime("%H:%M", now)
        clock = self._clock_img(hhmm)
        m = int(self.f_time.size * 0.30)
        img.alpha_composite(clock, (int((w - clock.width) / 2),
                                    int(self.y_clock) - m))
        sec = time.time() % 60.0        # continuous: it is the clock itself
        sy = self.y_sec + self.sec_h / 2
        fx_ = pad + (w - 2 * pad) * sec / 60.0
        d.line([(pad, sy), (fx_, sy)], fill=cy, width=self.sec_h)
        r = self.sec_h * 1.1
        d.ellipse([fx_ - r, sy - r, fx_ + r, sy + r], fill=cyh)
        d.text((pad, self.y_date), time.strftime("%a %d/%m/%Y", now).upper(),
               font=self.f_small, fill=self.pal.dim)
        st = f"{int(sec):02d}S"
        d.text((w - pad - d.textlength(st, font=self.f_small), self.y_date),
               st, font=self.f_small, fill=cy)

        # --- the ring cluster, the radar, the optional tier ---
        self._cluster(d, v)
        d = self._radar(img, d, v)
        if self.tall:
            d = self._tier2(img, d, v)

        # --- telemetry cells (labels/brackets are in the static layer) ---
        gpu = v.gpu
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        rd, ru = human_rate(v.disk_rd)
        wr_, wu = human_rate(v.disk_wr)
        l1, l5, _ = os.getloadavg()
        up = v.uptime
        vram = (f"{gpu['mem_used'] / 1024:.1f}/{gpu['mem_total'] / 1024:.0f}G"
                if gpu else "--")
        cells = [(f"{v.temp:.0f}°C" if v.temp else "--", self.col("cpu")),
                 (f"{gpu['temp']:.0f}°C" if gpu else "--", self.col("gpu")),
                 (f"{v.nvme_temp:.0f}°C" if v.nvme_temp else "--", self.col("ssd")),
                 (vram, self.col("gpu")),
                 (f"{dv} {du}", self.col("cpu")),
                 (f"{uv} {uu}", self.col("ram")),
                 (f"{l1:.2f} {l5:.2f}", self.pal.fg),
                 (f"{v.procs}", self.pal.fg),
                 (f"{up // 86400}D {up % 86400 // 3600:02d}H {up % 3600 // 60:02d}M",
                  self.pal.dim),
                 (f"R{rd} W{wr_}", self.col("ssd"))]
        for (cx, cyy, cw), (txt, col) in zip(self.cells, cells):
            f = self.fit_text(d, txt, self.f_cell, cw * 0.9, mono=True)
            d.text((cx + cw * 0.05, cyy + self.row_h * 0.12 + self.f_tiny.size * 1.15),
                   txt, font=f, fill=col)

        self._scanline(img, colour=cy)

        # --- hologram artefact: shift one slice for a whole glitch window ---
        gr = self.wrng(self.HZ_GLITCH, salt=0x6717C4)
        if gr.random() < self.GLITCH_P:
            gy = gr.randrange(int(h * 0.05), int(h * 0.90))
            gh = max(3, int(h * gr.uniform(0.004, 0.012)))
            dx = gr.choice((-1, 1)) * gr.randrange(max(3, int(w * 0.008)),
                                                   max(6, int(w * 0.03)))
            img.paste(img.crop((0, gy, w, gy + gh)), (dx, gy))
        return img.convert("RGB")


VIEWS = {"holo-ring": HoloRingView}
