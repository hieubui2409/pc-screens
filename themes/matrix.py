"""matrix — phosphor-terminal theme (vertical).

Falling glyph rain behind translucent console panels: glowing clock, a
terminal log with dot leaders that "decodes" on every vitals sample, a
per-core hex readout and block-bar traces. Signature colours are phosphor
greens; --palette recolours everything including the rain.

Rain columns advance on wall-clock time; glyphs are pinned to screen cells
and re-randomise per column on a slow window, so the field shimmers without
strobing. Rain density rides CPU load.
"""

from __future__ import annotations

import os
import random
import time

from PIL import Image, ImageDraw, ImageFilter

from fx import (FXBase, Palette, dens, hz, lighten, load_mono, mix,
                sample_serial, scale, vitals)

MATRIX = Palette("matrix",
                 ((51, 255, 119), (186, 255, 150), (34, 204, 102), (150, 255, 200)),
                 bg=(2, 6, 4), grid=(8, 26, 14),
                 fg=(222, 255, 232), dim=(88, 156, 110))

# Halfwidth katakana needs a CJK face — DejaVu renders tofu for it.
RAIN_FONTS = (
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/komatuna/komatuna.ttf",
    "/usr/share/fonts/truetype/seto/setofont.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
KATA = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉ0123456789"
ASCII = "0123456789ABCDEF+*=<>#$%&"
SCRAMBLE = "#$%&@*+=?0123456789ABCDEF"


def _kmg(bps: float) -> str:
    for u, dv in (("G", 2 ** 30), ("M", 2 ** 20), ("K", 2 ** 10)):
        if bps >= dv:
            return f"{bps / dv:.1f}{u}"
    return f"{bps:.0f}B"


class MatrixView(FXBase):
    HZ_HEAD = 3.2        # head-glyph flicker rate
    HZ_CURSOR = 1.6      # block-cursor blink rate
    DECODE_S = 0.45      # log-line decode duration
    N_FRONT = 12         # rain columns, front layer
    N_BACK = 10          # rain columns, depth layer

    def __init__(self, w: int, h: int, palette: Palette | None = None,
                 opts: dict | None = None):
        self.w, self.h = w, h
        self.pal = palette or MATRIX
        self._start(0x3A791)
        self.host = os.uname().nodename[:16].lower()
        self.osid = f"{os.uname().sysname.lower()} {os.uname().release.split('-')[0]}"
        self.tall = h / max(1, w) >= 2.6

        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        self.mx = w * 0.045
        self.px = w * 0.035
        inner = w - (self.mx + self.px) * 2

        size = int(min(w * 0.34, h * 0.15))
        while size > 14 and probe.textlength("00:00", font=load_mono(size)) > inner:
            size = int(size * 0.93)
        self.f_time = load_mono(size)
        # log line is ~36 monospace cells; the font is whatever fits them
        ls = int(w * 0.055)
        while ls > 9 and probe.textlength("0" * 36, font=load_mono(ls)) > inner:
            ls = int(ls * 0.94)
        self.f_log = load_mono(ls)
        self.f_small = load_mono(max(8, int(ls * 0.80)))
        self.f_label = load_mono(max(9, int(ls * 0.92)))

        # --- rain field: glyph masks pre-rendered, columns fixed at init ---
        path = next((p for p in RAIN_FONTS if os.path.exists(p)), None)
        self._layers = []
        rnd = random.Random(0xA11CE)
        for n, sf, x_off in ((self.N_BACK, 0.52, 0.5), (self.N_FRONT, 0.80, 0.0)):
            colw = w / n
            gs = max(10, int(colw * sf))
            if path:
                from PIL import ImageFont
                font, glyphs = ImageFont.truetype(path, gs), KATA
            else:
                font, glyphs = load_mono(gs), ASCII
            tiles = []
            tw, th = int(gs * 0.95) + 2, int(gs * 1.30) + 2
            for ch in glyphs:
                im = Image.new("L", (tw, th), 0)
                ImageDraw.Draw(im).text((1, 0), ch, font=font, fill=255)
                tiles.append(im)
            cols = []
            for i in range(n):
                cols.append((colw * (i + x_off) + (colw - tw) / 2,
                             rnd.uniform(1.8, 5.0) if sf < 0.7 else rnd.uniform(3.0, 8.5),
                             rnd.randint(9, 20) if sf < 0.7 else rnd.randint(12, 26),
                             rnd.uniform(0, 900),
                             0.45 + 0.13 * (i % 5)))
            order = list(range(n))
            rnd.shuffle(order)
            self._layers.append({"tiles": tiles, "step": gs * 1.08, "cols": cols,
                                 "order": order, "back": sf < 0.7})
        self._gtab = [rnd.randrange(len(KATA)) for _ in range(512)]

        # prime the held sample, then latch its serial, so the first frame
        # is not caught mid-decode
        vitals()
        self._dec_serial = sample_serial("vitals")
        self._dec_t = -9.0
        self._dec_line = 0
        self._bg = self._build_bg()

    # --- static layer ---------------------------------------------------------
    def _build_bg(self) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), self.pal.bg)
        d = ImageDraw.Draw(img)
        dark = scale(self.pal.bg, 0.35)
        for y in range(0, self.h, 3):            # CRT row texture, every 3rd line
            d.line([(0, y), (self.w, y)], fill=dark, width=1)
        rail = scale(self.pal.base("cpu"), 0.30)
        m = max(2, int(self.w * 0.008))
        d.line([(m, 0), (m, self.h)], fill=rail, width=1)
        d.line([(self.w - m, 0), (self.w - m, self.h)], fill=rail, width=1)
        return img

    # --- rain -----------------------------------------------------------------
    def _rain(self, img: Image.Image, load_frac: float) -> None:
        d = ImageDraw.Draw(img)
        el = self.ease("rain", load_frac)
        heads_hot = lighten(self.pal.base("cpu"), 0.72)
        head_step = int(self.t * hz(self.HZ_HEAD))
        gt, ng = self._gtab, len(self._gtab)
        for layer in self._layers:
            base = self.pal.base("cpu")
            if layer["back"]:
                base = scale(base, 0.55)
            # 20-entry fade ramp rebuilt per frame so --palette recolours it
            ramp = [mix(self.pal.bg, base, 0.12 + 0.88 * (j / 19) ** 1.35)
                    for j in range(20)]
            head_col = heads_hot if not layer["back"] else lighten(base, 0.35)
            tiles, step, cols = layer["tiles"], layer["step"], layer["cols"]
            rows = int(self.h / step) + 1
            nmax = len(cols)
            nact = max(6, min(nmax, dens(nmax * (0.68 + 0.42 * el))))
            for i in layer["order"][:nact]:
                x, spd, tail, phase, grate = cols[i]
                span = rows + tail
                head = int(self.t * hz(spd) + phase) % span
                win = self.cycle(grate, salt=0x51 + i * 17)
                for k in range(tail):
                    r = head - k
                    if r < 0 or r >= rows:
                        continue
                    if k == 0:
                        idx = gt[(i * 31 + head_step) % ng]
                        col = head_col
                    else:
                        idx = gt[(i * 7919 + r * 104729 + win * 97) % ng]
                        col = ramp[int((1 - k / tail) * 19)]
                    d.bitmap((x, r * step), tiles[idx % len(tiles)], fill=col)

    # --- widgets --------------------------------------------------------------
    def _panel(self, img, y0: float, y1: float, base):
        """Backing box that dims the rain so the readouts stay readable."""
        d = ImageDraw.Draw(img, "RGBA")
        x0, x1 = self.mx, self.w - self.mx
        d.rectangle([x0, y0, x1, y1], fill=tuple(self.pal.bg) + (180,))
        d.rectangle([x0, y0, x1, y1], outline=tuple(scale(base, 0.42)) + (255,))
        tick = self.w * 0.035
        b = tuple(base) + (255,)
        for cx, sx in ((x0, 1), (x1, -1)):
            for cy, sy in ((y0, 1), (y1, -1)):
                d.line([(cx, cy), (cx + tick * sx, cy)], fill=b, width=2)
                d.line([(cx, cy), (cx, cy + tick * sy)], fill=b, width=2)
        return ImageDraw.Draw(img)

    def _log_line(self, d, x, y, name, value, key, decoding: bool):
        prefix = "> " + name + " " + "." * (8 - len(name)) + " "
        d.text((x, y), prefix, font=self.f_log, fill=self.pal.dim)
        vx = x + d.textlength(prefix, font=self.f_log)
        hot = self.col(key, True)
        if decoding:
            prog = (self.t - self._dec_t) / self.DECODE_S
            n_ok = int(len(value) * max(0.0, min(1.0, prog)))
            r = random.Random(int(self.t * hz(25)) * 2654435761 ^ 0xDEC)
            scram = "".join(r.choice(SCRAMBLE) for _ in value[n_ok:])
            d.text((vx, y), value[:n_ok], font=self.f_log, fill=hot)
            d.text((vx + d.textlength(value[:n_ok], font=self.f_log), y),
                   scram, font=self.f_log, fill=self.col(key))
        else:
            d.text((vx, y), value, font=self.f_log, fill=hot)

    def _trace(self, img, d, x, y, gw, gh, label, key, frac, sub):
        base, hot = self.col(key), self.col(key, True)
        d.text((x, y), "> " + label, font=self.f_small, fill=base)
        d.text((x + gw - d.textlength(sub, font=self.f_small), y),
               sub, font=self.f_small, fill=self.pal.dim)
        ay = y + self.f_small.size * 1.35
        ah = gh - self.f_small.size * 1.35 - 4
        if ah < 8:
            return
        dq = self.hist(f"mx:{key}", frac, n=44)
        bw = gw / len(dq)
        dark = scale(base, 0.20)
        for i, v in enumerate(dq):
            v = max(0.04, min(1.0, v)) ** 0.72   # low loads still read as bars
            col = hot if i == len(dq) - 1 else mix(dark, base, 0.20 + 0.80 * v)
            d.rectangle([x + i * bw, ay + ah * (1 - v),
                         x + i * bw + bw * 0.72, ay + ah], fill=col)
        # slice the bars into cells — LED-column look, one pass of lines
        da = ImageDraw.Draw(img, "RGBA")
        yy = ay + ah
        cell = max(5, ah / 14)
        while yy > ay:
            da.line([(x, yy), (x + gw, yy)], fill=(0, 0, 0, 150), width=2)
            yy -= cell
        d.line([(x, ay + ah), (x + gw, ay + ah)], fill=scale(base, 0.5), width=1)

    # --- frame ----------------------------------------------------------------
    def render(self) -> Image.Image:
        self._tick()
        w, h = self.w, self.h
        img = self._bg.copy().convert("RGBA")
        v = vitals()
        gpu = v.gpu
        now = time.localtime()

        self._rain(img, v.load / 100)

        gr, grh = self.col("cpu"), self.col("cpu", True)
        px = self.mx + self.px
        inner = w - px * 2
        lh = self.f_log.size * (1.72 if self.tall else 1.42)
        sh = self.f_small.size

        # decode trigger: one log line resolves whenever the sample changes
        serial = sample_serial("vitals")
        if serial != self._dec_serial:
            self._dec_serial = serial
            self._dec_t = self.t
            self._dec_line = (serial * 5 + 3) % 7

        # ---- layout: panel heights first, leftover becomes rain bands ----
        track_h = max(4, int(h * 0.005))
        p1_h = (self.px + sh * 2.1 + self.f_time.size * 1.10
                + track_h + sh * 1.9 + self.px)
        n_lines = 10 if self.tall else 7
        p2_h = self.px * 2 + n_lines * lh
        cores = v.per_core or [0.0]
        per_row = len(cores) if len(cores) <= 20 else -(-len(cores) // 2)
        hs = max(10, min(int(inner / (per_row * 0.75)), int(w * 0.075)))
        rows_n = -(-len(cores) // per_row)
        p3_h = self.px * 2 + self.f_label.size * 1.6 + rows_n * hs * 1.15
        n_metrics = 4 if gpu else 3
        gap_min = h * 0.012
        min_g = sh * 1.35 + 30
        cap_g = sh * 1.35 + h * 0.048
        p4_avail = h - 2 * self.mx - p1_h - p2_h - p3_h - 4 * gap_min - self.px * 2
        n_g = max(0, min(n_metrics, int(p4_avail // min_g)))
        g_h = min(cap_g, p4_avail / n_g) if n_g else 0.0
        p4_h = self.px * 2 + g_h * n_g if n_g else 0.0
        # panels are fixed-height; whatever is left over opens as rain bands
        gap = max(gap_min,
                  (h - 2 * self.mx - p1_h - p2_h - p3_h - p4_h) / 4)

        # ---- panel 1: header + clock + seconds track ----
        y0 = self.mx
        d = self._panel(img, y0, y0 + p1_h, gr)
        y = y0 + self.px
        cursor = "█" if self.cycle(self.HZ_CURSOR) % 2 == 0 else ""
        d.text((px, y), f"> {self.host}{cursor}", font=self.f_small, fill=grh)
        date = time.strftime("%a %d/%m/%Y", now).lower()
        d.text((w - px - d.textlength(date, font=self.f_small), y),
               date, font=self.f_small, fill=self.pal.dim)
        y += sh * 1.6
        d.line([(px, y), (w - px, y)], fill=scale(gr, 0.45), width=1)
        y += sh * 0.5

        hhmm = time.strftime("%H:%M", now)
        tw = d.textlength(hhmm, font=self.f_time)
        tx = (w - tw) / 2
        # glow blurred on a clock-sized strip, not the full canvas
        strip = Image.new("RGBA", (int(tw) + 48, int(self.f_time.size * 1.45)),
                          (0, 0, 0, 0))
        ImageDraw.Draw(strip).text((24, 20), hhmm, font=self.f_time,
                                   fill=tuple(gr) + (215,))
        strip = strip.filter(ImageFilter.GaussianBlur(max(2, self.f_time.size * 0.07)))
        img.alpha_composite(strip, (int(tx) - 24, int(y) - 20))
        d = ImageDraw.Draw(img)
        d.text((tx, y), hhmm, font=self.f_time, fill=self.pal.fg)
        y += self.f_time.size * 1.14

        # fractional seconds: continuous phosphor track — it is the clock,
        # not a sampled reading, so it may glide every frame
        sec = time.time() % 60.0
        d.rectangle([px, y, px + inner, y + track_h], fill=scale(gr, 0.18))
        fw = inner * sec / 60.0
        d.rectangle([px, y, px + fw, y + track_h], fill=gr)
        d.rectangle([px + fw - 2, y - 1, px + fw + 2, y + track_h + 1], fill=grh)
        for i in range(7):
            tx2 = px + inner * i / 6
            d.line([(tx2, y + track_h + 2), (tx2, y + track_h + 5)],
                   fill=scale(self.pal.dim, 0.6), width=1)
        y += track_h + sh * 0.75
        d.text((px, y), f"{int(sec):02d}s", font=self.f_small, fill=gr)
        ep = f"e{int(time.time())}"
        d.text((w - px - d.textlength(ep, font=self.f_small), y),
               ep, font=self.f_small, fill=self.pal.dim)

        # ---- panel 2: terminal log ----
        up = v.uptime
        rd, wr = _kmg(v.disk_rd), _kmg(v.disk_wr)
        lines = [
            ("cpu", f"{v.load:.0f}% "
                    + (f"{v.temp:.0f}°C " if v.temp else "")
                    + f"{v.freq_ghz:.1f}GHz", "cpu"),
            ("gpu", (f"{gpu['util']:.0f}% {gpu['temp']:.0f}°C {gpu['power']:.0f}W"
                     if gpu else "offline"), "gpu"),
            ("mem", f"{v.mem_used:.1f}G/{v.mem_total:.0f}G {v.mem_pct:.0f}%", "ram"),
            ("ssd", f"{v.disk_pct:.0f}% "
                    + (f"{v.nvme_temp:.0f}°C " if v.nvme_temp else "")
                    + f"R {rd} W {wr}", "ssd"),
            ("net", f"▼{_kmg(v.net_down)} ▲{_kmg(v.net_up)}", "cpu"),
            ("up", f"{up // 86400}d {up % 86400 // 3600:02d}h "
                   f"{up % 3600 // 60:02d}m", "ram"),
            ("procs", f"{v.procs}", "gpu"),
        ]
        if self.tall:
            l1, l5, l15 = os.getloadavg()
            lines += [
                ("ldavg", f"{l1:.2f} {l5:.2f} {l15:.2f}", "cpu"),
                ("rxtx", f"▼{v.net_rx_total:.1f}G ▲{v.net_tx_total:.1f}G", "ssd"),
                ("os", self.osid, "ram"),
            ]
        y0 = self.mx + p1_h + gap
        d = self._panel(img, y0, y0 + p2_h, self.col("ram"))
        decoding = self.t - self._dec_t < self.DECODE_S
        for i, (name, val, key) in enumerate(lines):
            self._log_line(d, px, y0 + self.px + i * lh, name, val, key,
                           decoding and i == self._dec_line)

        # ---- panel 3: per-core hex readout ----
        f_hex = load_mono(hs)
        y0 = y0 + p2_h + gap
        base = self.col("gpu")
        d = self._panel(img, y0, y0 + p3_h, base)
        y = y0 + self.px
        d.text((px, y), f"> cores [{len(cores)}]", font=self.f_label, fill=base)
        ghz = f"{v.freq_ghz:.1f}GHz"
        d.text((w - px - d.textlength(ghz, font=self.f_label), y),
               ghz, font=self.f_label, fill=self.pal.dim)
        y += self.f_label.size * 1.6
        adv = inner / per_row
        dark = scale(base, 0.32)
        for i, load in enumerate(cores):
            f = max(0.0, min(1.0, load / 100))
            ch = "0123456789ABCDEF"[min(15, int(f * 15.999))]
            cx = px + (i % per_row) * adv
            cy = y + (i // per_row) * hs * 1.15
            d.text((cx, cy), ch, font=f_hex,
                   fill=mix(dark, lighten(base, 0.55), f ** 0.8))

        # ---- panel 4: history traces (capped, rest of the height is rain) ----
        if n_g:
            y0 = y0 + p3_h + gap
            metrics = [("cpu", "cpu", v.load / 100,
                        f"{v.load:.0f}%" + (f" {v.temp:.0f}°C" if v.temp else ""))]
            if gpu:
                metrics.append(("gpu", "gpu", gpu["util"] / 100,
                                f"{gpu['util']:.0f}% {gpu['temp']:.0f}°C"))
            metrics += [("mem", "ram", v.mem_pct / 100, f"{v.mem_used:.1f}G"),
                        ("ssd", "ssd", v.disk_pct / 100, f"{v.disk_pct:.0f}%")]
            d = self._panel(img, y0, y0 + p4_h, self.col("ssd"))
            for i, (label, key, frac, sub) in enumerate(metrics[:n_g]):
                self._trace(img, d, px, y0 + self.px + i * g_h, inner,
                            g_h - sh * 0.5, label, key, frac, sub)

        self._scanline(img, colour=gr)
        return img.convert("RGB")


VIEWS = {"matrix": MatrixView}
