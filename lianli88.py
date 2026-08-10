#!/usr/bin/env python3
"""
Driver for the Lian Li 8.8" Universal Screen (USB 1cbe:a088) on Linux.

Protocol ported from sgtaziz/lian-li-linux (crates/lianli-devices/src/crypto.rs
and .../winusb/lcd/core.rs), which reverse-engineered L-Connect 3.

Wire format
-----------
Every command is a 512-byte header, optionally followed by a raw payload
(JPEG/PNG/H.264) on bulk endpoint 0x01. The header is built as:

    plaintext[500]:
        [0]     command opcode
        [1]     0
        [2..4]  0x1A 0x6D            magic
        [4..8]  timestamp, LE u32    ms since session start, strictly increasing
        [8..]   command parameters
    ciphertext = DES-CBC(key=iv=b"slv3tuzx", PKCS7)(plaintext)   -> 504 bytes
    header[512] = ciphertext || zeros || 0xA1 0x1A               (trailer at 510)

Responses come back on endpoint 0x81 in plaintext (not encrypted).

The ARGB frame around the panel is a *separate* USB device (0416:8050) and is
not handled here.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from dataclasses import dataclass

try:
    import usb.core
    import usb.util
    from Crypto.Cipher import DES
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover
    sys.exit(f"missing dependency: {exc}\nrun:  screens/.venv/bin/python {sys.argv[0]}")

VID, PID = 0x1CBE, 0xA088
EP_OUT, EP_IN = 0x01, 0x81
DES_KEY = b"slv3tuzx"

# --- opcodes (crypto.rs) -----------------------------------------------------
CMD_GET_VER = 0x0A
CMD_REBOOT = 0x0B
CMD_ROTATE = 0x0D
CMD_BRIGHTNESS = 0x0E
CMD_FRAME_RATE = 0x0F
CMD_GET_H264_BLOCK = 0x11
CMD_SET_CLOCK = 0x33
CMD_STOP_CLOCK = 0x34
CMD_PUSH_JPG = 0x65
CMD_PUSH_PNG = 0x66
CMD_CLEAR_PNG = 0x67
CMD_START_PLAY = 0x79
CMD_QUERY_BLOCK = 0x7A
CMD_STOP_PLAY = 0x7B
CMD_SWITCH_TO_DESKTOP = 0x96


@dataclass(frozen=True)
class ScreenInfo:
    """Panel parameters for the Universal Screen 8.8" (screen.rs)."""

    width: int = 480
    height: int = 1920
    max_fps: int = 120
    jpeg_quality: int = 95
    max_payload: int = 512_000


SCREEN = ScreenInfo()


class PacketBuilder:
    """Builds the DES-encrypted 512-byte command headers."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._last_ts = 0

    def _timestamp(self) -> int:
        raw = int((time.monotonic() - self._t0) * 1000) & 0xFFFFFFFF
        # The firmware rejects a non-increasing timestamp, so force monotonicity
        # when two commands land inside the same millisecond.
        ts = raw if raw > self._last_ts else self._last_ts + 1
        self._last_ts = ts
        return ts & 0xFFFFFFFF

    def build(self, command: int, params: bytes = b"") -> bytes:
        buf = bytearray(500)
        buf[0] = command
        buf[2] = 0x1A
        buf[3] = 0x6D
        buf[4:8] = self._timestamp().to_bytes(4, "little")
        params = params[:492]
        buf[8 : 8 + len(params)] = params

        # PKCS7 over 500 bytes adds a full-ish block -> 504
        pad = 8 - (len(buf) % 8) or 8
        padded = bytes(buf) + bytes([pad]) * pad
        cipher = DES.new(DES_KEY, DES.MODE_CBC, DES_KEY)
        encrypted = cipher.encrypt(padded)

        out = bytearray(512)
        out[: len(encrypted)] = encrypted
        out[510] = 0xA1
        out[511] = 0x1A
        return bytes(out)

    # --- typed helpers ---
    def jpeg(self, size: int) -> bytes:
        return self.build(CMD_PUSH_JPG, size.to_bytes(4, "big"))

    def png(self, size: int) -> bytes:
        return self.build(CMD_PUSH_PNG, size.to_bytes(4, "big"))

    def brightness(self, value: int) -> bytes:
        return self.build(CMD_BRIGHTNESS, bytes([min(value, 100)]))

    def rotation(self, value: int) -> bytes:
        return self.build(CMD_ROTATE, bytes([value & 0x03]))

    def frame_rate(self, fps: int) -> bytes:
        return self.build(CMD_FRAME_RATE, bytes([fps]))

    def get_ver(self) -> bytes:
        return self.build(CMD_GET_VER)

    def sync_clock(self, mode: int = 2) -> bytes:
        n = time.localtime()
        return self.build(
            CMD_SET_CLOCK,
            bytes([
                (n.tm_year >> 8) & 0xFF, n.tm_year & 0xFF,
                n.tm_mon, n.tm_mday, n.tm_hour, n.tm_min, n.tm_sec, mode,
            ]),
        )

    def stop_clock(self) -> bytes:
        return self.build(CMD_STOP_CLOCK, b"\x00")

    def clear_png(self) -> bytes:
        return self.build(CMD_CLEAR_PNG)

    def stop_play(self) -> bytes:
        return self.build(CMD_STOP_PLAY)

    def query_block(self) -> bytes:
        return self.build(CMD_QUERY_BLOCK)

    def switch_to_desktop(self) -> bytes:
        return self.build(CMD_SWITCH_TO_DESKTOP)

    def reboot(self) -> bytes:
        return self.build(CMD_REBOOT)

    def start_play(self, chunk_len: int, is_last: bool, play_count: int = 0,
                   play_tick: int = 0) -> bytes:
        params = (chunk_len.to_bytes(4, "big") + bytes([1 if is_last else 0, play_count])
                  + play_tick.to_bytes(4, "big"))
        return self.build(CMD_START_PLAY, params)


class UniversalScreen:
    WRITE_TIMEOUT = 2000
    READ_TIMEOUT = 200

    def __init__(self, screen: ScreenInfo = SCREEN, verbose: bool = False):
        self.screen = screen
        self.verbose = verbose
        self.pkt = PacketBuilder()
        self._initialized = False
        self.firmware: str | None = None

        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise SystemExit(
                f"Lian Li 8.8\" screen ({VID:04x}:{PID:04x}) not found.\n"
                "Is it plugged in? (it may be in desktop mode as 1a86:ace1/ad21)"
            )
        self.dev = dev
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            dev.set_configuration(1)
        except usb.core.USBError:
            pass  # already configured
        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as e:
            raise SystemExit(
                f"cannot claim USB interface: {e}\n"
                "Permission denied? Run screens/setup-root.sh once (installs udev rules)."
            ) from e

    # --- low level ---
    def _log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr)

    def _write(self, data: bytes, timeout: int | None = None) -> None:
        timeout = timeout or self.WRITE_TIMEOUT
        offset = 0
        while offset < len(data):
            n = self.dev.write(EP_OUT, data[offset:], timeout)
            if not n:
                raise IOError(f"zero-length USB write at {offset}/{len(data)}")
            offset += n

    def _read(self, timeout: int | None = None) -> bytes:
        try:
            return bytes(self.dev.read(EP_IN, 512, timeout or self.READ_TIMEOUT))
        except usb.core.USBError:
            return b""

    def _read_flush(self) -> None:
        while True:
            try:
                if not self.dev.read(EP_IN, 512, 5):
                    break
            except usb.core.USBError:
                break

    def _command(self, header: bytes, label: str) -> bytes:
        self._write(header)
        resp = self._read()
        self._log(f"  {label}: {len(resp)}B {resp[:16].hex(' ') if resp else '(no reply)'}")
        self._read_flush()
        return resp

    # --- protocol ---
    def read_firmware(self) -> str | None:
        resp = self._command(self.pkt.get_ver(), "GetVer")
        if len(resp) >= 9:
            raw = bytes(resp[8:40])
            fw = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
            if fw:
                self.firmware = fw
                return fw
        return None

    def initialize(self) -> None:
        if self._initialized:
            return
        self._read_flush()
        fw = self.read_firmware()
        self._log(f"firmware: {fw or 'unknown'}")
        self._command(self.pkt.sync_clock(2), "SyncClock")
        self._command(self.pkt.stop_clock(), "StopClock")
        self.clear_layers()
        self._initialized = True

    def clear_layers(self) -> None:
        """Blank both the PNG overlay layer and the JPEG background layer."""
        w, h = self.screen.width, self.screen.height
        buf = io.BytesIO()
        Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(buf, "PNG")
        png = buf.getvalue()
        self._write(self.pkt.png(len(png)) + png)
        self._read()
        self._read_flush()

        buf = io.BytesIO()
        Image.new("RGB", (w, h), (0, 0, 0)).save(
            buf, "JPEG", quality=self.screen.jpeg_quality)
        jpg = buf.getvalue()
        self._write(self.pkt.jpeg(len(jpg)) + jpg)
        self._read()
        self._read_flush()

    def query_buffer_level(self) -> int | None:
        self._write(self.pkt.query_block())
        resp = self._read()
        self._read_flush()
        return resp[8] if len(resp) > 8 else None

    def wait_buffer(self, threshold: int = 2, max_polls: int = 600) -> None:
        for _ in range(max_polls):
            level = self.query_buffer_level()
            if level is None or level <= threshold:
                return
            time.sleep(0.05)

    def send_jpeg(self, jpeg: bytes) -> None:
        if len(jpeg) > self.screen.max_payload:
            raise ValueError(
                f"JPEG {len(jpeg)}B exceeds panel limit {self.screen.max_payload}B")
        self.initialize()
        self._write(self.pkt.jpeg(len(jpeg)) + jpeg)
        resp = self._read()
        self._read_flush()
        # byte 8 is the device-side frame buffer depth; back off when it fills.
        if len(resp) > 8 and resp[8] > 3:
            self.wait_buffer(2)

    def send_image(self, img: Image.Image, rotate: int = 0) -> None:
        self.send_jpeg(encode_jpeg(img, self.screen, rotate))

    def set_brightness(self, value: int) -> None:
        self._command(self.pkt.brightness(value), "Brightness")

    def set_rotation(self, value: int) -> None:
        self._command(self.pkt.rotation(value), "Rotate")

    def set_frame_rate(self, fps: int) -> None:
        self._command(self.pkt.frame_rate(max(1, min(fps, self.screen.max_fps))),
                      "FrameRate")

    def switch_to_desktop_mode(self) -> None:
        """Reboot the panel into desktop mode, where it enumerates as a CH340
        device (1a86:ace1/ad21) and can be driven as a real monitor via evdi."""
        self._command(self.pkt.stop_play(), "StopPlay")
        self._command(self.pkt.switch_to_desktop(), "SwitchToDesktop")
        self._command(self.pkt.reboot(), "Reboot")
        self._initialized = False

    def close(self) -> None:
        try:
            usb.util.release_interface(self.dev, 0)
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError:
            pass


def encode_jpeg(img: Image.Image, screen: ScreenInfo = SCREEN, rotate: int = 0) -> bytes:
    """Fit an image to the panel and JPEG-encode it under the payload cap."""
    if rotate:
        img = img.rotate(rotate, expand=True)
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.size != (screen.width, screen.height):
        img = fit_cover(img, screen.width, screen.height)

    quality = screen.jpeg_quality
    while quality >= 30:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, subsampling=0)
        data = buf.getvalue()
        if len(data) <= screen.max_payload:
            return data
        quality -= 5
    raise ValueError("cannot fit frame under the panel payload limit")


def fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale to cover the target box, then centre-crop — no letterboxing."""
    scale = max(w / img.width, h / img.height)
    resized = img.resize((max(1, round(img.width * scale)),
                          max(1, round(img.height * scale))), Image.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def render_text(text: str, screen: ScreenInfo = SCREEN, rotate: int = 90,
                bg=(0, 0, 0), fg=(255, 255, 255)) -> Image.Image:
    """Render text on a landscape canvas; rotate=90 matches a horizontally
    mounted panel (native orientation is 480x1920 portrait)."""
    landscape = rotate in (90, 270)
    w, h = (screen.height, screen.width) if landscape else (screen.width, screen.height)
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    lines = text.split("\n")
    size = max(12, min(h // max(1, len(lines)) - 8, w // max(1, max(len(l) for l in lines)) * 2))
    font = load_font(size)
    total = sum(draw.textbbox((0, 0), l, font=font)[3] for l in lines)
    y = (h - total) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text(((w - bbox[2]) // 2, y), line, font=font, fill=fg)
        y += bbox[3]
    return img


def load_font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def main() -> int:
    p = argparse.ArgumentParser(
        description='Control the Lian Li 8.8" Universal Screen (1cbe:a088) on Linux')
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="probe the panel and print its firmware version")

    p_img = sub.add_parser("image", help="display a still image")
    p_img.add_argument("path")
    p_img.add_argument("--rotate", type=int, default=90,
                       help="degrees CCW; 90 (default) for a horizontally mounted panel")

    p_txt = sub.add_parser("text", help="display text")
    p_txt.add_argument("text")
    p_txt.add_argument("--rotate", type=int, default=90)

    p_br = sub.add_parser("brightness", help="set backlight 0-100")
    p_br.add_argument("value", type=int)

    p_rot = sub.add_parser("rotation", help="set panel rotation 0-3")
    p_rot.add_argument("value", type=int)

    sub.add_parser("clear", help="blank both layers")
    sub.add_parser("desktop", help="reboot into desktop mode (real second monitor)")

    args = p.parse_args()

    screen = UniversalScreen(verbose=args.verbose)
    try:
        if args.cmd == "info":
            screen._read_flush()
            fw = screen.read_firmware()
            print(f'Lian Li 8.8" Universal Screen  ({VID:04x}:{PID:04x})')
            print(f"  panel      : {screen.screen.width}x{screen.screen.height}")
            print(f"  firmware   : {fw or 'no reply'}")
            level = screen.query_buffer_level()
            print(f"  buffer lvl : {level if level is not None else 'no reply'}")
        elif args.cmd == "image":
            screen.send_image(Image.open(args.path), rotate=args.rotate)
            print(f"sent {args.path}")
        elif args.cmd == "text":
            screen.send_image(render_text(args.text, screen.screen, args.rotate),
                              rotate=args.rotate)
            print("sent text")
        elif args.cmd == "brightness":
            screen.set_brightness(args.value)
            print(f"brightness -> {args.value}")
        elif args.cmd == "rotation":
            screen.set_rotation(args.value)
            print(f"rotation -> {args.value}")
        elif args.cmd == "clear":
            screen.initialize()
            print("cleared")
        elif args.cmd == "desktop":
            screen.switch_to_desktop_mode()
            print("switch sent — the panel will reboot and re-enumerate as 1a86:ace1/ad21")
    finally:
        screen.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
