"""oscilloscope — a bench DSO/MSO front panel (horizontal).

Left ~70% is the CRT: a 10x8 graticule with three phosphor traces (CH1=CPU,
CH2=GPU, CH3=MEM) scrolling right-to-left. Right ~30% is the softkey side
menu: per-channel measurement boxes plus small stat keys. The bottom strip is
a logic analyser — one 2-level digital lane per core.

Two constraints shape the drawing code:
  * The sweep must never step. Samples land at READOUT_HZ, so between samples
    the traces slide left by a sub-sample offset computed from wall time since
    the last serial change of the "vitals" hold key.
  * No per-frame blur. Phosphor persistence is the same polyline redrawn
    shifted right by k samples and mixed toward bg — which is exactly the
    screen image of k samples ago, since the trace only translates. Glow is a
    wide low-alpha line under the crisp one.
"""

from __future__ import annotations

import os
import time

from PIL import Image, ImageDraw

import fx
from fx import (FXBase, Palette, human_rate, hz, lighten, load_mono, mix,
                sample_serial, scale, vitals)

SCOPE = Palette("scope",
                ((61, 255, 136), (255, 225, 74), (120, 220, 255), (255, 130, 170)),
                bg=(4, 12, 7), grid=(26, 62, 38), fg=(224, 250, 232),
                dim=(112, 168, 128))


class OscilloscopeView(FXBase):
    N = 64                          # visible samples per analog channel
    M = 12                          # samples per logic lane
    GHOSTS = (0.34, 0.20, 0.11)     # persistence brightness, k = 1, 2, 3 samples ago
    CHANNELS = (("CH1", "CPU", "cpu"), ("CH2", "GPU", "gpu"), ("CH3", "MEM", "ram"))
    SK_LABELS = ("RX ▼", "TX ▲", "DSK R", "DSK W", "VRAM", "SSD", "UPTIME", "SYS")

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or SCOPE
        self._start(0x05C1)
        self.host = os.uname().nodename.upper()[:12]
        self.ncores = max(1, len(vitals().per_core))

        self.f_tiny = load_mono(max(8, int(h * 0.021)))
        self.f_small = load_mono(max(9, int(h * 0.025)))
        self.f_label = load_mono(max(10, int(h * 0.029)))
        self.f_clock = load_mono(max(11, int(h * 0.033)))
        self.f_val = load_mono(max(14, int(h * 0.050)))

        # frame layout: status bar / scope+side-menu / logic strip
        self.pad = max(6, int(h * 0.015))
        self.bar_h = max(24, int(h * 0.075))
        self.logic_h = max(48, int(h * 0.17))
        self.sx0, self.sy0 = self.pad, self.bar_h + 5
        self.sx1, self.sy1 = int(w * 0.70), h - self.logic_h - 6
        self.rx0, self.rx1 = self.sx1 + self.pad + 2, w - self.pad
        self.scope_bg = scale(self.pal.bg, 0.55)

        # one extra step of overshoot each side so the ends never gap while
        # the trace is mid-slide; the layer bounds do the clipping.
        self.step = (self.sx1 - self.sx0) / (self.N - 2)
        self._serial = -1
        self._t_sample = 0.0

        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        # right column: 3 channel boxes on top, 2x4 softkey grid below
        gap = max(3, int(h * 0.009))
        ch_h = int((self.sy1 - self.sy0) * 0.20)
        self.ch_boxes = []
        y = self.sy0
        for _ in range(3):
            self.ch_boxes.append((self.rx0, y, self.rx1, y + ch_h))
            y += ch_h + gap
        rows_, cols_ = 4, 2
        sk_h = (self.sy1 - y - (rows_ - 1) * gap) / rows_
        sk_w = (self.rx1 - self.rx0 - gap) / cols_
        self.sk_boxes = []
        for i in range(len(self.SK_LABELS)):
            r, c = divmod(i, cols_)
            x0 = self.rx0 + c * (sk_w + gap)
            y0 = y + r * (sk_h + gap)
            self.sk_boxes.append((x0, y0, x0 + sk_w, y0 + sk_h))

        # logic lanes: one row when lanes stay wide enough, else two rows
        ax0, ax1 = self.pad + 4, w - self.pad - 4
        hdr = self.f_tiny.size + 6
        lt0, lt1 = h - self.logic_h + hdr, h - 5
        rows = 1 if (ax1 - ax0) / self.ncores >= 64 else 2
        self.l_cols = -(-self.ncores // rows)
        lane_w = (ax1 - ax0) / self.l_cols
        row_h = (lt1 - lt0) / rows
        lblw = probe.textlength("D19", font=self.f_tiny) + 5
        self.lanes = []
        for i in range(self.ncores):
            r, c = divmod(i, self.l_cols)
            x0 = ax0 + c * lane_w
            y0 = lt0 + r * row_h
            self.lanes.append((x0 + 2, y0 + (row_h - self.f_tiny.size) / 2 - 1,
                               x0 + lblw + 2, x0 + lane_w - 8,
                               y0 + 4, y0 + row_h - 5))
        self._logic_geom = (ax0, ax1, lt0, lt1, rows, lane_w, row_h)

        self._bg = self._build_bg()

    # --- static layer ---------------------------------------------------------
    def _build_bg(self) -> Image.Image:
        w, h = self.w, self.h
        pal = self.pal
        img = Image.new("RGB", (w, h), pal.bg)
        d = ImageDraw.Draw(img)
        cpu = pal.base("cpu")

        # status bar
        d.rectangle([0, 0, w, self.bar_h], fill=mix(pal.bg, pal.fg, 0.05))
        d.line([(0, self.bar_h), (w, self.bar_h)], fill=scale(cpu, 0.55), width=2)
        ty = (self.bar_h - self.f_label.size) / 2 - 1
        x = self.pad + self.f_small.size * 1.6      # dot slot, drawn per frame
        d.text((x, ty), "RUN", font=self.f_label, fill=cpu)
        x += d.textlength("RUN", font=self.f_label) + self.f_label.size * 1.2
        brand = "SCOPE-2040 · MSO 20CH"
        d.text((x, ty), brand, font=self.f_label, fill=pal.fg)
        x += d.textlength(brand, font=self.f_label) + self.f_label.size * 1.2
        d.text((x, ty + 1), self.host, font=self.f_small, fill=pal.dim)

        # CRT bezel, fill and scanlines (prebuilt: zero per-frame cost)
        d.rectangle([self.sx0 - 3, self.sy0 - 3, self.sx1 + 3, self.sy1 + 3],
                    outline=scale(pal.dim, 0.6), width=2)
        d.rectangle([self.sx0, self.sy0, self.sx1, self.sy1], fill=self.scope_bg)
        dark = mix(self.scope_bg, (0, 0, 0), 0.45)
        for yy in range(self.sy0 + 2, self.sy1, 3):
            d.line([(self.sx0, yy), (self.sx1, yy)], fill=dark, width=1)

        # graticule: 10x8 dotted divisions + solid centre lines with fine ticks
        divx = (self.sx1 - self.sx0) / 10
        divy = (self.sy1 - self.sy0) / 8
        dots = []
        for i in range(1, 10):
            gx = self.sx0 + i * divx
            dots += [(gx, yy) for yy in range(self.sy0 + 2, self.sy1, 4)]
        for i in range(1, 8):
            gy = self.sy0 + i * divy
            dots += [(xx, gy) for xx in range(self.sx0 + 2, self.sx1, 4)]
        d.point(dots, fill=pal.grid)
        cx, cyy = self.sx0 + 5 * divx, self.sy0 + 4 * divy
        mid = scale(pal.dim, 0.55)
        d.line([(cx, self.sy0), (cx, self.sy1)], fill=mid, width=1)
        d.line([(self.sx0, cyy), (self.sx1, cyy)], fill=mid, width=1)
        k = 0
        while self.sx0 + k * divx / 5 < self.sx1:            # fine ticks, 0.2 div
            tx = self.sx0 + k * divx / 5
            d.line([(tx, cyy - 3), (tx, cyy + 3)], fill=mid, width=1)
            d.line([(tx, self.sy0), (tx, self.sy0 + 3)], fill=pal.grid, width=1)
            d.line([(tx, self.sy1 - 3), (tx, self.sy1)], fill=pal.grid, width=1)
            k += 1
        k = 0
        while self.sy0 + k * divy / 5 < self.sy1:
            tyk = self.sy0 + k * divy / 5
            d.line([(cx - 3, tyk), (cx + 3, tyk)], fill=mid, width=1)
            d.line([(self.sx0, tyk), (self.sx0 + 3, tyk)], fill=pal.grid, width=1)
            d.line([(self.sx1 - 3, tyk), (self.sx1, tyk)], fill=pal.grid, width=1)
            k += 1

        # side-menu chrome: channel boxes with colour tab + static labels
        box_fill = mix(pal.bg, pal.fg, 0.035)
        for ci, (chn, name, key) in enumerate(self.CHANNELS):
            x0, y0, x1, y1 = self.ch_boxes[ci]
            base = pal.base(key)
            d.rectangle([x0, y0, x1, y1], fill=box_fill, outline=scale(pal.dim, 0.5))
            d.rectangle([x0, y0, x0 + 4, y1], fill=base)
            d.text((x0 + 10, y0 + 4), chn, font=self.f_label, fill=base)
            d.text((x0 + 10 + d.textlength(chn, font=self.f_label) + 6, y0 + 4),
                   name, font=self.f_label, fill=pal.dim)
        for i, lab in enumerate(self.SK_LABELS):
            x0, y0, x1, y1 = self.sk_boxes[i]
            d.rectangle([x0, y0, x1, y1], fill=box_fill, outline=scale(pal.dim, 0.4))
            d.text((x0 + 6, (y0 + y1 - self.f_tiny.size) / 2 - 1), lab,
                   font=self.f_tiny, fill=pal.dim)

        # logic strip frame, header and lane separators
        ax0, ax1, lt0, lt1, rows, lane_w, row_h = self._logic_geom
        d.rectangle([self.pad, h - self.logic_h, w - self.pad, h - 2],
                    outline=scale(pal.dim, 0.5))
        d.text((ax0 + 2, h - self.logic_h + 2),
               f"LOGIC ANALYSER · D00–D{self.ncores - 1:02d} · THRESH 50%",
               font=self.f_tiny, fill=pal.dim)
        tag = "1 SA/DIV"
        d.text((ax1 - d.textlength(tag, font=self.f_tiny), h - self.logic_h + 2),
               tag, font=self.f_tiny, fill=scale(pal.dim, 0.8))
        sepc = scale(pal.dim, 0.28)
        for c in range(1, self.l_cols):
            d.line([(ax0 + c * lane_w, lt0), (ax0 + c * lane_w, lt1)],
                   fill=sepc, width=1)
        if rows == 2:
            d.line([(ax0, lt0 + row_h), (ax1, lt0 + row_h)], fill=sepc, width=1)
        # static hi/lo level rails so a lane reads as a logic channel even
        # when its trace pins to one level
        rail = scale(pal.dim, 0.32)
        for _, _, wx0, wx1, y_hi, y_lo in self.lanes:
            d.line([(wx0, y_hi), (wx1, y_hi)], fill=rail, width=1)
            d.line([(wx0, y_lo), (wx1, y_lo)], fill=rail, width=1)
        return img

    # --- helpers --------------------------------------------------------------
    def _ch_y(self, ci: int, v01: float) -> float:
        """Stacked channel bands: centres at 26/52/78% of the CRT height."""
        sh = self.sy1 - self.sy0
        amp = sh * 0.30
        centre = self.sy0 + sh * (0.26 + 0.26 * ci)
        return centre + amp / 2 - max(0.0, min(1.0, v01)) * amp

    # --- frame ----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        v = vitals()
        pal = self.pal
        img = self._bg.copy().convert("RGBA")
        d = ImageDraw.Draw(img)

        # sub-sample scroll: wall time since the last vitals resample, as a
        # fraction of the sample period — this is what keeps the sweep fluid.
        serial = sample_serial("vitals")
        if serial != self._serial:
            self._serial, self._t_sample = serial, self.t
        frac = min(1.0, (self.t - self._t_sample) * fx.READOUT_HZ)

        gpu = v.gpu
        ch_now = (v.load / 100.0,
                  (gpu["util"] / 100.0) if gpu else 0.0,
                  v.mem_pct / 100.0)

        # --- status bar dynamics ---
        ty = (self.bar_h - self.f_clock.size) / 2 - 1
        r = self.f_small.size * 0.42
        dcx = self.pad + r + 2
        dot = mix(scale(pal.dim, 0.55), pal.hot("cpu"), 1.0 - frac)  # sample flash
        d.ellipse([dcx - r, self.bar_h / 2 - r, dcx + r, self.bar_h / 2 + r], fill=dot)
        x = self.rx1
        clock = time.strftime("%H:%M:%S")
        x -= d.textlength(clock, font=self.f_clock)
        d.text((x, ty), clock, font=self.f_clock, fill=pal.fg)
        for txt, col, f in ((time.strftime("%a %d/%m").upper(), pal.dim, self.f_small),
                            (f"{fx.READOUT_HZ:.1f} SA/S", pal.base("cpu"), self.f_small),
                            (f"{self.N} PTS", pal.dim, self.f_small)):
            x -= self.f_small.size * 1.2 + d.textlength(txt, font=f)
            d.text((x, ty + (self.f_clock.size - f.size) / 2), txt, font=f, fill=col)

        # --- traces on a CRT-sized layer (layer bounds clip the overshoot) ---
        lw_, lh_ = self.sx1 - self.sx0, self.sy1 - self.sy0
        layer = Image.new("RGBA", (lw_, lh_), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ch_hists = []
        for ci, (chn, name, key) in enumerate(self.CHANNELS):
            vals = list(self.hist(f"ch{ci}", ch_now[ci], n=self.N))
            ch_hists.append(vals)
            base = pal.base(key)
            if ci == 1 and not gpu:
                base = scale(base, 0.45)         # no GPU: CH2 idles, dimmed
            pts = [(lw_ - (self.N - 1 - i + frac) * self.step,
                    self._ch_y(ci, p) - self.sy0) for i, p in enumerate(vals)]
            for k in (3, 2, 1):                  # dimmest ghost first
                gcol = mix(pal.bg, base, self.GHOSTS[k - 1])
                ld.line([(px + k * self.step, py) for px, py in pts],
                        fill=gcol + (255,), width=1)
            ld.line(pts, fill=base + (70,), width=6)     # glow pass, no blur
            ld.line(pts, fill=base + (255,), width=2)
            ld.line(pts, fill=lighten(base, 0.45) + (255,), width=1)
            # beam spot where the trace is currently being written
            bx, by = pts[-1]
            bx = min(bx, lw_ - 2)
            hot = lighten(base, 0.75)
            ld.ellipse([bx - 4, by - 4, bx + 4, by + 4], fill=base + (90,))
            ld.ellipse([bx - 2, by - 2, bx + 2, by + 2], fill=hot + (255,))
        img.alpha_composite(layer, (self.sx0, self.sy0))
        d = ImageDraw.Draw(img)

        # --- trigger: level line + edge arrow at the CH1 mean, TRIG'D flag ---
        trig = pal.base("ssd")
        mu = sum(ch_hists[0]) / len(ch_hists[0])
        tly = self.ease("trig", self._ch_y(0, mu))
        d.point([(px, tly) for px in range(self.sx0 + 4, self.sx1 - 12, 7)],
                fill=scale(trig, 0.55))
        d.polygon([(self.sx1 - 2, tly), (self.sx1 - 11, tly - 6),
                   (self.sx1 - 11, tly + 6)], fill=trig)
        if ch_now[0] > mu:
            if int(self.t * hz(2.0)) % 2 == 0:
                flag, fcol = "TRIG'D", trig
            else:
                flag, fcol = None, trig
        else:
            flag, fcol = "AUTO", pal.dim
        if flag:
            d.text((self.sx1 - 8 - d.textlength(flag, font=self.f_tiny),
                    self.sy0 + 5), flag, font=self.f_tiny, fill=fcol)

        # channel legend + timebase inside the CRT's bottom edge
        ly = self.sy1 - self.f_tiny.size - 4
        lx = self.sx0 + 8
        for chn, name, key in self.CHANNELS:
            txt = f"{chn}:{name}"
            d.text((lx, ly), txt, font=self.f_tiny, fill=pal.base(key))
            lx += d.textlength(txt, font=self.f_tiny) + self.f_tiny.size * 1.3
        spdiv = (self.N - 2) / 10.0 / fx.READOUT_HZ
        tb = f"{spdiv:.1f}S/DIV"
        d.text((self.sx1 - 8 - d.textlength(tb, font=self.f_tiny), ly),
               tb, font=self.f_tiny, fill=pal.dim)

        # --- side menu: per-channel measurement boxes ---
        subs = ((f"{v.temp:.0f}°C · {v.freq_ghz:.1f}GHZ" if v.temp
                 else f"{v.freq_ghz:.1f}GHZ"),
                (f"{gpu['temp']:.0f}°C · {gpu['power']:.0f}W" if gpu else "N/A"),
                f"{v.mem_used:.1f}/{v.mem_total:.0f}G")
        vals_txt = (f"{v.load:.0f}%",
                    f"{gpu['util']:.0f}%" if gpu else "--",
                    f"{v.mem_pct:.0f}%")
        for ci, (chn, name, key) in enumerate(self.CHANNELS):
            x0, y0, x1, y1 = self.ch_boxes[ci]
            base = pal.base(key)
            tw = d.textlength(vals_txt[ci], font=self.f_val)
            d.text((x1 - 8 - tw, y0 + 3), vals_txt[ci], font=self.f_val, fill=pal.fg)
            d.text((x0 + 10, y0 + 6 + self.f_label.size * 1.35), subs[ci],
                   font=self.f_tiny, fill=pal.dim)
            hv = [p * 100 for p in ch_hists[ci]]
            st = (f"MIN{min(hv):3.0f} MAX{max(hv):3.0f} "
                  f"AVG{sum(hv) / len(hv):3.0f} PP{max(hv) - min(hv):3.0f}")
            d.text((x0 + 10, y1 - self.f_tiny.size - 4), st,
                   font=self.f_tiny, fill=scale(base, 0.85))

        # --- side menu: small softkeys ---
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        rv, ru = human_rate(v.disk_rd)
        wv, wu = human_rate(v.disk_wr)
        up = v.uptime
        load1 = os.getloadavg()[0]
        sk_vals = (f"{dv} {du}", f"{uv} {uu}", f"{rv} {ru}", f"{wv} {wu}",
                   (f"{gpu['mem_used'] / 1024:.1f}/{gpu['mem_total'] / 1024:.0f}G"
                    if gpu else "--"),
                   f"{v.disk_pct:.0f}% {v.disk_used:.0f}G",
                   f"{up // 86400}D {up % 86400 // 3600:02d}:{up % 3600 // 60:02d}",
                   f"{v.procs}P {load1:.2f}")
        for i, txt in enumerate(sk_vals):
            x0, y0, x1, y1 = self.sk_boxes[i]
            f = self.fit_text(d, txt, self.f_small,
                              x1 - x0 - 14 - d.textlength(self.SK_LABELS[i],
                                                          font=self.f_tiny),
                              mono=True)
            d.text((x1 - 6 - d.textlength(txt, font=f),
                    (y0 + y1 - f.size) / 2 - 1), txt, font=f, fill=pal.fg)

        # --- logic analyser: per-core 2-level digital lanes, same slide ---
        base = pal.base("cpu")
        lo_col = scale(base, 0.42)
        edge = scale(base, 0.75)
        for i, (tx, tly_, wx0, wx1, y_hi, y_lo) in enumerate(self.lanes):
            load = v.per_core[i] if i < len(v.per_core) else 0.0
            vals = list(self.hist(f"core{i}", load, n=self.M))
            d.text((tx, tly_), f"D{i:02d}", font=self.f_tiny,
                   fill=pal.hot("cpu") if load >= 50.0 else scale(pal.dim, 0.9))
            lstep = (wx1 - wx0) / (self.M - 2)
            xs = [wx1 - (self.M - 1 - j + frac) * lstep for j in range(self.M)]
            lv = [p >= 50.0 for p in vals]
            for j in range(self.M - 1):
                y = y_hi if lv[j] else y_lo
                if xs[j + 1] > wx0 and xs[j] < wx1:
                    d.line([(max(xs[j], wx0), y), (min(xs[j + 1], wx1), y)],
                           fill=base if lv[j] else lo_col, width=2)
                if lv[j + 1] != lv[j] and wx0 < xs[j + 1] < wx1:
                    d.line([(xs[j + 1], y_hi), (xs[j + 1], y_lo)], fill=edge, width=1)
            y = y_hi if lv[-1] else y_lo                 # newest runs to the edge
            if xs[-1] < wx1:
                d.line([(max(xs[-1], wx0), y), (wx1, y)],
                       fill=base if lv[-1] else lo_col, width=2)

        return img.convert("RGB")


VIEWS = {"oscilloscope": OscilloscopeView}
