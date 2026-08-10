"""synthwave — the 1986 retrowave poster theme (vertical).

A striped chrome sun hovers over a magenta perspective floor; above it a
chrome-gradient italic clock, a cassette seconds track, VU-meter LED stacks
with peak-hold, a per-core equalizer and a tape-deck telemetry footer.
Deliberately info-dense: every neon_grid readout is here, restyled.

Signature colours are hot pink / cyan / sun yellow / purple; --palette
recolours everything (sun, sky, floor and chrome included).
"""

from __future__ import annotations

import math
import os
import time

from PIL import Image, ImageDraw, ImageFilter

from lianli88 import load_font
from fx import (FXBase, Palette, hold, human_rate, hz, lighten, load_mono,
                mix, scale, vitals)

SYNTH = Palette("synthwave",
                ((255, 113, 197), (127, 231, 255), (255, 211, 79), (178, 102, 255)),
                bg=(16, 5, 28), grid=(52, 20, 72), dim=(174, 144, 198))


class SynthwaveView(FXBase):
    SHEAR = 0.16        # italic lean of the chrome clock
    HZ_SHOOT = 0.25     # shooting-star windows per second (~one per 4s)
    HZ_JIT = 0.33       # VHS jitter windows per second (~one per 3s)
    HZ_BOB = 0.10       # sun bob cycles per second
    HZ_BREATH = 0.28    # sun glow breathe cycles per second
    RPS_REEL = 0.40     # cassette reel revolutions per second
    FLOOR_ROWS = 13     # denser than neon_grid's 9 — different floor read
    FLOOR_BASE = 0.50   # floor rows scrolled per second at idle...
    FLOOR_LOAD = 2.40   # ...plus this much at 100% CPU
    PEAK_HOLD = 1.5     # seconds a VU peak lingers before decaying
    PEAK_DECAY = 0.45   # peak fall rate in fraction/second (wall-clock)

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or SYNTH
        self._start(0x1986)
        self.host = os.uname().nodename.upper()[:12]
        self._peaks: dict[str, list] = {}
        self._clk = None                      # (hhmm, chrome, glow) cache

        # fonts probed against the canvas so both 480x960 and 480x1920 work
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        size = int(min(w * 0.34, h * 0.14))
        while size > 14 and (probe.textlength("00:00", font=load_mono(size))
                             + self.SHEAR * size) > w * 0.86:
            size = int(size * 0.93)
        self.f_time = load_mono(size)
        self.f_label = load_mono(max(10, int(size * 0.150)))
        self.f_small = load_mono(max(9, int(size * 0.118)))

        # clock mask geometry is constant for a mono face — measure once
        tb = probe.textbbox((0, 0), "00:00", font=self.f_time)
        th = tb[3] - tb[1] + 2
        self._clk_off = (tb[0], tb[1])
        self._clk_mh = th
        self._clk_mw = int((tb[2] - tb[0]) + self.SHEAR * th) + 2
        self._clk_pad = max(4, int(size * 0.16))

        # geometry: floor at the bottom, sun parked on the horizon above it
        self.pad = w * 0.075
        self.floor_y = int(h * 0.90)
        self.sun_d = int(min(w * 0.62, h * 0.17))
        self.sun_cx = w // 2
        self.sun_cy = int(self.floor_y - self.sun_d * 0.28)

        # static layers: one build cost, zero per-frame cost
        # sky kept RGBA so render() pays a memcpy, not a mode conversion
        self._sky = self._build_sky().convert("RGBA")
        self._sun = self._build_sun()
        self._glow_mask, self._glow_col = self._build_sun_glow()
        self._chrome = self._build_chrome_strip()
        self._horizon = self._build_horizon_glow()

    # --- static layers --------------------------------------------------------
    def _build_sky(self) -> Image.Image:
        """Purple-to-magenta gradient, starfield, faint static scanlines."""
        w, h = self.w, self.h
        img = Image.new("RGB", (w, h), self.pal.bg)
        d = ImageDraw.Draw(img)
        horizon = mix(self.pal.bg, self.pal.base("cpu"), 0.42)
        for y in range(self.floor_y):
            f = (y / self.floor_y) ** 2.1     # dark up top, ramp at horizon
            d.line([(0, y), (w, y)], fill=mix(self.pal.bg, horizon, f))
        d.rectangle([0, self.floor_y, w, h], fill=scale(self.pal.bg, 0.55))
        # ~50 fixed stars; layout frozen by the instance seed, no twinkle
        import random
        srng = random.Random(self.seed ^ 0x57AB5)
        cols = (self.pal.fg, self.pal.dim, mix(self.pal.base("gpu"), self.pal.bg, 0.3))
        for _ in range(50):
            sx = srng.uniform(4, w - 4)
            sy = srng.uniform(4, self.floor_y * 0.9)
            r = srng.choice((0, 0, 1))
            d.ellipse([sx - r, sy - r, sx + r + 1, sy + r + 1],
                      fill=srng.choice(cols))
        # static VHS scanlines, subtle enough to survive on top of text
        ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        for y in range(0, h, 3):
            od.line([(0, y), (w, y)], fill=(0, 0, 0, 16))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
        return img

    def _build_sun(self) -> Image.Image:
        """The striped disc: yellow-to-magenta gradient through a gap mask."""
        d_ = self.sun_d
        mask = Image.new("L", (d_, d_), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse([0, 0, d_ - 1, d_ - 1], fill=255)
        # gap stripes widen toward the bottom — the classic set-sun cut
        yy, k = d_ * 0.42, 0
        while yy < d_:
            gap = d_ * (0.014 + 0.013 * k)
            md.rectangle([0, yy, d_, yy + gap], fill=0)
            yy += gap + max(d_ * 0.02, d_ * (0.085 - 0.008 * k))
            k += 1
        grad = Image.new("RGB", (d_, d_))
        gd = ImageDraw.Draw(grad)
        top, bot = self.pal.base("ram"), self.pal.base("cpu")
        for y in range(d_):
            gd.line([(0, y), (d_, y)], fill=mix(top, bot, y / d_))
        sun = Image.new("RGBA", (d_, d_), (0, 0, 0, 0))
        sun.paste(grad, (0, 0), mask)
        return sun

    def _build_sun_glow(self):
        """Blurred halo mask + solid colour plate; alpha is scaled per frame."""
        g = int(self.sun_d * 1.55)
        m = Image.new("L", (g, g), 0)
        r = self.sun_d * 0.52
        ImageDraw.Draw(m).ellipse([g / 2 - r, g / 2 - r, g / 2 + r, g / 2 + r],
                                  fill=150)
        m = m.filter(ImageFilter.GaussianBlur(self.sun_d * 0.11))
        col = Image.new("RGB", (g, g), mix(self.pal.base("cpu"),
                                           self.pal.base("ram"), 0.35))
        return m, col

    def _build_chrome_strip(self) -> Image.Image:
        """Two-tone chrome gradient the clock mask is filled with."""
        mw, mh = self._clk_mw, self._clk_mh
        strip = Image.new("RGB", (mw, mh))
        d = ImageDraw.Draw(strip)
        sky = mix(self.pal.base("gpu"), self.pal.fg, 0.18)
        for y in range(mh):
            t = y / mh
            if t < 0.46:
                c = mix(sky, self.pal.fg, (t / 0.46) ** 1.4)
            elif t < 0.52:
                c = self.pal.fg               # the horizon line inside the type
            else:
                u = (t - 0.52) / 0.48
                c = mix(scale(self.pal.base("cpu"), 0.55),
                        lighten(self.pal.base("cpu"), 0.35), u)
            d.line([(0, y), (mw, y)], fill=c)
        return strip

    def _build_horizon_glow(self) -> Image.Image:
        gh = 16
        strip = Image.new("RGBA", (self.w, gh), (0, 0, 0, 0))
        d = ImageDraw.Draw(strip)
        base = self.pal.base("cpu")
        for row in range(gh):
            a = int(110 * math.sin(math.pi * row / gh))
            d.line([(0, row), (self.w, row)], fill=tuple(base) + (a,))
        return strip

    # --- cached clock ---------------------------------------------------------
    def _clock_layers(self, hhmm: str):
        """Chrome fill + glow for HH:MM, rebuilt only when the minute changes.

        The blur runs on a strip the size of the type, never the canvas.
        """
        if self._clk and self._clk[0] == hhmm:
            return self._clk[1], self._clk[2]
        mw, mh, p = self._clk_mw, self._clk_mh, self._clk_pad
        mask = Image.new("L", (mw, mh), 0)
        ImageDraw.Draw(mask).text((-self._clk_off[0], -self._clk_off[1]),
                                  hhmm, font=self.f_time, fill=255)
        # shear: top rows shift right — the retro italic lean
        mask = mask.transform((mw, mh), Image.AFFINE,
                              (1, self.SHEAR, -self.SHEAR * mh, 0, 1, 0),
                              resample=Image.BILINEAR)
        chrome = Image.new("RGBA", (mw, mh), (0, 0, 0, 0))
        chrome.paste(self._chrome, (0, 0), mask)
        glow = Image.new("RGBA", (mw + 2 * p, mh + 2 * p), (0, 0, 0, 0))
        glow.paste(tuple(self.pal.base("cpu")) + (200,), (p, p, p + mw, p + mh),
                   mask)
        glow = glow.filter(ImageFilter.GaussianBlur(self.f_time.size * 0.05))
        self._clk = (hhmm, chrome, glow)
        return chrome, glow

    # --- widgets --------------------------------------------------------------
    def _peak_track(self, key: str, cur: float) -> float:
        """Max of the recent window, held then decayed on wall-clock time."""
        p = self._peaks.get(key)
        if p is None or cur >= p[0]:
            self._peaks[key] = [cur, self.t]
            return cur
        if self.t - p[1] > self.PEAK_HOLD:
            p[0] = max(cur, p[0] - self.PEAK_DECAY * self.dt)
        return p[0]

    def _ribbon_strip(self, img, rx, ry, rw, rh, histo, base):
        """History ribbon drawn in a local strip — fx.ribbon composites a
        full-canvas layer per call, too costly for four rows per frame."""
        strip = Image.new("RGBA", (int(rw) + 2, int(rh) + 3), (0, 0, 0, 0))
        sd = ImageDraw.Draw(strip)
        step = rw / (len(histo) - 1)
        pts = [(i * step, rh - vv * rh * 0.94) for i, vv in enumerate(histo)]
        sd.polygon([(0, rh)] + pts + [(pts[-1][0], rh)],
                   fill=tuple(base) + (60,))
        sd.line(pts, fill=tuple(base), width=2)
        sd.line([(0, rh), (rw, rh)], fill=scale(base, 0.45), width=1)
        img.alpha_composite(strip, (int(rx), int(ry)))

    def _vu(self, d, x, y, bw, bh, frac, peak, base):
        n = 30
        off = mix(self.pal.bg, base, 0.18)
        self.seg_bar(d, x, y, bw, bh, frac, n, base, off, gap_frac=0.32)
        seg = bw / n
        pi = min(n - 1, int(peak * n))
        if pi > frac * n:                    # peak chip ahead of the fill
            d.rectangle([x + pi * seg, y, x + pi * seg + seg * 0.68, y + bh],
                        fill=lighten(base, 0.55))

    def _equalizer(self, d, x, y, bw, eq_h, cores):
        """One thin 20-segment column per core, bottom-lit, eased heights."""
        n = max(1, len(cores))
        segs = 20
        gap = max(1.0, bw * 0.006)
        cw = (bw - gap * (n - 1)) / n
        sh = eq_h / segs
        lo, hi = self.pal.base("gpu"), self.pal.base("cpu")
        for i in range(n):
            f = self.ease(f"eq{i}", max(0.0, min(1.0, (cores[i] if i < len(cores) else 0) / 100)))
            lit = round(f * segs)
            cx = x + i * (cw + gap)
            for s in range(segs):
                sy = y + eq_h - (s + 1) * sh
                col = mix(lo, hi, s / (segs - 1))
                if s >= lit:
                    col = mix(self.pal.bg, col, 0.14)
                d.rectangle([cx, sy, cx + cw, sy + sh * 0.68], fill=col)

    def _reel(self, d, cx, cy, r, base):
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  outline=base, width=2)
        a0 = self.t * math.tau * hz(self.RPS_REEL)
        for k in range(3):
            a = a0 + k * math.tau / 3
            d.line([(cx, cy), (cx + math.cos(a) * r * 0.82,
                               cy + math.sin(a) * r * 0.82)],
                   fill=scale(base, 0.8), width=2)
        d.ellipse([cx - r * 0.28, cy - r * 0.28, cx + r * 0.28, cy + r * 0.28],
                  fill=base)

    def _shooting_star(self, d):
        """One streak per ~4s window; position derives from window progress."""
        r = self.wrng(self.HZ_SHOOT, salt=0x57A2)
        if r.random() > 0.75:
            return
        prog = (self.t * hz(self.HZ_SHOOT)) % 1.0
        if prog > 0.30:                      # visible only for the first slice
            return
        p = prog / 0.30
        x0 = r.uniform(0.15, 0.85) * self.w
        y0 = r.uniform(0.04, 0.24) * self.h
        dx = r.choice((-1, 1)) * r.uniform(0.22, 0.38) * self.w
        dy = r.uniform(0.06, 0.13) * self.h
        hx, hy = x0 + dx * p, y0 + dy * p
        fade = 1.0 - p
        tail = 0.35 + 0.4 * p
        d.line([(hx - dx * 0.22 * tail, hy - dy * 0.22 * tail), (hx, hy)],
               fill=scale(self.pal.base("gpu"), 0.35 + 0.5 * fade), width=1)
        d.line([(hx - dx * 0.10 * tail, hy - dy * 0.10 * tail), (hx, hy)],
               fill=scale(self.pal.fg, 0.5 + 0.5 * fade), width=1)
        d.ellipse([hx - 1.5, hy - 1.5, hx + 1.5, hy + 1.5],
                  fill=lighten(self.pal.base("gpu"), 0.6 * fade))

    def _floor(self, img, d, load_frac):
        """Magenta perspective floor; scroll speed rides CPU load."""
        w, h, y0 = self.w, self.h, self.floor_y
        fh = h - y0
        base = self.pal.base("cpu")
        d.rectangle([0, y0, w, h], fill=scale(self.pal.bg, 0.55))
        vpx = w / 2
        for k in range(-11, 12):            # denser lanes than neon_grid
            xb = vpx + k * (w * 0.105)
            d.line([(vpx, y0), (xb, h)],
                   fill=scale(base, 0.62 if k % 2 == 0 else 0.38),
                   width=2 if k % 2 == 0 else 1)
        speed = self.FLOOR_BASE + self.FLOOR_LOAD * load_frac
        phase = (self.t * hz(speed)) % 1.0
        for i in range(self.FLOOR_ROWS):
            z = (i + phase) / self.FLOOR_ROWS
            yy = y0 + fh * (z ** 2.3)
            d.line([(0, yy), (w, yy)],
                   fill=mix(self.pal.bg, base, 0.20 + 0.75 * z),
                   width=1 + int(z * 2.2))
        d.line([(0, y0), (w, y0)], fill=lighten(base, 0.5), width=2)
        img.alpha_composite(self._horizon, (0, y0 - 8))

    def _vhs_jitter(self, img):
        """Shift one band 1-2px sideways for a moment each ~3s window."""
        r = self.wrng(self.HZ_JIT, salt=0x1177)
        if (self.t * hz(self.HZ_JIT)) % 1.0 > 0.14:
            return
        by = int(r.uniform(0.05, 0.85) * self.h)
        bh = r.randint(6, 20)
        shift = r.choice((-2, -1, 1, 2))
        band = img.crop((0, by, self.w, by + bh))
        img.paste(band, (shift, by))

    # --- frame ----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        w, h = self.w, self.h
        img = self._sky.copy()
        d = ImageDraw.Draw(img)
        pk, cn = self.pal.base("cpu"), self.pal.base("gpu")
        yl, pu = self.pal.base("ram"), self.pal.base("ssd")
        pad, avail = self.pad, w - self.pad * 2
        fl, fs = self.f_label, self.f_small

        now = time.localtime()
        v = vitals()
        gpu = v.gpu
        self._shooting_star(d)

        # --- sun: bobs on a slow sine, glow breathes; floor covers its base ---
        bob = math.sin(self.t * math.tau * hz(self.HZ_BOB)) * self.sun_d * 0.015
        breath = 0.60 + 0.28 * math.sin(self.t * math.tau * hz(self.HZ_BREATH))
        g = self._glow_mask.size[0]
        gm = self._glow_mask.point(lambda p: int(p * breath))
        gx = self.sun_cx - g // 2
        gy = int(self.sun_cy + bob) - g // 2
        img.paste(self._glow_col, (gx, gy), gm)
        sx = self.sun_cx - self.sun_d // 2
        sy = int(self.sun_cy + bob) - self.sun_d // 2
        img.paste(self._sun, (sx, sy), self._sun)
        d = ImageDraw.Draw(img)

        # --- header strip ---
        y = h * 0.012
        d.text((pad, y), "◉ SYNTHWAVE FM", font=fs, fill=pk)
        d.text((w - pad - d.textlength(self.host, font=fs), y),
               self.host, font=fs, fill=self.pal.dim)
        y += fs.size * 1.4
        date = time.strftime("%A %d %b %Y", now).upper()
        f_date = self.fit_text(d, date, fs, avail * 0.7, mono=True)
        d.text((pad, y), date, font=f_date, fill=cn)
        ghz = f"{v.freq_ghz:.1f}GHZ"
        d.text((w - pad - d.textlength(ghz, font=fs), y), ghz,
               font=fs, fill=self.pal.dim)
        y += fs.size * 1.5
        d.line([(pad, y), (w - pad, y)], fill=scale(pu, 0.8), width=2)
        y += h * 0.010

        # --- chrome clock (mask cached per minute) ---
        hhmm = time.strftime("%H:%M", now)
        chrome, glow = self._clock_layers(hhmm)
        cx = int((w - self._clk_mw) / 2)
        img.alpha_composite(glow, (cx - self._clk_pad, int(y) - self._clk_pad))
        img.paste(chrome, (cx, int(y)), chrome)
        d = ImageDraw.Draw(img)
        y += self._clk_mh + h * 0.012

        # --- cassette seconds track: fractional, so the fill glides ---
        sec = time.time() % 60.0
        blink = "▶ PLAY" if self.cycle(1.0) % 2 == 0 else "▷ PLAY"
        d.text((pad, y), blink, font=fs, fill=yl)
        stxt = f"{int(sec):02d}S"
        d.text((w - pad - d.textlength(stxt, font=fs), y), stxt,
               font=fs, fill=cn)
        y += fs.size * 1.35
        sbh = max(6, h * 0.010)
        self.seg_bar(d, pad, y, avail, sbh, sec / 60.0, 32,
                     cn, mix(self.pal.bg, cn, 0.14), gap_frac=0.30)
        y += sbh + h * 0.012

        # --- per-core equalizer ---
        d.text((pad, y), f"CORES {len(v.per_core)}", font=fl, fill=pu)
        la = hold("loadavg", os.getloadavg)
        lav = f"LOAD {la[0]:.2f} {la[1]:.2f}"
        d.text((w - pad - d.textlength(lav, font=fs), y + fl.size - fs.size),
               lav, font=fs, fill=self.pal.dim)
        y += fl.size * 1.35
        eq_h = h * 0.058
        self._equalizer(d, pad, y, avail, eq_h, v.per_core)
        y += eq_h + h * 0.014

        # --- footer + sun geometry decide the VU stack's slot ---
        sun_top = self.floor_y - int(self.sun_d * 0.78)
        fp_h = int(fs.size * 6.1)
        foot_y = sun_top - h * 0.006 - fp_h

        rd, ru = human_rate(v.disk_rd)
        wr_, wu = human_rate(v.disk_wr)
        rows = [("CPU", v.load / 100, f"{v.load:.0f}%",
                 f"{v.temp:.0f}°C" if v.temp else "", "cpu",
                 f"LOAD {la[0]:.2f} · {v.freq_ghz:.1f}GHZ")]
        if gpu:
            rows.append(("GPU", gpu["util"] / 100, f"{gpu['util']:.0f}%",
                         f"{gpu['temp']:.0f}°C {gpu['power']:.0f}W", "gpu",
                         f"VRAM {gpu['mem_used']/1024:.1f}/{gpu['mem_total']/1024:.0f}G"))
        else:
            rows.append(("GPU", 0.0, "--", "", "gpu", "NO GPU"))
        rows.append(("MEM", v.mem_pct / 100, f"{v.mem_used:.1f}G",
                     f"of {v.mem_total:.0f}G", "ram",
                     f"{v.mem_pct:.0f}% COMMITTED"))
        rows.append(("SSD", v.disk_pct / 100, f"{v.disk_pct:.0f}%",
                     f"{v.nvme_temp:.0f}°C" if v.nvme_temp else
                     f"{v.disk_used:.0f}G", "ssd",
                     f"R {rd}{ru} · W {wr_}{wu}"))

        slot = (foot_y - h * 0.008 - y) / len(rows)
        bh = max(7, min(h * 0.013, slot * 0.10))
        for label, frac, val, sub, key, detail in rows:
            base = self.col(key)
            # value/bar sizes grow with the slot so a tall canvas fills it
            f_val = load_font(max(16, int(min(slot * 0.42,
                                              self.f_time.size * 0.72))))
            f_val = self.fit_text(d, val, f_val, avail * 0.42, floor=16)
            rib_h = f_val.size * 0.98
            content = fl.size * 1.25 + rib_h + bh * 2.2 + fs.size * 2.4
            ry0 = y + max(0.0, (slot - content) * 0.30)
            d.text((pad, ry0), label, font=fl, fill=base)
            if sub:
                d.text((w - pad - d.textlength(sub, font=fs),
                        ry0 + fl.size - fs.size), sub, font=fs,
                       fill=self.pal.dim)
            vy = ry0 + fl.size * 1.25
            d.text((pad, vy), val, font=f_val, fill=self.pal.fg)
            # history ribbon fills the right column when the slot allows it
            histo = self.hist(f"sw:{label}", frac, n=40)
            if slot > fl.size * 1.25 + rib_h + bh * 2.6:
                self._ribbon_strip(img, pad + avail * 0.46, vy,
                                   avail * 0.54, rib_h, histo, base)
                d = ImageDraw.Draw(img)
            by = vy + rib_h + bh * 0.8
            eased = self.ease(label, frac)
            self._vu(d, pad, by, avail, bh, eased,
                     self._peak_track(label, eased), base)
            ty2 = by + bh
            if slot - (ty2 - y) > fs.size * 1.6:
                # tick row under the meter, lit up to the fill
                for i in range(11):
                    tx = pad + avail * i / 10
                    on = i / 10 <= eased
                    d.line([(tx, ty2 + 4), (tx, ty2 + (11 if i % 5 == 0 else 7))],
                           fill=base if on else scale(self.pal.dim, 0.4),
                           width=2)
                ty2 += 12
            if slot - (ty2 - y) > fs.size * 1.7:
                d.text((pad, ty2 + fs.size * 0.45), detail,
                       font=fs, fill=self.pal.dim)
            y += slot

        # --- tape-deck footer panel ---
        px0, px1 = pad * 0.55, w - pad * 0.55
        d.rounded_rectangle([px0, foot_y, px1, foot_y + fp_h], radius=8,
                            fill=mix(self.pal.bg, pu, 0.14),
                            outline=scale(pu, 0.85), width=2)
        ip = fs.size * 0.7
        ry = foot_y + ip + fs.size * 0.9
        rr = fs.size * 0.95
        self._reel(d, w * 0.40, ry, rr, pu)
        self._reel(d, w * 0.60, ry, rr, pu)
        d.rectangle([w * 0.40 + rr * 1.3, ry - 2, w * 0.60 - rr * 1.3, ry + 2],
                    fill=scale(pu, 0.6))
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        ty = ry - fs.size * 0.55
        d.text((px0 + ip, ty), f"▼ {dv}{du}", font=fs, fill=cn)
        utxt = f"▲ {uv}{uu}"
        d.text((px1 - ip - d.textlength(utxt, font=fs), ty), utxt,
               font=fs, fill=pk)
        row2 = ry + rr + fs.size * 0.6
        d.text((px0 + ip, row2), f"R {rd}{ru} · W {wr_}{wu}", font=fs, fill=yl)
        vram = (f"VRAM {gpu['mem_used']/1024:.1f}/{gpu['mem_total']/1024:.0f}G"
                if gpu else "VRAM --")
        d.text((px1 - ip - d.textlength(vram, font=fs), row2), vram,
               font=fs, fill=self.pal.dim)
        row3 = row2 + fs.size * 1.4
        up = v.uptime
        d.text((px0 + ip, row3),
               f"UP {up // 86400}D {up % 86400 // 3600:02d}H {up % 3600 // 60:02d}M",
               font=fs, fill=self.pal.dim)
        pr = f"{v.procs} PROCS"
        d.text((px1 - ip - d.textlength(pr, font=fs), row3), pr,
               font=fs, fill=self.pal.dim)

        # --- floor last: it owns the bottom band and crops the sun's base ---
        self._floor(img, d, self.ease("floor", v.load / 100))
        self._vhs_jitter(img)
        return img.convert("RGB")


VIEWS = {"synthwave": SynthwaveView}
