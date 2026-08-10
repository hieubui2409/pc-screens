"""deep-space — starship bridge viewport + navigation console (horizontal).

A warp starfield streams across the canvas at a speed riding CPU load, past a
ringed planet orbited by two moons and four metric bodies (CPU/GPU/MEM/SSD —
angular speed maps to the metric). The left console: SHIP TIME clock, stardate,
voyage uptime, a per-core thruster array, comms rates, RAM/VRAM reserves. A CPU
sample jumping >15 points launches a comet. Signature palette is deep blues;
--palette recolours everything.

Layout is x-column based so 1920x480 and 960x480 both work: planet zone width
derives from the planet radius, the three HUD columns split what remains.
"""

from __future__ import annotations

import math
import os
import random
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from fx import (FXBase, Palette, dens, human_rate, hz, load_mono, mix,
                sample_serial, scale, vitals)

SPACE = Palette("deep-space",
                ((127, 178, 255), (79, 107, 255), (234, 242, 255), (160, 120, 255)),
                bg=(3, 4, 12), grid=(13, 17, 34), dim=(118, 134, 172))


class DeepSpaceView(FXBase):
    WARP_BASE = 0.40     # starfield speed factor at idle...
    WARP_LOAD = 1.70     # ...plus this much at 100% CPU (eased)
    COMET_S = 1.2        # comet flight time
    ORB_IDLE = 0.10      # orbit body rad/s at 0%...
    ORB_FULL = 2.30      # ...added at 100%
    RING_RPS = 0.055     # ring band drift, fraction of ring width per second

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or SPACE
        self._start(0xD5E9)
        self.host = os.uname().nodename.upper()[:12]

        pad = max(10, int(h * 0.035))
        self.pad = pad

        # --- planet zone geometry (right side) ---
        self.pr = pr = min(h * 0.34, w * 0.078)
        self.pcx = w - pr * 2.85
        self.pcy = h * 0.585
        self.orx = [pr * (1.75 + 0.30 * i) for i in range(4)]   # orbit x-radii
        self.ory = [rx * 0.34 for rx in self.orx]
        self.px0 = self.pcx - self.orx[-1] - pad * 0.8          # zone left edge
        self._oph = [i * 1.7 for i in range(4)]                 # orbit phases

        # --- HUD columns fill the rest ---
        gap = pad
        self.lx0 = pad * 1.2
        avail = self.px0 - self.lx0 - pad - 2 * gap
        self.cA = (self.lx0, avail * 0.38)
        self.cB = (self.cA[0] + self.cA[1] + gap, avail * 0.33)
        self.cC = (self.cB[0] + self.cB[1] + gap, avail * 0.29)

        # --- fonts, clock probed against column A ---
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        size = int(h * 0.20)
        while size > 12 and probe.textlength("00:00:00",
                                             font=load_mono(size)) > self.cA[1] * 0.99:
            size = int(size * 0.93)
        self.f_time = load_mono(size)
        self.f_head = load_mono(max(10, int(h * 0.032)))
        self.f_label = load_mono(max(9, int(h * 0.027)))
        self.f_small = load_mono(max(9, int(h * 0.0235)))
        self.f_tiny = load_mono(max(8, int(h * 0.0195)))

        # --- starfield: 3 parallax layers, positions fixed at init ---
        # Counts scale with width; sized so 60 renders at 1920x480 stay <18ms.
        aw = w / 1920
        srng = random.Random(0x57A2)
        self.layers = []
        for count, spd, sz, bright, streak in (
                (int(110 * aw), 26.0, 1, 0.40, False),
                (int(72 * aw), 55.0, 2, 0.65, False),
                (int(44 * aw), 110.0, 2, 1.00, True)):
            stars = []
            for _ in range(count):
                cf = srng.uniform(0.35, 1.0)
                col = scale(mix(self.pal.dim, self.pal.fg, cf), bright)
                stars.append((srng.uniform(0, w), srng.uniform(h * 0.05, h * 0.95),
                              sz, col))
            self.layers.append((stars, spd, streak))

        # --- comet state ---
        self._comet_t0: float | None = None
        self._comet_seed = 0
        self._comet_fired = False
        self._prev_load: float | None = None
        self._v_serial = -1

        # moons: (x-radius, y-radius, body radius, period s, phase0)
        self.moons = ((pr * 1.52, pr * 0.55, max(3, pr * 0.085), 23.0, 0.9),
                      (pr * 2.42, pr * 0.80, max(2, pr * 0.055), 47.0, 3.8))

        # The frame canvas stays RGB: ImageDraw's "RGBA" blend mode only blends
        # onto RGB images (on RGBA it overwrites, leaving opaque blocks).
        self._planet = self._build_planet()
        self._bg = self._build_bg()

        # callout boxes: left of the orbits, connected to each orbit's vertex
        ch_ = self.f_small.size + self.f_tiny.size + pad * 0.9
        cw_ = min(self.pcx - self.orx[0] - self.px0 + pr * 0.9, pr * 2.1)
        cy0 = self.pcy - (ch_ * 4 + pad * 0.6 * 3) / 2
        self._call = [(self.px0, cy0 + i * (ch_ + pad * 0.6), cw_, ch_)
                      for i in range(4)]

    # --- one-time builds -------------------------------------------------------
    def _build_planet(self) -> Image.Image:
        """Shaded sphere + terminator, built once — too costly per frame."""
        pr = int(self.pr)
        S = pr * 2 + 8
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        base = self.pal.base("gpu")
        edge = mix(self.pal.bg, base, 0.30)
        lit = mix(base, (255, 255, 255), 0.42)
        steps = max(24, pr // 2)
        for i in range(steps + 1):          # off-centre radial gradient
            f = i / steps
            r = pr * (1 - f * 0.97)
            cx = S / 2 - pr * 0.30 * f
            cy = S / 2 - pr * 0.24 * f
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=mix(edge, lit, f ** 1.35))

        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).ellipse([S / 2 - pr, S / 2 - pr, S / 2 + pr, S / 2 + pr],
                                     fill=255)
        # latitude bands, clipped to the disc
        ov = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        dark = scale(base, 0.35)
        for fy, th in ((0.34, 0.06), (0.52, 0.09), (0.70, 0.05)):
            od.rectangle([0, S * fy, S, S * (fy + th)], fill=tuple(dark) + (70,))
        ov.putalpha(ImageChops.multiply(ov.getchannel("A"), mask))
        img.alpha_composite(ov)
        # terminator: night side away from the console lights
        sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        x0 = S * 0.48
        for x in range(int(x0), S, 2):
            a = int(175 * max(0.0, (x - x0) / (S - x0)) ** 1.4)
            sd.rectangle([x, 0, x + 2, S], fill=(0, 0, 6, a))
        sh.putalpha(ImageChops.multiply(sh.getchannel("A"), mask))
        img.alpha_composite(sh)
        # thin atmosphere rim
        rd = ImageDraw.Draw(img, "RGBA")
        for k, a in ((1, 110), (2, 60), (4, 30)):
            rd.ellipse([S / 2 - pr - k, S / 2 - pr - k, S / 2 + pr + k, S / 2 + pr + k],
                       outline=tuple(scale(base, 0.8)) + (a,), width=1)
        return img

    def _build_bg(self) -> Image.Image:
        """Static layer: nebulae, distant stars, console chrome."""
        w, h, pad = self.w, self.h, self.pad
        img = Image.new("RGB", (w, h), self.pal.bg)
        d = ImageDraw.Draw(img, "RGBA")
        for fx_, fy_, fr, key in ((0.16, 0.28, 0.52, "cpu"),
                                  (0.52, 0.80, 0.46, "ssd"),
                                  (0.88, 0.18, 0.58, "gpu")):
            c = scale(self.pal.base(key), 0.55)
            R = h * fr
            for s in range(10):
                r = R * (1 - s / 10)
                d.ellipse([w * fx_ - r * 1.6, h * fy_ - r, w * fx_ + r * 1.6, h * fy_ + r],
                          fill=tuple(c) + (4,))
        srng = random.Random(0xB6)
        dust = scale(self.pal.dim, 0.55)
        for _ in range(int(120 * w / 1920)):
            d.point((srng.uniform(0, w), srng.uniform(0, h)), fill=dust)

        cy = self.pal.base("cpu")
        line = scale(cy, 0.45)
        top, bot = h * 0.062, h - h * 0.062
        d.line([(pad, top), (w - pad, top)], fill=line, width=1)
        d.line([(pad, bot), (w - pad, bot)], fill=line, width=1)
        ln = w * 0.03
        for cx, cyy, sx, sy in ((pad, pad * 0.5, 1, 1), (w - pad, pad * 0.5, -1, 1),
                                (pad, h - pad * 0.5, 1, -1),
                                (w - pad, h - pad * 0.5, -1, -1)):
            d.line([(cx, cyy), (cx + ln * sx, cyy)], fill=cy, width=3)
            d.line([(cx, cyy), (cx, cyy + pad * 0.9 * sy)], fill=cy, width=3)
        # static header text (dynamic values are drawn per frame)
        d.text((pad * 1.2, h * 0.014), f"USS {self.host}", font=self.f_head,
               fill=self.pal.fg)
        mid = "DEEP-SPACE NAV CONSOLE"
        d.text(((w - d.textlength(mid, font=self.f_tiny)) / 2, h * 0.024), mid,
               font=self.f_tiny, fill=self.pal.dim)
        # HUD column separators
        sep = scale(cy, 0.22)
        for x in (self.cB[0] - pad * 0.5, self.cC[0] - pad * 0.5, self.px0 - pad * 0.4):
            d.line([(x, h * 0.095), (x, h * 0.90)], fill=sep, width=1)
        lbl = "ORBITAL TELEMETRY"
        d.text((self.px0 + pad * 0.4, h * 0.075), lbl, font=self.f_tiny,
               fill=scale(self.pal.dim, 0.85))
        # a distant sun with diffraction spikes fills the sky above the orbits
        sx, sy = self.pcx - self.pr * 1.1, h * 0.16
        sun = self.pal.hot("ram")
        for ln2, wd2 in ((self.pr * 0.55, 1), (self.pr * 0.22, 2)):
            d.line([(sx - ln2, sy), (sx + ln2, sy)], fill=scale(sun, 0.5), width=wd2)
            d.line([(sx, sy - ln2 * 0.7), (sx, sy + ln2 * 0.7)],
                   fill=scale(sun, 0.5), width=wd2)
        for r, a in ((6, 40), (3.5, 110), (2, 255)):
            d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=tuple(sun) + (a,))
        return img

    # --- per-frame pieces ------------------------------------------------------
    def _ribbon(self, img, pts, base, baseline, area_alpha=60, width=2):
        """fx.ribbon composites a full-canvas layer per call; three of those
        per frame blow the 18ms budget, so blend the fill in-place instead."""
        da = ImageDraw.Draw(img, "RGBA")
        da.polygon([(pts[0][0], baseline)] + list(pts) + [(pts[-1][0], baseline)],
                   fill=tuple(base) + (area_alpha,))
        da.line(pts, fill=tuple(base) + (255,), width=width)

    def _glow_local(self, img, text, font, xy, colour, alpha=215):
        """glow_text but blurring only the text's own patch, not the canvas —
        a full 1920px Gaussian pass per frame would blow the render budget."""
        d0 = ImageDraw.Draw(img)
        p = max(6, font.size // 3)
        tw = int(d0.textlength(text, font=font)) + p * 2
        th = int(font.size * 1.4) + p * 2
        layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((p, p), text, font=font,
                                   fill=tuple(colour) + (alpha,))
        layer = layer.filter(ImageFilter.GaussianBlur(max(2, font.size * 0.07)))
        img.paste(layer, (max(0, int(xy[0] - p)), max(0, int(xy[1] - p))), layer)
        d0.text(xy, text, font=font, fill=self.pal.fg)

    def _stars(self, d, sf: float):
        w, h = self.w, self.h
        for stars, spd0, streak in self.layers:
            spd = spd0 * sf
            ln = min(w * 0.03, spd * 0.10)
            for x0, y, s, col in stars:
                x = (x0 - self.t * spd) % (w + 60) - 30
                c = scale(col, 0.45) if x < self.px0 else col   # keep HUD legible
                if streak and spd > 70:
                    d.line([(x, y), (x + ln, y)], fill=c, width=s)
                else:
                    d.rectangle([x, y, x + s, y + s], fill=c)

    def _comet(self, d):
        if self._comet_t0 is None:
            return
        p = (self.t - self._comet_t0) / self.COMET_S
        if p >= 1.0:
            self._comet_t0 = None
            return
        w, h = self.w, self.h
        rng = random.Random(self._comet_seed)
        ya = rng.uniform(h * 0.08, h * 0.30)
        yb = ya + rng.uniform(-h * 0.10, h * 0.18)
        flip = rng.random() < 0.35
        xa, xb = (-w * 0.08, w * 1.08) if not flip else (w * 1.08, -w * 0.08)
        hx = xa + (xb - xa) * p
        hy = ya + (yb - ya) * p
        hot = self.pal.hot("ram")
        for k in range(9, 0, -1):           # fading tail behind the head
            t2 = max(0.0, p - k * 0.022)
            tx, ty = xa + (xb - xa) * t2, ya + (yb - ya) * t2
            d.line([(tx, ty), (hx, hy)], fill=scale(hot, 0.12 + 0.8 * (1 - k / 9)),
                   width=1 + (k < 4))
        r = 3
        d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=hot)
        self._sparks(d, hx, hy, 4, hot, dens(3))

    def _moon(self, d, x, y, r, behind: bool):
        body = scale(mix(self.pal.dim, self.pal.fg, 0.35), 0.55 if behind else 0.95)
        d.ellipse([x - r, y - r, x + r, y + r], fill=body)
        lr = r * 0.55
        d.ellipse([x - r * 0.5 - lr, y - r * 0.4 - lr, x - r * 0.5 + lr,
                   y - r * 0.4 + lr], fill=scale(self.pal.fg, 0.6 if behind else 1.0))

    def _ring(self, d, front: bool):
        a0, a1 = (0, 180) if front else (180, 360)
        pr, cx, cy = self.pr, self.pcx, self.pcy
        rxi, rxo = pr * 1.28, pr * 1.66
        for k in range(5):
            f = k / 4
            rx = rxi + (rxo - rxi) * f
            ry = rx * 0.26
            col = scale(mix(self.pal.base("ram"), self.pal.base("ssd"), f),
                        0.50 if front else 0.30)
            d.arc([cx - rx, cy - ry, cx + rx, cy + ry], a0, a1, fill=col, width=2)
        # 3 drifting bands stand in for the ring surface rotating
        for k in range(3):
            bp = (self.t * self.RING_RPS + k / 3) % 1.0
            rx = rxi + (rxo - rxi) * bp
            ry = rx * 0.26
            fade = 0.30 + 0.70 * math.sin(math.pi * bp)
            col = scale(self.pal.hot("ssd"), fade * (0.9 if front else 0.5))
            d.arc([cx - rx, cy - ry, cx + rx, cy + ry], a0, a1, fill=col, width=1)

    def _thruster(self, d, x, y, bw, bh, frac, i, base, hot):
        """One flame bar: dark nozzle, gradient exhaust, hot tip."""
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=bh / 2,
                            fill=(9, 11, 22), outline=scale(base, 0.30))
        d.rectangle([x, y, x + bh * 0.55, y + bh], fill=scale(base, 0.55))
        if frac > 0.02:
            flick = self.wrng(9, salt=100 + i).uniform(0.90, 1.08)
            fw = min(bw - 2, max(bh, bw * frac * flick))
            n = 6
            for k in range(n):
                t = k / n
                xx = x + bh * 0.55 + (fw - bh * 0.55) * t
                x2 = x + bh * 0.55 + (fw - bh * 0.55) * (t + 1 / n)
                d.rectangle([xx, y + 1, min(x + fw, x2 + 1), y + bh - 1],
                            fill=mix(scale(base, 0.30), hot, t ** 1.4))
            r = bh * 0.42
            d.ellipse([x + fw - r, y + bh / 2 - r, x + fw + r, y + bh / 2 + r],
                      fill=hot)

    # --- frame -----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        w, h, pad = self.w, self.h, self.pad
        v = vitals()
        gpu = v.gpu
        now = time.localtime()
        img = self._bg.copy()
        d = ImageDraw.Draw(img)
        cyb, cyh = self.col("cpu"), self.col("cpu", True)
        gpb = self.col("gpu")
        ssb = self.col("ssd")

        # comet trigger: fires on a >15pt CPU jump between samples
        serial = sample_serial("vitals")
        if serial != self._v_serial:
            if (self._prev_load is not None
                    and abs(v.load - self._prev_load) > 15 and self._comet_t0 is None):
                self._comet_t0, self._comet_seed = self.t, serial
            self._prev_load, self._v_serial = v.load, serial
        if not self._comet_fired and self.t > 0.8:   # one launch salute at start
            self._comet_fired = True
            if self._comet_t0 is None:
                self._comet_t0, self._comet_seed = self.t, 0x5A17

        # --- sky ---
        sf = self.WARP_BASE + self.WARP_LOAD * self.ease("warp", v.load / 100)
        self._stars(d, sf)
        self._comet(d)

        # --- planet stack: back moons, back ring, orbits, sphere, front ---
        pcx, pcy, pr = self.pcx, self.pcy, self.pr
        mpos = []
        for rx, ry, mr, per, ph in self.moons:
            a = math.tau * self.t / per + ph
            mpos.append((pcx + math.cos(a) * rx, pcy + math.sin(a) * ry, mr,
                         math.sin(a) < 0))
        for mx, my, mr, behind in mpos:
            if behind:
                self._moon(d, mx, my, mr, True)
        self._ring(d, front=False)
        for i in range(4):
            rx, ry = self.orx[i], self.ory[i]
            d.ellipse([pcx - rx, pcy - ry, pcx + rx, pcy + ry],
                      outline=scale(self.pal.base(i), 0.30), width=1)
        S = self._planet.width
        img.paste(self._planet, (int(pcx - S / 2), int(pcy - S / 2)), self._planet)
        d = ImageDraw.Draw(img)
        self._ring(d, front=True)
        for i in range(4):                  # near arcs pass in front of the disc
            rx, ry = self.orx[i], self.ory[i]
            d.arc([pcx - rx, pcy - ry, pcx + rx, pcy + ry], 0, 180,
                  fill=scale(self.pal.base(i), 0.40), width=1)
        for mx, my, mr, behind in mpos:
            if not behind:
                self._moon(d, mx, my, mr, False)

        # targeting reticle: corner ticks + slow-rotating dashes, chrome only
        rr = pr * 1.12
        ret = scale(self.pal.base("ram"), 0.55)
        for k in range(4):
            a0 = math.degrees(self.t * math.tau * 0.02) + k * 90 + 25
            d.arc([pcx - rr, pcy - rr, pcx + rr, pcy + rr], a0, a0 + 40,
                  fill=ret, width=1)
        for sx2, sy2 in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            cx2, cy2 = pcx + sx2 * rr * 0.78, pcy + sy2 * rr * 0.78
            d.line([(cx2, cy2), (cx2 + sx2 * pr * 0.14, cy2)], fill=ret, width=2)
            d.line([(cx2, cy2), (cx2, cy2 + sy2 * pr * 0.14)], fill=ret, width=2)
        d.text((pcx + pr * 0.35, pcy - rr - self.f_tiny.size * 1.4), "ORBIT LOCK",
               font=self.f_tiny, fill=ret)
        # live nav lines under the zone label
        ny = h * 0.075 + self.f_tiny.size * 1.5
        d.text((self.px0 + pad * 0.4, ny),
               f"REL VEL {sf:.2f}C · MOONS 2", font=self.f_tiny,
               fill=scale(self.pal.dim, 0.75))
        d.text((self.px0 + pad * 0.4, ny + self.f_tiny.size * 1.4),
               f"BODIES 4 · SCAN {int(self.t * hz(self.HZ_PIP)) % 4 + 1}/4",
               font=self.f_tiny, fill=scale(self.pal.dim, 0.75))

        # --- orbit gauges: angular speed maps to the metric ---
        fracs = (v.load / 100, (gpu["util"] / 100) if gpu else 0.0,
                 v.mem_pct / 100, v.disk_pct / 100)
        for i, frac in enumerate(fracs):
            sp = self.ORB_IDLE + self.ORB_FULL * self.ease(f"orb{i}", frac)
            self._oph[i] += self.dt * sp
            base, hot = self.col(i), self.pal.hot(i)
            rx, ry = self.orx[i], self.ory[i]
            a = self._oph[i]
            for k in range(5, 0, -1):       # trail stretches with speed
                ta = a - k * 0.055 * (1 + sp * 1.6)
                tx, ty = pcx + math.cos(ta) * rx, pcy + math.sin(ta) * ry
                tr = max(1.2, pr * 0.028 * (1 - k / 7))
                d.ellipse([tx - tr, ty - tr, tx + tr, ty + tr],
                          fill=scale(base, 0.55 - k * 0.08))
            bx, by = pcx + math.cos(a) * rx, pcy + math.sin(a) * ry
            r = max(3, pr * 0.045)
            d.ellipse([bx - r * 2.1, by - r * 2.1, bx + r * 2.1, by + r * 2.1],
                      fill=scale(base, 0.30))
            d.ellipse([bx - r, by - r, bx + r, by + r], fill=base)
            d.ellipse([bx - r * 0.5, by - r * 0.5, bx + r * 0.5, by + r * 0.5],
                      fill=hot)

        # --- callouts: dark-backed readouts wired to their orbits ---
        labels = ("CPU", "GPU", "MEM", "SSD")
        pcts = (f"{v.load:.0f}%",
                f"{gpu['util']:.0f}%" if gpu else "--",
                f"{v.mem_pct:.0f}%", f"{v.disk_pct:.0f}%")
        subs = (f"{v.temp:.0f}°C·{v.freq_ghz:.1f}GHZ" if v.temp
                else f"{v.freq_ghz:.1f}GHZ",
                f"{gpu['temp']:.0f}°C·{gpu['power']:.0f}W" if gpu else "OFFLINE",
                f"{v.mem_used:.1f}/{v.mem_total:.0f}G",
                f"{v.nvme_temp:.0f}°C·{v.disk_used:.0f}G" if v.nvme_temp
                else f"{v.disk_used:.0f}/{v.disk_total:.0f}G")
        for i, (bx, by, bw, bh) in enumerate(self._call):
            base = self.col(i)
            d.line([(bx + bw, by + bh / 2), (pcx - self.orx[i], pcy)],
                   fill=scale(base, 0.35), width=1)
            d.rectangle([bx, by, bx + bw, by + bh], fill=(5, 7, 16),
                        outline=scale(base, 0.45))
            d.line([(bx, by), (bx, by + bh)], fill=base, width=3)
            tx = bx + pad * 0.5
            d.text((tx, by + pad * 0.25), labels[i], font=self.f_small, fill=base)
            pw = d.textlength(pcts[i], font=self.f_small)
            d.text((bx + bw - pad * 0.4 - pw, by + pad * 0.25), pcts[i],
                   font=self.f_small, fill=self.pal.fg)
            sub = subs[i]
            f_sub = self.fit_text(d, sub, self.f_tiny, bw - pad, mono=True)
            d.text((tx, by + pad * 0.35 + self.f_small.size), sub,
                   font=f_sub, fill=self.pal.dim)

        # --- column A: ship time / stardate / voyage / reactor trace ---
        ax, awd = self.cA
        y = h * 0.095
        d.text((ax, y), "SHIP TIME", font=self.f_tiny, fill=cyb)
        self._pips(d, ax + d.textlength("SHIP TIME", font=self.f_tiny) + pad * 0.6,
                   y + self.f_tiny.size * 0.3, self.f_tiny.size * 0.36, cyh, 0,
                   dark=scale(self.pal.dim, 0.3))
        y += self.f_tiny.size * 1.6
        self._glow_local(img, time.strftime("%H:%M:%S", now), self.f_time,
                         (ax, y), cyb)
        d = ImageDraw.Draw(img)
        y += self.f_time.size * 1.22
        # minute progress glides — it is the clock, not a sampled reading
        sec = time.time() % 60.0
        bh = max(3, h * 0.009)
        d.rectangle([ax, y, ax + awd, y + bh], fill=(10, 13, 26))
        fw = awd * sec / 60.0
        d.rectangle([ax, y, ax + fw, y + bh], fill=cyb)
        d.ellipse([ax + fw - bh, y - bh * 0.5, ax + fw + bh, y + bh * 1.5], fill=cyh)
        y += bh + self.f_tiny.size * 0.8
        d.text((ax, y), f"STARDATE {now.tm_year}.{now.tm_yday:03d}",
               font=self.f_small, fill=cyb)
        y += self.f_small.size * 1.45
        d.text((ax, y), time.strftime("%a %d %b %Y", now).upper(),
               font=self.f_tiny, fill=self.pal.dim)
        y += self.f_tiny.size * 1.8
        up = v.uptime
        d.text((ax, y), "VOYAGE", font=self.f_tiny, fill=gpb)
        d.text((ax + d.textlength("VOYAGE ", font=self.f_tiny) + pad * 0.3, y),
               f"T+ {up // 86400}D {up % 86400 // 3600:02d}:"
               f"{up % 3600 // 60:02d}:{up % 60:02d}",
               font=self.f_tiny, fill=self.pal.fg)
        y += self.f_tiny.size * 2.0
        d.text((ax, y), "REACTOR TRACE", font=self.f_tiny,
               fill=scale(self.pal.dim, 0.9))
        y += self.f_tiny.size * 1.4
        histo = self.hist("ds:cpu", v.load / 100, n=44)
        rib_h = max(h * 0.06, h * 0.895 - y - self.f_tiny.size * 2.0)
        # graticule so the trace reads as an instrument even when idle-flat
        for k in range(1, 4):
            gy = y + rib_h * k / 4
            d.line([(ax, gy), (ax + awd, gy)], fill=scale(cyb, 0.28), width=1)
            d.text((ax + awd - self.f_tiny.size * 1.9, gy - self.f_tiny.size - 1),
                   f"{100 - k * 25}", font=self.f_tiny, fill=scale(cyb, 0.5))
        for k in range(9):
            gx = ax + awd * k / 8
            d.line([(gx, y + rib_h - 4), (gx, y + rib_h)], fill=scale(cyb, 0.45))
        step = awd / (len(histo) - 1)
        pts = [(ax + i * step, y + rib_h - vv * rib_h * 0.94)
               for i, vv in enumerate(histo)]
        self._ribbon(img, pts, cyb, y + rib_h, area_alpha=60, width=2)
        d.line([(ax, y + rib_h), (ax + awd, y + rib_h)],
               fill=scale(cyb, 0.4), width=1)
        y += rib_h + self.f_tiny.size * 0.6
        pk, av = max(histo) * 100, sum(histo) / len(histo) * 100
        d.text((ax, y), f"PK {pk:.0f}%  AV {av:.0f}%", font=self.f_tiny,
               fill=self.pal.dim)

        # --- column B: thruster array (per-core) + main drive + loadavg ---
        bxc, bwd = self.cB
        y = h * 0.095
        d.text((bxc, y), "THRUSTER ARRAY", font=self.f_tiny, fill=gpb)
        ghz = f"{len(v.per_core)}C·{v.freq_ghz:.1f}GHZ"
        d.text((bxc + bwd - d.textlength(ghz, font=self.f_tiny), y), ghz,
               font=self.f_tiny, fill=self.pal.dim)
        y += self.f_tiny.size * 1.7
        cores = v.per_core[:40]
        banks = 2 if len(cores) > 6 else 1
        rows = -(-len(cores) // banks)
        bgap = pad * 0.5
        tw_ = (bwd - bgap * (banks - 1)) / banks
        rh = min((h * 0.70 - y) / rows, h * 0.052)
        for i, c in enumerate(cores):
            bx = bxc + (i // rows) * (tw_ + bgap)
            by = y + (i % rows) * rh
            self._thruster(d, bx, by, tw_, rh * 0.64,
                           self.ease(f"core{i}", c / 100), i, cyb, cyh)
        y += rows * rh + h * 0.028
        d.text((bxc, y), "MAIN DRIVE", font=self.f_tiny, fill=cyb)
        val = f"{v.load:.0f}%"
        d.text((bxc + bwd - d.textlength(val, font=self.f_small), y - 2), val,
               font=self.f_small, fill=self.pal.fg)
        y += self.f_tiny.size * 1.5
        self.seg_bar(d, bxc, y, bwd, h * 0.022, self.ease("drive", v.load / 100),
                     14, cyb, (12, 15, 30))
        y += h * 0.022 + self.f_tiny.size * 1.1
        la = os.getloadavg()
        ncpu = max(1, len(v.per_core))
        for i, (tag, lv) in enumerate(zip(("1M", "5M", "15M"), la)):
            ly = y + i * self.f_tiny.size * 1.75
            if ly + self.f_tiny.size > h * 0.90:
                break
            d.text((bxc, ly), f"LOAD {tag}", font=self.f_tiny, fill=self.pal.dim)
            lw = f"{lv:.2f}"
            d.text((bxc + bwd - d.textlength(lw, font=self.f_tiny), ly), lw,
                   font=self.f_tiny, fill=self.pal.fg)
            mx0 = bxc + bwd * 0.34
            mx1 = bxc + bwd - pad * 2.6
            d.rectangle([mx0, ly + 2, mx1, ly + self.f_tiny.size - 2],
                        fill=(10, 13, 26))
            d.rectangle([mx0, ly + 2,
                         mx0 + (mx1 - mx0) * min(1.0, lv / ncpu),
                         ly + self.f_tiny.size - 2], fill=scale(gpb, 0.9))

        # --- column C: comms / disk io / reserves ---
        cxc, cwd = self.cC
        y = h * 0.095
        d.text((cxc, y), "COMMS ARRAY", font=self.f_tiny, fill=ssb)
        y += self.f_tiny.size * 1.7
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        for arrow, name, rate, unit, tot, colr in (
                ("▼", "DOWNLINK", dv, du, v.net_rx_total, cyb),
                ("▲", "UPLINK", uv, uu, v.net_tx_total, gpb)):
            d.text((cxc, y), f"{arrow} {name}", font=self.f_tiny, fill=colr)
            y += self.f_tiny.size * 1.25
            d.text((cxc, y), f"{rate} {unit}", font=self.f_small, fill=self.pal.fg)
            ts = f"Σ{tot:.0f}G"
            d.text((cxc + cwd - d.textlength(ts, font=self.f_tiny), y + 1), ts,
                   font=self.f_tiny, fill=self.pal.dim)
            y += self.f_small.size * 1.5
        nh = self.hist("ds:net", v.net_down, n=40)
        # floor keeps an idle link as a low line, not a solid block
        nmax = max(max(nh) * 1.15, 64 * 1024.0)
        rib = max(h * 0.05, min(h * 0.09, h * 0.9))
        stepn = cwd / (len(nh) - 1)
        pts = [(cxc + i * stepn, y + rib - (vv / nmax) * rib * 0.92)
               for i, vv in enumerate(nh)]
        self._ribbon(img, pts, ssb, y + rib, area_alpha=55, width=1)
        d.line([(cxc, y + rib), (cxc + cwd, y + rib)], fill=scale(ssb, 0.4), width=1)
        y += rib + self.f_tiny.size * 0.9
        rd_, ru_ = human_rate(v.disk_rd)
        wr_, wu_ = human_rate(v.disk_wr)
        d.text((cxc, y), "DISK IO", font=self.f_tiny, fill=self.pal.dim)
        y += self.f_tiny.size * 1.25
        io = f"R {rd_}{ru_}  W {wr_}{wu_}"
        d.text((cxc, y), io, font=self.fit_text(d, io, self.f_small, cwd, mono=True),
               fill=self.pal.fg)
        y += self.f_small.size * 1.7
        rows_c = [("RAM", f"{v.mem_used:.1f}/{v.mem_total:.0f}G",
                   v.mem_pct / 100, "ram")]
        if gpu:
            rows_c.append(("VRAM", f"{gpu['mem_used'] / 1024:.1f}/"
                                   f"{gpu['mem_total'] / 1024:.0f}G",
                           gpu["mem_used"] / max(1.0, gpu["mem_total"]), "gpu"))
        rows_c.append(("PROCS", f"{v.procs}", min(1.0, v.procs / 900), "ssd"))
        rows_c.append(("STORAGE", f"{v.disk_pct:.0f}%", v.disk_pct / 100, "cpu"))
        for name, txt, frac, key in rows_c:
            if y + self.f_tiny.size * 2.6 > h * 0.905:
                break
            base = self.col(key)
            d.text((cxc, y), name, font=self.f_tiny, fill=base)
            d.text((cxc + cwd - d.textlength(txt, font=self.f_tiny), y), txt,
                   font=self.f_tiny, fill=self.pal.fg)
            y += self.f_tiny.size * 1.35
            self.seg_bar(d, cxc, y, cwd, max(4, h * 0.014),
                         self.ease(f"res{name}", frac), 12, base, (12, 15, 30))
            y += max(4, h * 0.014) + self.f_tiny.size * 0.75
        # disk-write trace fills whatever height is left in the column
        rem = h * 0.895 - y - self.f_tiny.size * 1.5
        if rem > h * 0.05:
            d.text((cxc, y), "IO TRACE", font=self.f_tiny,
                   fill=scale(self.pal.dim, 0.9))
            y += self.f_tiny.size * 1.4
            rem = h * 0.895 - y
            wh = self.hist("ds:io", v.disk_wr, n=40)
            wmax = max(max(wh) * 1.15, 4 * 2 ** 20)
            for k in range(1, 3):
                gy = y + rem * k / 3
                d.line([(cxc, gy), (cxc + cwd, gy)], fill=scale(gpb, 0.25), width=1)
            stepw = cwd / (len(wh) - 1)
            pts = [(cxc + i * stepw, y + rem - (vv / wmax) * rem * 0.92)
                   for i, vv in enumerate(wh)]
            self._ribbon(img, pts, gpb, y + rem, area_alpha=55, width=1)
            d.line([(cxc, y + rem), (cxc + cwd, y + rem)],
                   fill=scale(gpb, 0.4), width=1)

        # --- footer strip ---
        fy = h - h * 0.052
        fx = pad * 1.2
        for txt, colr in ((f"PROCS {v.procs}", self.pal.dim),
                          (f"LOAD {la[0]:.2f} {la[1]:.2f} {la[2]:.2f}", self.pal.dim),
                          (f"SSD {v.disk_used:.0f}/{v.disk_total:.0f}G", self.pal.dim),
                          (f"MEM {v.mem_used:.1f}/{v.mem_total:.0f}G", self.pal.dim),
                          (f"NET Σ▼{v.net_rx_total:.0f}G ▲{v.net_tx_total:.0f}G",
                           self.pal.dim),
                          (f"T+{up // 86400}D{up % 86400 // 3600:02d}H", self.pal.dim),
                          (f"WARP {sf:.2f}", cyb)):
            if fx + d.textlength(txt, font=self.f_tiny) > w - pad * 6:
                break
            d.text((fx, fy), txt, font=self.f_tiny, fill=colr)
            fx += d.textlength(txt, font=self.f_tiny) + pad * 2.2
        stepm = int(self.t * hz(self.HZ_MARK))
        marker = "".join("█" if (stepm + i) % 6 < 3 else "░" for i in range(6))
        d.text((w - pad * 1.2 - d.textlength(marker, font=self.f_tiny), fy),
               marker, font=self.f_tiny, fill=cyb)
        date = time.strftime("%d/%m/%Y", now)
        d.text((w - pad * 1.2 - d.textlength(date, font=self.f_tiny), h * 0.024),
               date, font=self.f_tiny, fill=self.pal.dim)

        # FXBase._scanline needs an RGBA canvas; paste-with-mask does the same
        band = max(6, int(h * 0.05))
        pos = int(((self.t / self.SWEEP_S) % 1.0) * (h + band)) - band
        strip = Image.new("RGBA", (w, band), (0, 0, 0, 0))
        sd = ImageDraw.Draw(strip)
        for row in range(band):
            a = int(46 * math.sin(math.pi * row / band))
            sd.line([(0, row), (w, row)], fill=tuple(cyb) + (a,))
        img.paste(strip, (0, max(0, pos)), strip)
        return img


VIEWS = {"deep-space": DeepSpaceView}
