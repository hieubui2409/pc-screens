"""carbon-gauge — racing cockpit cluster (horizontal).

Round spring-needle dials for CPU / GPU / MEM / SSD over a procedural
carbon-fibre weave, a shift-light strip riding the top edge on CPU load,
a dashboard LCD clock with an odometer-style uptime, and telemetry in
small inset windows below the dials. Signature colours are racing
red/amber on near-black carbon; --palette recolours everything.

Horizontal layout: LCD cluster left, dials centre, tach/cores + gear
right.  Everything static (weave, dial faces with ticks/numbers/zones,
window frames) is built once in __init__; per frame only needles,
readouts, LEDs, digits and speed streaks are drawn — no per-frame blur.
"""

from __future__ import annotations

import math
import os
import random
import time

from PIL import Image, ImageDraw

from lianli88 import load_font
from fx import (FXBase, Palette, dens, human_rate, hz, lighten, load_mono,
                mix, scale, vitals)

CARBON = Palette("carbon",
                 ((255, 65, 54), (255, 177, 74), (240, 244, 248), (255, 120, 80)),
                 bg=(13, 15, 19), grid=(30, 33, 40), dim=(148, 154, 165))

# dial sweep: value 0 at lower-left, 100 at lower-right, over the top
A0, SWEEP = 150.0, 240.0          # PIL degrees (clockwise, 0 = 3 o'clock)
REDLINE = 0.80                     # red zone + flash threshold


class CarbonGaugeView(FXBase):
    HZ_BLINK = 4.0     # redline / shift-light flash cadence
    SPRING_W = 9.0     # needle natural frequency (rad/s)
    SPRING_Z = 0.55    # under-damped on purpose: slight overshoot on spikes
    N_LEDS = 12

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or CARBON
        self._start(0xCA9B07)
        self.host = os.uname().nodename.upper()[:12]

        v = vitals()
        self.n_cores = max(1, len(v.per_core))
        self._springs: dict[str, list[float]] = {}
        self._ph = 0.0                     # integrated streak travel (px)

        # --- column layout: LCD cluster | dials | tach/gear ---
        self.strip_h = max(40, int(h * 0.11))
        self.Lw = max(140, int(w * 0.17))
        rail = 8
        self.gx0, self.gx1 = self.Lw + rail + 6, w - self.Lw - rail - 6

        gauges = [("cpu", "CPU"), ("gpu", "GPU"), ("ram", "MEM"), ("ssd", "SSD")]
        if not v.gpu:
            gauges = [g for g in gauges if g[0] != "gpu"]
        # Drop SSD only when the measured slot is too narrow for a readable
        # dial (ticks + numbers need ~190 px of face) — not on width alone.
        if (self.gx1 - self.gx0) / len(gauges) < 190 and len(gauges) > 3:
            gauges = [g for g in gauges if g[0] != "ssd"]
        self.gauges = gauges

        slot = (self.gx1 - self.gx0) / len(gauges)
        band_h = h - self.strip_h
        self.gr = min(slot * 0.94, band_h * 0.72) / 2
        self.gcy = self.strip_h + h * 0.02 + self.gr
        self.gcx = [self.gx0 + slot * (i + 0.5) for i in range(len(gauges))]
        self.panel_y = int(self.gcy + self.gr + h * 0.025)

        # --- fonts, probed against the actual columns ---
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        pad = int(self.Lw * 0.09)
        size = int(self.Lw * 0.30)
        while size > 12 and probe.textlength("00:00:00",
                                             font=load_mono(size)) > (self.Lw - pad * 2) * 0.94:
            size = int(size * 0.93)
        self.f_lcd = load_mono(size)
        self.f_small = load_mono(max(9, int(h * 0.026)))
        self.f_tiny = load_mono(max(8, int(h * 0.022)))
        self.f_tick = load_mono(max(8, int(self.gr * 0.105)))
        self.f_glabel = load_mono(max(9, int(self.gr * 0.14)))
        self.f_gval = load_mono(max(12, int(self.gr * 0.20)))
        self.f_gsub = load_mono(max(8, int(self.gr * 0.115)))
        self.f_odo = load_mono(max(10, int(self.Lw * 0.075)))
        self.f_badge = load_font(max(12, int(self.Lw * 0.10)))
        self.f_gear = load_font(max(20, int(h * 0.11)))
        self.f_pval = load_mono(max(11, int(h * 0.040)))

        self._bg = self._build_bg()
        self._faces = self._build_faces()   # (sprite, (x, y)) per dial

    # ------------------------------------------------------------------ colours
    def _metal(self, f: float):
        return mix(self.pal.bg, self.pal.dim, f)

    def _zones(self):
        """ok / warn / red band colours, all palette-derived."""
        return (scale(self.pal.base("ram"), 0.42),
                scale(self.pal.base("gpu"), 0.75),
                self.pal.base("cpu"))

    def _led_col(self, t: float):
        """Shift-light gradient across the strip (ok -> warn -> red)."""
        if t < 0.5:
            return mix(scale(self.pal.base("ram"), 0.85), self.pal.base("gpu"), t * 2)
        return mix(self.pal.base("gpu"), self.pal.base("cpu"), (t - 0.5) * 2)

    # ------------------------------------------------------------- static layer
    def _weave(self, img) -> None:
        """45° twill carbon weave: two strand directions in a 2x2 block tile,
        built once and paste-tiled — per-pixel work never happens per frame."""
        s = 14
        bg = self.pal.bg
        cells = []
        for fwd in (True, False):
            cell = Image.new("RGB", (s, s), bg)
            cd = ImageDraw.Draw(cell)
            for k in range(-s, s * 2, 3):
                shade = mix(bg, self.pal.fg, 0.030 if (k // 3) % 2 else 0.055)
                if fwd:
                    cd.line([(k, s), (k + s, 0)], fill=shade, width=2)
                else:
                    cd.line([(k, 0), (k + s, s)], fill=shade, width=2)
            # edge shadow gives the over/under depth of the twill
            cd.line([(0, 0), (s, 0)], fill=scale(bg, 0.6), width=1)
            cd.line([(0, 0), (0, s)], fill=scale(bg, 0.6), width=1)
            cells.append(cell)
        tile = Image.new("RGB", (s * 2, s * 2), bg)
        for i in (0, 1):
            for j in (0, 1):
                tile.paste(cells[(i + j) % 2], (i * s, j * s))
        for x in range(0, self.w, s * 2):
            for y in range(0, self.h, s * 2):
                img.paste(tile, (x, y))

    def _brushed_v(self, d, x0, x1, y0, y1, rng) -> None:
        """Vertical brushed-metal rail: per-column brightness jitter."""
        for x in range(int(x0), int(x1)):
            d.line([(x, y0), (x, y1)], fill=self._metal(0.22 + 0.20 * rng.random()))
        d.line([(x0, y0), (x0, y1)], fill=self._metal(0.55))
        d.line([(x1, y0), (x1, y1)], fill=self._metal(0.08))

    def _inset(self, d, box, label=None, font=None) -> None:
        """Dark inset window with a metal frame — the house telemetry look."""
        d.rectangle(box, fill=scale(self.pal.bg, 0.5), outline=self._metal(0.4))
        d.line([(box[0] + 1, box[1] + 1), (box[2] - 1, box[1] + 1)],
               fill=scale(self.pal.bg, 0.25))          # top inner shadow
        if label:
            d.text((box[0] + 6, box[1] + 4), label, font=font or self.f_tiny,
                   fill=self.pal.dim)

    def _build_bg(self) -> Image.Image:
        w, h = self.w, self.h
        img = Image.new("RGB", (w, h), self.pal.bg)
        self._weave(img)
        d = ImageDraw.Draw(img)
        rng = random.Random(0xB2A55)

        # separator rails + trim under the shift strip
        self._brushed_v(d, self.Lw, self.Lw + 8, 0, h, rng)
        self._brushed_v(d, w - self.Lw - 8, w - self.Lw, 0, h, rng)
        for y in (self.strip_h - 3, self.strip_h - 1):
            d.line([(0, y), (w, y)], fill=self._metal(0.45 if y % 2 else 0.2))

        # --- shift-light housing + unlit wells (lit fills are per-frame) ---
        n = self.N_LEDS
        led = self.strip_h * 0.50
        pitch = led * 1.6
        x0 = (w - n * pitch) / 2
        cy = self.strip_h * 0.48
        d.rounded_rectangle([x0 - led, cy - led * 0.85, x0 + n * pitch + led * 0.4,
                             cy + led * 0.85], radius=led * 0.8,
                            fill=scale(self.pal.bg, 0.55), outline=self._metal(0.4))
        self._leds = []
        for i in range(n):
            cx = x0 + pitch * (i + 0.5)
            r = led / 2
            d.ellipse([cx - r, cy - r, cx + r, cy + r],
                      fill=scale(self._led_col(i / (n - 1)), 0.16),
                      outline=self._metal(0.3))
            self._leds.append((cx, cy, r))

        # --- left column: badge, LCD bezel, odometer cells, tell-tales ---
        pad = int(self.Lw * 0.09)
        lx0, lx1 = pad, self.Lw - pad
        y = self.strip_h + h * 0.035
        f_b = self.fit_text(d, self.host, self.f_badge, lx1 - lx0 - 16)
        bh = f_b.size * 1.8
        d.rounded_rectangle([lx0, y, lx1, y + bh], radius=4,
                            outline=self._metal(0.5), width=2)
        d.text(((lx0 + lx1) / 2, y + bh / 2), self.host, font=f_b,
               fill=self.pal.fg, anchor="mm")
        d.line([(lx0 + 8, y + bh - 4), (lx1 - 8, y + bh - 4)],
               fill=self.pal.base("cpu"), width=2)
        y += bh + h * 0.03

        lcd_h = self.f_lcd.size * 1.55
        self._inset(d, [lx0, y, lx1, y + lcd_h])
        tl = d.textlength("00:00:00", font=self.f_lcd)
        self._lcd_xy = ((lx0 + lx1 - tl) / 2, y + lcd_h / 2)
        # LCD ghost segments — cheap stand-in for a glow pass
        d.text(self._lcd_xy, "88:88:88", font=self.f_lcd,
               fill=mix(scale(self.pal.bg, 0.5), self.pal.fg, 0.07), anchor="lm")
        y += lcd_h + h * 0.012
        self._date_xy = ((lx0 + lx1) / 2, y + self.f_tiny.size * 0.7)
        y += self.f_tiny.size * 1.9

        # odometer: ODO label + 4 hour cells, colon, 2 minute cells
        ch = self.f_odo.size * 1.5
        cw = self.f_odo.size * 0.85
        ox = lx0 + d.textlength("ODO", font=self.f_tiny) + 8
        d.text((lx0, y + ch / 2), "ODO", font=self.f_tiny,
               fill=self.pal.dim, anchor="lm")
        self._odo_cells = []
        for i in range(6):
            fill = scale(self.pal.bg, 0.45) if i < 4 else scale(self.pal.base("cpu"), 0.30)
            if i == 4:
                self._odo_colon = (ox + 2, y + ch / 2)
                ox += 7
            d.rectangle([ox, y, ox + cw, y + ch], fill=fill, outline=self._metal(0.35))
            self._odo_cells.append((ox + cw / 2, y + ch / 2))
            ox += cw + 2
        y += ch + h * 0.035

        # tell-tale warning lights (fills are per-frame)
        self._tells = []
        tw = (lx1 - lx0 - 12) / 3
        for i, lbl in enumerate(("NET", "DSK", "TMP")):
            bx = lx0 + i * (tw + 6)
            box = (bx, y, bx + tw, y + self.f_tiny.size * 2.1)
            d.rounded_rectangle(box, radius=3, fill=scale(self.pal.bg, 0.5),
                                outline=self._metal(0.35))
            self._tells.append((box, lbl))
        y += self.f_tiny.size * 2.1 + h * 0.045

        # CPU load trace — fills whatever the LCD stack left in this column
        self._trace = None
        if h - 12 - y > 46:
            d.text((lx0, y), "TRACE · CPU", font=self.f_tiny, fill=self.pal.dim)
            y += self.f_tiny.size * 1.5
            self._inset(d, [lx0, y, lx1, h - 12])
            for f in (0.25, 0.50, 0.75):        # static reference lines
                gy = y + (h - 12 - y) * f
                d.line([(lx0 + 4, gy), (lx1 - 4, gy)], fill=scale(self.pal.bg, 0.75))
            self._trace = (lx0 + 5, y + 5, lx1 - 5, h - 17)

        # --- right column: rev/cores block, gear window, temps line ---
        rx0, rx1 = w - self.Lw + pad, w - pad
        ry = self.strip_h + h * 0.035
        d.text((rx0, ry), "TACH · CORES", font=self.f_tiny, fill=self.pal.dim)
        cores = f"x{self.n_cores}"
        d.text((rx1 - d.textlength(cores, font=self.f_tiny), ry), cores,
               font=self.f_tiny, fill=self.pal.base("gpu"))
        ry += self.f_tiny.size * 1.6
        rev_h = h * 0.26
        self._inset(d, [rx0, ry, rx1, ry + rev_h])
        self._rev = (rx0 + 5, ry + 5, rx1 - 5, ry + rev_h - 5)
        n = self.n_cores
        bw = (self._rev[2] - self._rev[0]) / n
        for i in range(n):                       # static wells under the bars
            bx = self._rev[0] + i * bw
            d.rectangle([bx + 1, self._rev[1], bx + max(2, bw - 2), self._rev[3]],
                        fill=scale(self.pal.bg, 0.38))
        ry += rev_h + h * 0.045

        gw = min((rx1 - rx0) * 0.6, h * 0.24)
        gx = (rx0 + rx1 - gw) / 2
        gh = h * 0.20
        self._inset(d, [gx, ry, gx + gw, ry + gh])
        d.text(((rx0 + rx1) / 2, ry - 2), "GEAR", font=self.f_tiny,
               fill=self.pal.dim, anchor="mb")
        self._gear_xy = ((rx0 + rx1) / 2, ry + gh / 2)
        ry += gh + h * 0.045
        self._temps_xy = ((rx0 + rx1) / 2, ry)

        # --- telemetry mini-panels below the dials ---
        labels = ("NET", "DISK", "VRAM", "LOAD", "PROCS", "FREQ")
        keys = ("cpu", "gpu", "ram", "ssd", "cpu", "gpu")
        band_h = h - 10 - self.panel_y
        rows = 1 if band_h < 130 else 2
        cols = -(-len(labels) // rows)
        gap = 8
        pw = (self.gx1 - self.gx0 - gap * (cols - 1)) / cols
        ph = (band_h - gap * (rows - 1)) / rows
        self._panels = []
        for i, (lbl, key) in enumerate(zip(labels, keys)):
            px = self.gx0 + (i % cols) * (pw + gap)
            py = self.panel_y + (i // cols) * (ph + gap)
            self._inset(d, [px, py, px + pw, py + ph], label=lbl)
            d.rectangle([px, py, px + 2, py + ph], fill=self.pal.base(key))
            self._panels.append((px + 8, py + ph * 0.62, pw - 16))

        return img

    def _build_faces(self):
        """One sprite per dial: bezel, face, zone bands, ticks, numbers,
        label, readout frame — everything that never changes. Sprites carry
        their patch of weave so pasting them erases the streaks beneath."""
        faces = []
        r = self.gr
        S = int(r * 2) + 2
        z_ok, z_warn, z_red = self._zones()
        for (key, label), cx in zip(self.gauges, self.gcx):
            px, py = int(cx - S / 2), int(self.gcy - S / 2)
            spr = self._bg.crop((px, py, px + S, py + S))
            d = ImageDraw.Draw(spr)
            c = S / 2
            rng = random.Random(0xBE2E1)
            d.ellipse([c - r, c - r, c + r, c + r], fill=scale(self.pal.bg, 0.55))
            for rr in range(int(r * 0.91), int(r * 0.99)):   # brushed bezel ring
                d.ellipse([c - rr, c - rr, c + rr, c + rr],
                          outline=self._metal(0.28 + 0.22 * rng.random()))
            fr = r * 0.88
            d.ellipse([c - fr, c - fr, c + fr, c + fr],
                      fill=mix(self.pal.bg, self.pal.fg, 0.045))
            d.ellipse([c - fr, c - fr, c + fr, c + fr],
                      outline=scale(self.pal.bg, 0.6), width=2)

            def box(rad):
                return [c - rad, c - rad, c + rad, c + rad]

            bw = max(3, int(r * 0.055))
            d.arc(box(r * 0.79), A0, A0 + SWEEP, fill=self._metal(0.3), width=bw)
            d.arc(box(r * 0.79), A0, A0 + SWEEP * 0.60, fill=z_ok, width=bw)
            d.arc(box(r * 0.79), A0 + SWEEP * 0.60, A0 + SWEEP * REDLINE,
                  fill=z_warn, width=bw)
            d.arc(box(r * 0.79), A0 + SWEEP * REDLINE, A0 + SWEEP, fill=z_red, width=bw)

            for i in range(11):                              # ticks every 10
                a = math.radians(A0 + SWEEP * i / 10)
                major = i % 2 == 0
                r0, r1 = r * 0.75, r * (0.62 if major else 0.68)
                d.line([(c + math.cos(a) * r0, c + math.sin(a) * r0),
                        (c + math.cos(a) * r1, c + math.sin(a) * r1)],
                       fill=self.pal.fg if major else self.pal.dim,
                       width=3 if major else 1)
            for k in range(6):                               # numbers 0..100
                a = math.radians(A0 + SWEEP * k / 5)
                nx, ny = c + math.cos(a) * r * 0.50, c + math.sin(a) * r * 0.50
                d.text((nx, ny), str(k * 20), font=self.f_tick,
                       fill=z_red if k * 20 >= 80 else self.pal.dim, anchor="mm")

            d.text((c, c - r * 0.33), label, font=self.f_glabel,
                   fill=self.pal.base(key), anchor="mm")
            wy0, wy1 = c + r * 0.36, c + r * 0.64
            d.rectangle([c - r * 0.48, wy0, c + r * 0.48, wy1],
                        fill=scale(self.pal.bg, 0.42), outline=self._metal(0.4))
            faces.append((spr, (px, py)))
        # canvas-space redline arc + readout window, reused per frame
        self._zbox = lambda cx: [cx - r * 0.79, self.gcy - r * 0.79,
                                 cx + r * 0.79, self.gcy + r * 0.79]
        self._win = (r * 0.48, r * 0.36, r * 0.64)
        return faces

    # ---------------------------------------------------------------- dynamics
    def _needle(self, key: str, target: float, dtc: float) -> float:
        """Damped-spring needle: angle + angular velocity integrated with
        substeps so a stalled frame (dt clamped to 0.2s) stays stable."""
        st = self._springs.setdefault(key, [target, 0.0])
        k = self.SPRING_W ** 2
        damp = 2 * self.SPRING_Z * self.SPRING_W
        steps = max(1, int(dtc / 0.02) + 1)
        sdt = dtc / steps
        for _ in range(steps):
            st[1] += (k * (target - st[0]) - damp * st[1]) * sdt
            st[0] += st[1] * sdt
        return max(-0.03, min(1.05, st[0]))      # small physical over-travel

    def _draw_needle(self, d, cx, p):
        r, cy = self.gr, self.gcy
        a = math.radians(A0 + SWEEP * p)
        ux, uy = math.cos(a), math.sin(a)
        px_, py_ = -uy, ux
        wd = max(2.0, r * 0.030)
        tip = (cx + ux * r * 0.68, cy + uy * r * 0.68)
        tail = (cx - ux * r * 0.16, cy - uy * r * 0.16)
        red = self.pal.base("cpu")
        d.polygon([tip, (cx + px_ * wd, cy + py_ * wd),
                   (tail[0] + px_ * wd * 0.8, tail[1] + py_ * wd * 0.8),
                   (tail[0] - px_ * wd * 0.8, tail[1] - py_ * wd * 0.8),
                   (cx - px_ * wd, cy - py_ * wd)], fill=red)
        hub = r * 0.10
        d.ellipse([cx - hub, cy - hub, cx + hub, cy + hub],
                  fill=self._metal(0.45), outline=self._metal(0.7), width=2)
        dot = hub * 0.35
        d.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=red)

    def _streaks(self, d, loadf: float, dtc: float):
        """Faint horizontal motion streaks in the dial band; density and
        speed follow CPU load. Phase is integrated so an eased speed change
        never teleports a streak."""
        self._ph += (50 + 500 * loadf) * dtc
        y0, y1 = self.strip_h + 4, self.panel_y - 6
        span = self.gx1 - self.gx0
        for i in range(dens(3 + int(loadf * 15))):
            r = random.Random(self.seed * 31 + i)
            ln = span * r.uniform(0.03, 0.11)
            x = self.gx0 + ((r.random() * span - self._ph * r.uniform(0.6, 1.5))
                            % (span + ln)) - ln
            yy = y0 + r.random() * (y1 - y0)
            d.line([(max(self.gx0, x), yy), (min(self.gx1, x + ln), yy)],
                   fill=mix(self.pal.bg, self.pal.fg, 0.05 + 0.09 * r.random()))

    # ------------------------------------------------------------------- frame
    def render(self) -> Image.Image:
        self._tick()
        dtc = min(self.dt, 0.2)          # a stall must not catapult the springs
        img = self._bg.copy()
        d = ImageDraw.Draw(img)
        v = vitals()
        gpu = v.gpu
        blink = (self.t * hz(self.HZ_BLINK)) % 1.0 < 0.5
        loadf = self.ease("load", v.load / 100)

        # background motion first, dial faces pasted over it
        self._streaks(d, loadf, dtc)
        for spr, xy in self._faces:
            img.paste(spr, xy)
        d = ImageDraw.Draw(img)

        # --- dials ---
        temp = f"{v.temp:.0f}°C" if v.temp else ""
        vals = {"cpu": (v.load, f"{v.load:.0f}%", temp),
                "gpu": (gpu.get("util", 0.0) if gpu else None,
                        f"{gpu['util']:.0f}%" if gpu else "--",
                        f"{gpu['temp']:.0f}°C" if gpu else ""),
                "ram": (v.mem_pct, f"{v.mem_pct:.0f}%", f"{v.mem_used:.0f}G"),
                "ssd": (v.disk_pct, f"{v.disk_pct:.0f}%",
                        f"{v.nvme_temp:.0f}°C" if v.nvme_temp
                        else f"{v.disk_used:.0f}G")}
        ww, wy0, wy1 = self._win
        for (key, _), cx in zip(self.gauges, self.gcx):
            val, txt, sub = vals[key]
            red = val is not None and val > REDLINE * 100
            if red and blink:                    # redline: zone band flares
                d.arc(self._zbox(cx), A0 + SWEEP * REDLINE, A0 + SWEEP,
                      fill=lighten(self.pal.base("cpu"), 0.55),
                      width=max(3, int(self.gr * 0.055)))
            self._draw_needle(d, cx, self._needle(key, (val or 0.0) / 100, dtc))
            my = self.gcy + (wy0 + wy1) / 2
            fill = (lighten(self.pal.base("cpu"), 0.4) if red and blink
                    else self.pal.fg)
            d.text((cx - ww + 6, my), txt, font=self.f_gval, fill=fill, anchor="lm")
            if sub:
                d.text((cx + ww - 6, my), sub, font=self.f_gsub,
                       fill=self.pal.dim, anchor="rm")

        # --- shift lights: lit count eased, full flash past 90% ---
        lit = self.ease("shift", loadf * self.N_LEDS)
        flash_all = v.load > 90
        for i, (cx, cy, r) in enumerate(self._leds):
            on = blink if flash_all else i + 0.5 <= lit
            if on:
                col = self._led_col(i / (self.N_LEDS - 1))
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
                hr = r * 0.45
                d.ellipse([cx - hr, cy - hr, cx + hr, cy + hr],
                          fill=lighten(col, 0.6))
        self._pips(d, 12, self.strip_h * 0.34, self.f_tiny.size * 0.42,
                   self.pal.hot("gpu"), 0, dark=scale(self.pal.dim, 0.3))
        d.text((12 + self.f_tiny.size * 2.6, self.strip_h * 0.48), "IGNITION",
               font=self.f_tiny, fill=self.pal.base("gpu"), anchor="lm")
        trip = f"RX {v.net_rx_total:.1f}G  TX {v.net_tx_total:.1f}G"
        d.text((self.w - 12, self.strip_h * 0.48), trip, font=self.f_tiny,
               fill=self.pal.dim, anchor="rm")

        # --- LCD clock + date + odometer + tell-tales ---
        now = time.localtime()
        d.text(self._lcd_xy, time.strftime("%H:%M:%S", now), font=self.f_lcd,
               fill=self.pal.fg, anchor="lm")
        d.text(self._date_xy, time.strftime("%a %d %b %Y", now).upper(),
               font=self.f_tiny, fill=self.pal.dim, anchor="mm")
        odo = f"{min(v.uptime // 3600, 9999):04d}{(v.uptime % 3600) // 60:02d}"
        for (cx, cy), ch in zip(self._odo_cells, odo):
            d.text((cx, cy), ch, font=self.f_odo, fill=self.pal.fg, anchor="mm")
        d.text(self._odo_colon, ":", font=self.f_odo, fill=self.pal.dim, anchor="mm")

        tell_on = {"NET": v.net_down + v.net_up > 100 * 2**10,
                   "DSK": v.disk_rd + v.disk_wr > 2**20,
                   "TMP": bool(v.temp and v.temp > 80)}
        for (box, lbl) in self._tells:
            on = tell_on[lbl]
            if lbl == "TMP" and on and not blink:
                on = False                       # overheat warning blinks
            col = (self.pal.base("cpu") if lbl == "TMP" else
                   self.pal.base("gpu")) if on else scale(self.pal.dim, 0.45)
            d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), lbl,
                   font=self.f_tiny, fill=col, anchor="mm")

        # --- CPU trace (history appends on the sample clock, not per frame) ---
        if self._trace:
            tx0, ty0, tx1, ty1 = self._trace
            dq = self.hist("cg:cpu", v.load / 100, n=48)
            step = (tx1 - tx0) / (len(dq) - 1)
            pts = [(tx0 + i * step, ty1 - f * (ty1 - ty0))
                   for i, f in enumerate(dq)]
            d.line(pts, fill=self.pal.base("cpu"), width=2)
            hx, hy = pts[-1]
            d.ellipse([hx - 3, hy - 3, hx + 3, hy + 3],
                      fill=lighten(self.pal.base("cpu"), 0.5))

        # --- per-core rev bars (eased) ---
        rx0, ry0, rx1, ry1 = self._rev
        n = max(1, len(v.per_core))
        bw = (rx1 - rx0) / n
        for i, core in enumerate(v.per_core):
            f = self.ease(f"core{i}", max(0.0, min(1.0, core / 100)))
            bx = rx0 + i * bw
            col = mix(scale(self.pal.base("ram"), 0.8), self.pal.base("cpu"),
                      f ** 1.3)
            d.rectangle([bx + 1, ry1 - (ry1 - ry0) * max(f, 0.03),
                         bx + max(2, bw - 2), ry1], fill=col)

        # --- gear from load, temps line ---
        gear = "N" if loadf < 0.04 else str(min(6, 1 + int(loadf * 6)))
        gcol = self.pal.base("cpu") if gear == "6" and blink else self.pal.fg
        d.text(self._gear_xy, gear, font=self.f_gear, fill=gcol, anchor="mm")
        temps = "  ".join(t for t in (
            f"CPU {v.temp:.0f}°" if v.temp else "",
            f"GPU {gpu['temp']:.0f}°" if gpu else "",
            f"SSD {v.nvme_temp:.0f}°" if v.nvme_temp else "") if t)
        if temps:
            f = self.fit_text(d, temps, self.f_tiny, self.Lw * 0.86, mono=True)
            d.text(self._temps_xy, temps, font=f, fill=self.pal.dim, anchor="ma")

        # --- telemetry mini-panels ---
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        rv, ru = human_rate(v.disk_rd)
        wv, wu = human_rate(v.disk_wr)
        la = os.getloadavg()
        pvals = (f"▼{dv}{du[0]} ▲{uv}{uu[0]}",
                 f"R{rv}{ru[0]} W{wv}{wu[0]}",
                 f"{gpu['mem_used'] / 1024:.1f}/{gpu['mem_total'] / 1024:.0f}G"
                 if gpu else "--",
                 f"{la[0]:.2f} {la[1]:.2f}",
                 f"{v.procs}",
                 f"{v.freq_ghz:.2f}GHz")
        for (px, py, mw), txt in zip(self._panels, pvals):
            f = self.fit_text(d, txt, self.f_pval, mw, mono=True)
            d.text((px, py), txt, font=f, fill=self.pal.fg, anchor="lm")

        return img


VIEWS = {"carbon-gauge": CarbonGaugeView}
