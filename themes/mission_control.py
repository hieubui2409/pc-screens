"""mission-control — a flight-control telemetry wall (horizontal).

A fixed grid of thin-framed modules, NASA/ESA console style: mission clock
with MET, CPU telemetry with a 60-sample graph, a per-core matrix, GPU,
memory tank, storage, comms, a subsystem status board with blinking LEDs,
and a scrolling event ticker along the bottom. Signature palette is console
amber with green/blue/red status accents; --palette recolours everything.

Layout is a module grid, not a flow: rects are computed once in __init__
(per canvas width the least important modules are dropped), the frames and
captions are prebuilt into the background, and render() only draws values.
"""

from __future__ import annotations

import os
import random
import time

from PIL import Image, ImageDraw

from fx import (FXBase, Palette, human_rate, lighten, load_mono, mix,
                sample_serial, scale, vitals)

MISSION = Palette("mission",
                  ((255, 176, 59), (97, 255, 140), (120, 200, 255), (255, 96, 96)),
                  bg=(7, 8, 10), grid=(30, 27, 18),
                  fg=(238, 232, 214), dim=(148, 138, 108))

CAPTIONS = {
    "clock": "MISSION CLOCK", "cpu": "CPU TELEMETRY", "gpu": "GPU TELEMETRY",
    "cores": "CORE MATRIX", "mem": "MEMORY", "ssd": "STORAGE",
    "net": "COMMS", "status": "STATUS BOARD",
}


class MissionControlView(FXBase):
    TICKER_PPS = 70      # ticker scroll, wall-clock px/s — never frame-based
    BLINK_HZ = 1.5       # LED blink cadence when a subsystem is not nominal

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or MISSION
        self._start(0x4D15)
        self.host = os.uname().nodename.upper()[:12]

        # Colour roles by accent slot, so --palette recolours the whole wall.
        # Red-ish slot is reserved for alarms so it never reads as decoration.
        self.c_main = self.pal.base("cpu")      # console amber
        self.c_ok = self.pal.base("gpu")
        self.c_info = self.pal.base("ram")
        self.c_crit = self.pal.base("ssd")
        self.frame_col = scale(self.c_main, 0.38)
        self.panel_col = mix(self.pal.bg, self.pal.grid, 0.35)

        self._layout()
        rh = self.rh
        self.f_cap = load_mono(11)
        self.f_tiny = load_mono(9)
        self.f_small = load_mono(max(10, int(rh * 0.062)))
        self.f_mid = load_mono(max(12, int(rh * 0.115)))
        self.f_val = load_mono(max(20, int(rh * 0.30)))
        self.f_tick = load_mono(max(11, int(self.h * 0.028)))
        # clock face probed against its own module, not the canvas
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        cw = self.rect["clock"][2] - self.rect["clock"][0] - 28
        size = int(min(rh * 0.42, cw * 0.24))
        while size > 14 and probe.textlength("00:00:00", font=load_mono(size)) > cw:
            size = int(size * 0.94)
        self.f_time = load_mono(size)

        self._tick_serial = -1
        self._tick_text = ""
        self._tick_w = 1
        self._bg = self._build_bg()

    # --- module grid ----------------------------------------------------------
    def _layout(self) -> None:
        m, g = 8, 8
        th = max(26, int(self.h * 0.075))
        x0, x1 = m, self.w - m
        y1 = self.h - m - th - g
        self.rh = rh = (y1 - m - g) // 2
        # At <1400px the wall keeps 7 of 8 modules: STORAGE goes (its numbers
        # survive on the ticker); everything else measured to still fit.
        if self.w >= 1400:
            rows = [[("clock", 25), ("cpu", 28), ("gpu", 24), ("status", 23)],
                    [("cores", 28), ("mem", 23), ("ssd", 21), ("net", 28)]]
        else:
            rows = [[("clock", 34), ("cpu", 36), ("gpu", 30)],
                    [("cores", 30), ("mem", 21), ("net", 26), ("status", 23)]]
        self.mods: list[tuple[str, tuple]] = []
        for r, row in enumerate(rows):
            ry0 = m if r == 0 else y1 - rh       # rows pinned, edges aligned
            inner = (x1 - x0) - g * (len(row) - 1)
            tot = sum(wt for _, wt in row)
            cx = x0
            for i, (name, wt) in enumerate(row):
                rx1 = x1 if i == len(row) - 1 else cx + round(inner * wt / tot)
                self.mods.append((name, (cx, ry0, rx1, ry0 + rh)))
                cx = rx1 + g
        self.rect = dict(self.mods)
        self.ticker_r = (x0, y1 + g, x1, y1 + g + th)

    # --- static layer: interiors, noise, frames, captions ---------------------
    def _frame(self, d, r, caption: str, tag: str) -> None:
        x0, y0, x1, y1 = r
        d.rectangle(r, outline=self.frame_col, width=1)
        tk, c = 7, scale(self.c_main, 0.85)
        for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                               (x0, y1, 1, -1), (x1, y1, -1, -1)):
            d.line([(cx, cy), (cx + tk * sx, cy)], fill=c)
            d.line([(cx, cy), (cx, cy + tk * sy)], fill=c)
        txt = f" {caption} "
        tw = d.textlength(txt, font=self.f_cap)
        d.rectangle([x0 + 10, y0 - 6, x0 + 10 + tw, y0 + 6], fill=self.pal.bg)
        d.text((x0 + 10, y0 - self.f_cap.size * 0.62), txt,
               font=self.f_cap, fill=self.c_main)
        gtxt = f" {tag} "
        gw = d.textlength(gtxt, font=self.f_tiny)
        d.rectangle([x1 - 10 - gw, y0 - 5, x1 - 10, y0 + 5], fill=self.pal.bg)
        d.text((x1 - 10 - gw, y0 - self.f_tiny.size * 0.62), gtxt,
               font=self.f_tiny, fill=self.pal.dim)

    def _build_bg(self) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), self.pal.bg)
        d = ImageDraw.Draw(img)
        for r in [r for _, r in self.mods] + [self.ticker_r]:
            d.rectangle(r, fill=self.panel_col)
        # static dither so the wall never reads as flat colour; prebuilt = free
        rng = random.Random(0x51A7)
        buckets: list[list] = [[], [], []]
        for _ in range(self.w * self.h // 150):
            buckets[rng.randrange(3)].append(
                (rng.randrange(self.w), rng.randrange(self.h)))
        for pts, f in zip(buckets, (0.05, 0.09, 0.15)):
            d.point(pts, fill=mix(self.pal.bg, self.pal.dim, f))
        for i, (name, r) in enumerate(self.mods):
            self._frame(d, r, CAPTIONS[name], f"M{i + 1:02d}")
        self._frame(d, self.ticker_r, "EVENT LOG", f"M{len(self.mods) + 1:02d}")
        return img.convert("RGBA")

    # --- shared widgets -------------------------------------------------------
    def _kv(self, d, x, y, key, val, col) -> None:
        d.text((x, y), key, font=self.f_small, fill=self.pal.dim)
        d.text((x + self.f_small.size * 4.6, y), val, font=self.f_small, fill=col)

    def _graph(self, d, x, y, gw, gh, vals, base, second=None, sec_col=None):
        """Framed telemetry graph: solid area fill (no alpha layer — cheap),
        50% reference line, axis ticks on the left and bottom edges."""
        x, y = int(x), int(y)
        n = len(vals)
        step = gw / (n - 1)
        pts = [(x + i * step, y + gh - 1 - max(0.0, min(1.0, v)) * (gh - 3))
               for i, v in enumerate(vals)]
        d.polygon([(x, y + gh)] + pts + [(x + gw, y + gh)],
                  fill=mix(self.panel_col, base, 0.22))
        d.line([(x, y + gh * 0.5), (x + gw, y + gh * 0.5)],
               fill=mix(self.panel_col, self.pal.dim, 0.35))
        d.line(pts, fill=base, width=2)
        if second is not None:
            p2 = [(x + i * step, y + gh - 1 - max(0.0, min(1.0, v)) * (gh - 3))
                  for i, v in enumerate(second)]
            d.line(p2, fill=sec_col or self.c_ok, width=1)
        d.rectangle([x, y, x + gw, y + gh], outline=self.frame_col)
        for i in range(5):
            yy = y + gh * i / 4
            d.line([(x - 3, yy), (x, yy)], fill=self.pal.dim)
        for i in range(0, n, 5):
            xx = x + i * step
            d.line([(xx, y + gh), (xx, y + gh + 3)], fill=self.pal.dim)

    def _tank(self, d, x, y, tw, th, frac, base) -> None:
        """Vertical tank-level gauge with quarter ticks."""
        d.rectangle([x, y, x + tw, y + th], outline=self.frame_col)
        f = max(0.0, min(1.0, frac))
        fy = y + th - 2 - (th - 4) * f
        if f > 0.01:
            d.rectangle([x + 2, fy, x + tw - 2, y + th - 2],
                        fill=mix(self.panel_col, base, 0.60))
            d.line([(x + 2, fy), (x + tw - 2, fy)], fill=lighten(base, 0.35))
        for i in range(1, 4):
            yy = y + th * i / 4
            d.line([(x - 4, yy), (x, yy)], fill=self.pal.dim)

    def _state(self, frac):
        if frac is None:
            return scale(self.pal.dim, 0.7), "N/A", False
        if frac > 0.90:
            return self.c_crit, "CRITICAL", True
        if frac > 0.70:
            return self.c_main, "ELEVATED", True
        return self.c_ok, "NOMINAL", False

    @staticmethod
    def _met(up: int) -> str:
        return f"{up // 86400:03d}:{up % 86400 // 3600:02d}:{up % 3600 // 60:02d}"

    # --- modules --------------------------------------------------------------
    def _m_clock(self, d, r, v) -> None:
        x0, y0, x1, y1 = r
        px, py = x0 + 14, y0 + 14
        d.text((px, py), f"MSN {self.host}", font=self.f_small, fill=self.pal.dim)
        doy = f"DOY {time.localtime().tm_yday:03d}"
        d.text((x1 - 14 - d.textlength(doy, font=self.f_small), py), doy,
               font=self.f_small, fill=self.pal.dim)
        ty = py + self.f_small.size + 6
        hhmmss = time.strftime("%H:%M:%S")
        # cheap CRT echo instead of a full-canvas blur pass — perf budget
        d.text((px + 2, ty + 2), hhmmss, font=self.f_time,
               fill=scale(self.c_main, 0.35))
        d.text((px, ty), hhmmss, font=self.f_time, fill=self.pal.fg)
        ly = ty + self.f_time.size * 1.22
        d.text((px, ly), time.strftime("%a %d %b %Y").upper(),
               font=self.f_mid, fill=self.pal.dim)
        my = ly + self.f_mid.size * 1.45
        met = f"MET {self._met(v.uptime)}"
        d.text((px, my), met, font=self.f_mid, fill=self.c_main)
        if (self.t * 1.1) % 1.0 < 0.55:      # console cursor, wall-clock blink
            d.text((px + d.textlength(met + " ", font=self.f_mid), my), "█",
                   font=self.f_mid, fill=self.c_main)

    def _big_pct(self, d, x, y, val, suffix, max_w, col):
        f = self.fit_text(d, val + suffix, self.f_val, max_w, mono=True)
        d.text((x, y), val, font=f, fill=self.pal.fg)
        d.text((x + d.textlength(val, font=f) + 4,
                y + f.size - self.f_mid.size), suffix, font=self.f_mid, fill=col)
        return f.size

    def _m_cpu(self, d, r, v) -> None:
        x0, y0, x1, y1 = r
        px, py = x0 + 14, y0 + 18
        lw = (x1 - x0) * 0.40
        sz = self._big_pct(d, px, py, f"{v.load:.0f}", "%", lw - 8, self.c_main)
        yy = py + sz * 1.18
        la = os.getloadavg()
        for k, val in (("TEMP", f"{v.temp:.0f}°C" if v.temp else "--"),
                       ("FREQ", f"{v.freq_ghz:.2f} GHZ"),
                       ("LOAD", f"{la[0]:.2f} {la[1]:.2f}")):
            self._kv(d, px, yy, k, val, self.pal.fg)
            yy += self.f_small.size * 1.62
        gx = px + lw + 14
        gy = y0 + 22
        gh = y1 - 38 - gy
        gw = x1 - 16 - gx
        self._graph(d, gx, gy, gw, gh, list(self.hist("cpu", v.load / 100, n=60)),
                    self.c_main)
        d.text((gx + 4, gy + 2), "100", font=self.f_tiny, fill=self.pal.dim)
        d.text((gx + 4, gy + gh - 11), "0", font=self.f_tiny, fill=self.pal.dim)
        d.text((gx, y1 - 24), "LOAD / 60 SAMPLES", font=self.f_tiny,
               fill=self.pal.dim)

    def _m_gpu(self, d, r, v) -> None:
        x0, y0, x1, y1 = r
        px, py = x0 + 14, y0 + 18
        g = v.gpu
        if not g:
            msg = "NO DOWNLINK"
            d.text(((x0 + x1 - d.textlength(msg, font=self.f_mid)) / 2,
                    (y0 + y1) / 2 - self.f_mid.size), msg,
                   font=self.f_mid, fill=self.pal.dim)
            if (self.t * self.BLINK_HZ) % 1.0 < 0.62:
                sub = "ACQUIRING SIGNAL ..."
                d.text(((x0 + x1 - d.textlength(sub, font=self.f_tiny)) / 2,
                        (y0 + y1) / 2 + 6), sub, font=self.f_tiny,
                       fill=self.c_main)
            return
        lw = (x1 - x0) * 0.40
        sz = self._big_pct(d, px, py, f"{g['util']:.0f}", "%", lw - 8, self.c_ok)
        yy = py + sz * 1.18
        for k, val in (("TEMP", f"{g['temp']:.0f}°C"),
                       ("PWR", f"{g['power']:.0f} W"),
                       ("VRAM", f"{g['mem_used'] / 1024:.1f}/"
                                f"{g['mem_total'] / 1024:.0f}G")):
            self._kv(d, px, yy, k, val, self.pal.fg)
            yy += self.f_small.size * 1.62
        gx = px + lw + 14
        gy = y0 + 22
        gh = y1 - 38 - gy
        self._graph(d, gx, gy, x1 - 16 - gx, gh,
                    list(self.hist("gpu", g["util"] / 100, n=40)), self.c_ok)
        d.text((gx, y1 - 24), "UTIL / 40 SAMPLES", font=self.f_tiny,
               fill=self.pal.dim)

    def _m_cores(self, d, r, v) -> None:
        x0, y0, x1, y1 = r
        px, py = x0 + 14, y0 + 16
        cores = v.per_core[:20]
        n = max(1, len(cores))
        cols = 10 if n > 10 else n
        rows = -(-n // cols)
        gap = 3
        iw = x1 - x0 - 28
        cw = (iw - gap * (cols - 1)) / cols
        mh = min(((y1 - 16) - py) * 0.62, 100)
        ch = (mh - gap * (rows - 1)) / rows
        peak_i = max(range(n), key=lambda i: cores[i]) if cores else 0
        for i, load in enumerate(cores):
            cx = px + (i % cols) * (cw + gap)
            cy = py + (i // cols) * (ch + gap)
            fl = max(0.0, min(1.0, load / 100))
            hot = load > 85          # overdriven core flips to the alarm colour
            fill = (mix(self.panel_col, self.c_crit, 0.30 + 0.55 * fl) if hot
                    else mix(self.panel_col, self.c_main, 0.06 + 0.74 * fl))
            d.rectangle([cx, cy, cx + cw, cy + ch], fill=fill,
                        outline=self.c_crit if hot else self.frame_col)
            fnt = self.f_mid if cw >= 34 else self.f_tiny
            tcol = self.pal.bg if (fl > 0.45 or hot) else self.pal.dim
            txt = f"{load:.0f}"
            d.text((cx + (cw - d.textlength(txt, font=fnt)) / 2,
                    cy + (ch - fnt.size) / 2 - 1), txt, font=fnt, fill=tcol)
            if cw >= 34:
                d.text((cx + 3, cy + 2), f"{i:02d}", font=self.f_tiny, fill=tcol)
        avg = sum(cores) / n if cores else 0.0
        sy = py + mh + 10
        d.text((px, sy), f"AVG {avg:.0f}%  ·  PEAK C{peak_i:02d} "
                         f"{cores[peak_i]:.0f}%" if cores else "AVG --",
               font=self.f_small, fill=self.pal.dim)
        by = sy + self.f_small.size * 1.6
        self.seg_bar(d, px, by, iw, 8, self.ease("coreavg", avg / 100), 24,
                     self.c_main, scale(self.c_main, 0.18))
        d.text((px, by + 14), f"{n} THREADS  ·  {v.freq_ghz:.2f} GHZ",
               font=self.f_tiny, fill=self.pal.dim)

    def _m_mem(self, d, r, v) -> None:
        x0, y0, x1, y1 = r
        px, py = x0 + 12, y0 + 18
        tankw = 22
        tx = x1 - 14 - tankw
        la = tx - 12 - px
        sz = self._big_pct(d, px, py, f"{v.mem_used:.1f}", "GB", la * 0.9,
                           self.c_info)
        yy = py + sz * 1.18
        self._kv(d, px, yy, "TOTL", f"{v.mem_total:.0f} GB", self.pal.fg)
        yy += self.f_small.size * 1.62
        self._kv(d, px, yy, "USED", f"{v.mem_pct:.0f}%", self.pal.fg)
        by = y1 - 34
        self.seg_bar(d, px, by, la, 9, self.ease("mem", v.mem_pct / 100), 18,
                     self.c_info, scale(self.c_info, 0.18))
        d.text((px, by + 15), "COMMIT PRESSURE", font=self.f_tiny,
               fill=self.pal.dim)
        self._tank(d, tx, py, tankw, y1 - 14 - py,
                   self.ease("memtank", v.mem_pct / 100), self.c_info)

    def _m_ssd(self, d, r, v) -> None:
        x0, y0, x1, y1 = r
        px, py = x0 + 14, y0 + 18
        lw = (x1 - x0) * 0.42
        sz = self._big_pct(d, px, py, f"{v.disk_pct:.0f}", "%", lw - 8,
                           self.c_main)
        yy = py + sz * 1.18
        self._kv(d, px, yy, "NVME", f"{v.nvme_temp:.0f}°C" if v.nvme_temp
                 else "--", self.pal.fg)
        yy += self.f_small.size * 1.62
        self._kv(d, px, yy, "USED", f"{v.disk_used:.0f}/{v.disk_total:.0f}G",
                 self.pal.fg)
        # R/W column with tiny direction arrows
        rx = x0 + lw + 24
        rd, ru = human_rate(v.disk_rd)
        wr, wu = human_rate(v.disk_wr)
        ry = py + 4
        d.text((rx, ry), "▼", font=self.f_small, fill=self.c_info)
        d.text((rx + 16, ry), f"R {rd} {ru}", font=self.f_small, fill=self.pal.fg)
        ry += self.f_small.size * 1.8
        d.text((rx, ry), "▲", font=self.f_small, fill=self.c_ok)
        d.text((rx + 16, ry), f"W {wr} {wu}", font=self.f_small, fill=self.pal.fg)
        by = y1 - 34
        self.seg_bar(d, px, by, x1 - 14 - px, 9,
                     self.ease("ssd", v.disk_pct / 100), 22,
                     self.c_main, scale(self.c_main, 0.18))
        d.text((px, by + 15), "CAPACITY", font=self.f_tiny, fill=self.pal.dim)

    def _m_net(self, d, r, v) -> None:
        x0, y0, x1, y1 = r
        px, py = x0 + 14, y0 + 16
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        d.text((px, py), f"▼ {dv}", font=self.f_mid, fill=self.c_info)
        d.text((px + d.textlength(f"▼ {dv}", font=self.f_mid) + 5,
                py + self.f_mid.size - self.f_small.size), du,
               font=self.f_small, fill=self.pal.dim)
        ux = x0 + (x1 - x0) * 0.52
        d.text((ux, py), f"▲ {uv}", font=self.f_mid, fill=self.c_ok)
        d.text((ux + d.textlength(f"▲ {uv}", font=self.f_mid) + 5,
                py + self.f_mid.size - self.f_small.size), uu,
               font=self.f_small, fill=self.pal.dim)
        gy = py + self.f_mid.size * 1.5
        gh = y1 - 34 - gy
        # both directions share one scale so the lines are comparable
        dn = self.hist("netd", v.net_down, n=40)
        up = self.hist("netu", v.net_up, n=40)
        mx = max(max(dn), max(up), 10 * 1024)
        self._graph(d, px, gy, x1 - 16 - px, gh,
                    [x / mx for x in dn], self.c_info,
                    second=[x / mx for x in up], sec_col=self.c_ok)
        d.text((px, y1 - 26), f"ΣRX {v.net_rx_total:.1f}G  ΣTX "
                              f"{v.net_tx_total:.1f}G  ·  40 SAMPLES",
               font=self.f_tiny, fill=self.pal.dim)

    def _m_status(self, d, r, v) -> None:
        x0, y0, x1, y1 = r
        therm = max(v.temp or 0.0, v.gpu.get("temp", 0.0) if v.gpu else 0.0,
                    v.nvme_temp or 0.0) / 95.0
        rows = [("CPU", v.load / 100),
                ("GPU", v.gpu["util"] / 100 if v.gpu else None),
                ("MEM", v.mem_pct / 100),
                ("SSD", v.disk_pct / 100),
                ("NET", min(1.0, (v.net_down + v.net_up) / (100 * 2 ** 20))),
                ("THERMAL", therm)]
        pitch = (y1 - y0 - 24) / len(rows)
        on = (self.t * self.BLINK_HZ) % 1.0 < 0.62   # wall-clock blink phase
        yy = y0 + 14
        for i, (label, frac) in enumerate(rows):
            col, word, blink = self._state(frac)
            cy = yy + pitch / 2
            led = col if (not blink or on) else scale(col, 0.22)
            d.ellipse([x0 + 12, cy - 5, x0 + 22, cy + 5], fill=led,
                      outline=scale(col, 0.65))
            d.text((x0 + 32, cy - self.f_small.size * 0.62), label,
                   font=self.f_small, fill=self.pal.dim)
            pct = f"{frac * 100:3.0f}" if frac is not None else "---"
            d.text((x0 + (x1 - x0) * 0.52, cy - self.f_small.size * 0.62), pct,
                   font=self.f_small, fill=self.pal.fg)
            d.text((x1 - 12 - d.textlength(word, font=self.f_small),
                    cy - self.f_small.size * 0.62), word,
                   font=self.f_small, fill=col)
            if i:
                d.line([(x0 + 10, yy), (x1 - 10, yy)],
                       fill=mix(self.panel_col, self.pal.dim, 0.25))
            yy += pitch

    # --- ticker ---------------------------------------------------------------
    def _ticker_text(self, v) -> str:
        dv, du = human_rate(v.net_down)
        uv, uu = human_rate(v.net_up)
        rd, ru = human_rate(v.disk_rd)
        wr, wu = human_rate(v.disk_wr)
        worst = max(v.load / 100, v.mem_pct / 100, v.disk_pct / 100,
                    (v.gpu["util"] / 100) if v.gpu else 0.0)
        _, word, _ = self._state(worst)
        gpu = (f"GPU {v.gpu['util']:.0f}% {v.gpu['temp']:.0f}°C" if v.gpu
               else "GPU OFFLINE")
        parts = [
            f"T+{self._met(v.uptime)}",
            f"CPU {v.load:.0f}%" + (f" {v.temp:.0f}°C" if v.temp else ""),
            gpu,
            f"MEM {v.mem_used:.1f}/{v.mem_total:.0f}G",
            f"SSD {v.disk_pct:.0f}%",
            f"NET ▼{dv}{du} ▲{uv}{uu}",
            f"IO R {rd}{ru} W {wr}{wu}",
            f"PROCS {v.procs}",
            f"{self.host} {word}",
        ]
        return "  ·  ".join(parts) + "  ·  "

    def _draw_ticker(self, img, v) -> None:
        x0, y0, x1, y1 = self.ticker_r
        serial = sample_serial("vitals")
        if serial != self._tick_serial:        # text steps on the sample clock
            self._tick_serial = serial
            self._tick_text = self._ticker_text(v)
            self._tick_w = max(60, int(self.f_tick.getlength(self._tick_text)))
        # drawn on a strip so the marquee clips at the frame instead of
        # spilling into the margins (PIL has no clip rect)
        iw, ih = x1 - x0 - 2, y1 - y0 - 9
        strip = Image.new("RGB", (iw, ih), self.panel_col)
        sd = ImageDraw.Draw(strip)
        span = self._tick_w
        x = -int((self.t * self.TICKER_PPS) % span)
        ty = (ih - self.f_tick.size) // 2 - 1
        while x < iw:
            sd.text((x, ty), self._tick_text, font=self.f_tick, fill=self.c_main)
            x += span
        img.paste(strip, (x0 + 1, y0 + 8))

    # --- frame ----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        img = self._bg.copy()
        d = ImageDraw.Draw(img)
        v = vitals()
        for name, r in self.mods:
            getattr(self, "_m_" + name)(d, r, v)
        self._draw_ticker(img, v)
        self._scanline(img, colour=scale(self.c_main, 0.55))
        return img.convert("RGB")


VIEWS = {"mission-control": MissionControlView}
