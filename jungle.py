#!/usr/bin/env python3
"""
Driver for the Jungle Leopard / HONGTAI 9.16" secondary LCD (USB 33c3:7788).

The panel is an RT-Thread MCU board exposing a USB-CDC virtual serial port
(/dev/ttyACM*). Protocol confirmed by newan0805/loving_cool_aio_display_manage,
extracted from the vendor Electron app (main/_baseClass/device.js).

Two channels share the one serial link:

  Command channel — framed, little-endian:
      [0x55 0xAA][len:u16][key:u8][payload...][checksum:u16]
      len      = len(payload) + 7  (the whole frame)
      checksum = sum of every preceding byte, & 0xFFFF
      Responses use identical framing; bytes[5:-2] is a UTF-8 JSON blob.

  Image channel — NOT framed. After a one-time 0x11 ("commit"/start-live),
      raw JPEG bytes are written straight to the port. Firmware whose version
      string coerces to a number > 2.8 instead expects
      [len:u32 LE][jpeg][checksum:u16 LE].

Link parameters that matter: 2,000,000 baud, DTR asserted, and a single bulk
read for responses (see _read_response).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

try:
    import serial
    from serial.tools import list_ports
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover
    sys.exit(f"missing dependency: {exc}\nrun:  screens/.venv/bin/python {sys.argv[0]}")

VID, PID = 0x33C3, 0x7788
BAUD = 2_000_000

# --- command opcodes ---------------------------------------------------------
CMD_RESTART = 0x01
CMD_SET_BRIGHTNESS = 0x03
CMD_GET_INFO = 0x06
CMD_COMMIT = 0x11          # start-live; must precede the first image frame
CMD_MOTION_BEFORE_OFF = 0x14
CMD_MOTION_TIMEOUT = 0x15
CMD_CLOSE = 0x21
CMD_SET_SERIAL = 0x23
CMD_SET_MOTOR = 0x25
CMD_REALTIME_TIMEOUT = 0x26

DEFAULT_MAX_JPEG_KB = 50


def js_number(s: str) -> float | None:
    """Mimic JavaScript's Number(s) coercion.

    The vendor app gates the framed-image format on `version > 2.8` where
    `version` is the raw string from getInfo. JS coerces the *whole* string —
    "Ver1.0" becomes NaN (so the gate is always false), while "4.1" becomes
    4.1. Parsing out digits with a regex instead, as some ports do, would flip
    the behaviour on a "Ver4.1" panel and desync from what the firmware was
    actually shipped against. So: whole-string parse, or None.
    """
    try:
        return float(s.strip())
    except (ValueError, AttributeError):
        return None


def find_port() -> str | None:
    for p in list_ports.comports():
        if p.vid == VID and p.pid == PID:
            return p.device
    for path in ("/dev/jungleleopard", "/dev/ttyACM0"):
        if os.path.exists(path):
            return path
    return None


class JungleLeopard:
    def __init__(self, port: str | None = None, verbose: bool = False,
                 force_wrapper: bool | None = None):
        self.verbose = verbose
        self.port = port or find_port()
        if not self.port:
            raise SystemExit(
                f"Jungle Leopard panel ({VID:04x}:{PID:04x}) not found.\n"
                "Plugged in? Check:  lsusb -d 33c3:7788")
        try:
            # dsrdtr=True: at 2 Mbaud the link drops bytes without DTR-based
            # flow control; the MCU also gates its TX side on DTR.
            self.ser = serial.Serial(self.port, baudrate=BAUD, timeout=0.1, dsrdtr=True)
        except (OSError, serial.SerialException) as e:
            raise SystemExit(
                f"cannot open {self.port}: {e}\n"
                "Permission denied? Run screens/setup-root.sh once (udev rules + dialout).\n"
                "Device busy? ModemManager may have grabbed it — the udev rule sets "
                "ID_MM_DEVICE_IGNORE to stop that.") from e
        self.ser.dtr = True
        time.sleep(0.2)          # let the MCU's USB-CDC stack settle after DTR
        self.ser.reset_input_buffer()

        self.width = 1920
        self.height = 480
        self.info: dict = {}
        self.max_jpeg_kb = DEFAULT_MAX_JPEG_KB
        self._live = False
        self._force_wrapper = force_wrapper
        self._use_wrapper = False

    def _log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr)

    # --- command channel ---
    @staticmethod
    def build_frame(key: int, payload: bytes = b"") -> bytes:
        total = len(payload) + 7
        frame = bytes([0x55, 0xAA, total & 0xFF, (total >> 8) & 0xFF, key]) + payload
        checksum = sum(frame) & 0xFFFF
        return frame + bytes([checksum & 0xFF, (checksum >> 8) & 0xFF])

    def _read_response(self, timeout_ms: int = 3000) -> bytes | None:
        """One bulk read, not a header-then-body pair.

        At 2 Mbaud, doing read(5) for the header and then read(declared_len)
        loses bytes between the two syscalls, and the device's declared length
        does not always match what it actually transmits. Sleeping briefly and
        taking whatever arrived in a single read is reliable; the length field
        is only a sanity check afterwards.
        """
        self.ser.timeout = timeout_ms / 1000.0
        time.sleep(0.15)
        buf = self.ser.read(4096)
        if len(buf) < 7 or buf[0] != 0x55 or buf[1] != 0xAA:
            self._log(f"  bad/short response: {buf[:32].hex(' ') if buf else '(none)'}")
            return None
        return buf

    def command(self, key: int, payload: bytes = b"",
                expect_response: bool = True, timeout_ms: int = 3000) -> dict | None:
        frame = self.build_frame(key, payload)
        self._log(f"  TX 0x{key:02x}: {frame.hex(' ')}")
        self.ser.write(frame)
        self.ser.flush()
        if not expect_response:
            # The panel still ACKs commands we don't read. Let the reply land,
            # then drop it — otherwise it sits in the buffer and gets parsed as
            # the response to whatever command comes next.
            time.sleep(0.15)
            self.ser.reset_input_buffer()
            return None
        raw = self._read_response(timeout_ms)
        if raw is None:
            return None
        body = raw[5:-2]
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"raw": body.hex()}

    def refresh_info(self) -> dict:
        resp = self.command(CMD_GET_INFO)
        data = (resp or {}).get("data", {}) if resp else {}
        if data:
            self.info = data
            self.width = int(data.get("width", self.width))
            self.height = int(data.get("height", self.height))
            ver = js_number(str(data.get("version", "")))
            detected = ver is not None and ver > 2.8
            self._use_wrapper = detected if self._force_wrapper is None else self._force_wrapper
            self._log(f"  panel {self.width}x{self.height}, version "
                      f"{data.get('version')!r}, frame-wrapper={self._use_wrapper}")
        return data

    def set_brightness(self, percent: int) -> None:
        self.command(CMD_SET_BRIGHTNESS, bytes([max(0, min(100, percent))]),
                     expect_response=False)

    def restart(self) -> None:
        self.command(CMD_RESTART, expect_response=False)

    # --- image channel ---
    def _ensure_live(self) -> None:
        if not self._live:
            self.command(CMD_COMMIT, expect_response=False)
            self._live = True

    def _wrap(self, jpeg: bytes) -> bytes:
        if not self._use_wrapper:
            return jpeg
        body = len(jpeg).to_bytes(4, "little") + jpeg
        checksum = sum(body) & 0xFFFF
        return body + checksum.to_bytes(2, "little")

    def send_jpeg(self, jpeg: bytes) -> None:
        self._ensure_live()
        self.ser.write(self._wrap(jpeg))
        self.ser.flush()

    def send_image(self, img: Image.Image, rotate: int = 0) -> None:
        self.send_jpeg(encode_jpeg(img, self.width, self.height,
                                   self.max_jpeg_kb, rotate))

    def stop_live(self) -> None:
        if self._use_wrapper:
            # Vendor app resets the stream with a double JPEG-EOI sentinel.
            self.ser.write(bytes([0xFF, 0xD9, 0xFF, 0xD9]))
            self.ser.flush()
        self._live = False

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


def encode_jpeg(img: Image.Image, width: int, height: int,
                max_kb: float = DEFAULT_MAX_JPEG_KB, rotate: int = 0) -> bytes:
    """Fit to the panel, then step quality down by 2 until under the size cap."""
    if rotate:
        img = img.rotate(rotate, expand=True)
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.size != (width, height):
        img = fit_cover(img, width, height)

    cap = int(max_kb * 1024)
    quality = 100
    while quality >= 10:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= cap:
            return data
        quality -= 2
    raise ValueError(f"cannot encode a frame under {max_kb} KB")


def fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    resized = img.resize((max(1, round(img.width * scale)),
                          max(1, round(img.height * scale))), Image.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def load_font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_text(text: str, w: int, h: int, bg=(0, 0, 0), fg=(255, 255, 255)) -> Image.Image:
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    lines = text.split("\n")
    longest = max(len(l) for l in lines) or 1
    size = max(12, min(h // max(1, len(lines)) - 8, int(w / longest * 1.7)))
    font = load_font(size)
    heights = [draw.textbbox((0, 0), l, font=font)[3] for l in lines]
    y = (h - sum(heights)) // 2
    for line, lh in zip(lines, heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text(((w - bbox[2]) // 2, y), line, font=font, fill=fg)
        y += lh
    return img


def main() -> int:
    p = argparse.ArgumentParser(
        description='Control the Jungle Leopard / HONGTAI 9.16" LCD (33c3:7788)')
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--max-kb", type=float, default=DEFAULT_MAX_JPEG_KB,
                   help=f"JPEG size cap in KB (default {DEFAULT_MAX_JPEG_KB})")
    wrap = p.add_mutually_exclusive_group()
    wrap.add_argument("--wrapper", dest="wrapper", action="store_true", default=None,
                      help="force the [len][jpeg][crc] framed image format")
    wrap.add_argument("--no-wrapper", dest="wrapper", action="store_false",
                      help="force bare JPEG frames")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="query panel size, firmware, storage")

    p_img = sub.add_parser("image", help="display a still image")
    p_img.add_argument("path")
    p_img.add_argument("--rotate", type=int, default=0)

    p_txt = sub.add_parser("text", help="display text")
    p_txt.add_argument("text")

    p_br = sub.add_parser("brightness", help="set backlight 0-100")
    p_br.add_argument("value", type=int)

    sub.add_parser("restart", help="reboot the panel MCU")
    sub.add_parser("stop", help="end the live image session")

    args = p.parse_args()

    dev = JungleLeopard(port=args.port, verbose=args.verbose, force_wrapper=args.wrapper)
    dev.max_jpeg_kb = args.max_kb
    try:
        if args.cmd == "info":
            data = dev.refresh_info()
            if not data:
                print(f"no reply from {dev.port} — the panel did not answer getInfo (0x06)")
                return 1
            print(f'Jungle Leopard / HONGTAI  ({VID:04x}:{PID:04x})  on {dev.port}')
            for k in ("model", "version", "width", "height", "brightness",
                      "diplay_on", "uid", "region", "shape"):
                if k in data:
                    print(f"  {k:<12}: {data[k]}")
            extra = {k: v for k, v in data.items()
                     if k.startswith(("i_", "e_"))}
            if extra:
                print(f"  storage     : {json.dumps(extra)}")
        else:
            dev.refresh_info()   # needed for correct panel size + wrapper mode
            if args.cmd == "image":
                dev.send_image(Image.open(args.path), rotate=args.rotate)
                print(f"sent {args.path} ({dev.width}x{dev.height})")
            elif args.cmd == "text":
                dev.send_image(render_text(args.text, dev.width, dev.height))
                print("sent text")
            elif args.cmd == "brightness":
                dev.set_brightness(args.value)
                print(f"brightness -> {args.value}")
            elif args.cmd == "restart":
                dev.restart()
                print("restart sent")
            elif args.cmd == "stop":
                dev.stop_live()
                print("live session stopped")
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
