"""Driver for the ARGB LED ring around the Lian Li 8.8" screen (0416:8050).

Protocol (from lian-li-linux crates/lianli-devices/src/winusb/led.rs, which
reverse-engineered L-Connect): fixed 64-byte bulk packets on EP_OUT —

    byte[0]    = 0x11              set-LED-chunk command
    byte[1]    = LED offset        0, 20, 40, …
    byte[4:64] = 20 x RGB          one chunk of the 60-LED ring

Three chunks paint the whole ring; the firmware latches immediately, so a
frame is just three writes and "direct mode" animation is streaming frames.
"""

from __future__ import annotations

import time

import usb.core
import usb.util

VID, PID = 0x0416, 0x8050
LED_COUNT = 60
LEDS_PER_CHUNK = 20
PACKET_SIZE = 64
CMD_SET_LEDS = 0x11


class LedRing:
    """The 60-LED ring, painted by streaming full frames."""

    def __init__(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise RuntimeError(f"LED ring {VID:04x}:{PID:04x} not found")
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        dev.set_configuration()
        usb.util.claim_interface(dev, 0)

        # First bulk OUT endpoint — the device is vendor-class with one each way.
        cfg = dev.get_active_configuration()
        self.ep_out = None
        for ep in cfg[(0, 0)]:
            if (usb.util.endpoint_direction(ep.bEndpointAddress)
                    == usb.util.ENDPOINT_OUT
                    and usb.util.endpoint_type(ep.bmAttributes)
                    == usb.util.ENDPOINT_TYPE_BULK):
                self.ep_out = ep.bEndpointAddress
                break
        if self.ep_out is None:
            raise RuntimeError("LED ring: no bulk OUT endpoint")
        self.dev = dev
        self.count = LED_COUNT

    def send_frame(self, colors: list[tuple[int, int, int]]) -> None:
        """Paint the ring. `colors` shorter than 60 is padded with black."""
        frame = (list(colors) + [(0, 0, 0)] * LED_COUNT)[:LED_COUNT]
        for chunk in range(0, LED_COUNT, LEDS_PER_CHUNK):
            pkt = bytearray(PACKET_SIZE)
            pkt[0] = CMD_SET_LEDS
            pkt[1] = chunk
            for i, (r, g, b) in enumerate(frame[chunk:chunk + LEDS_PER_CHUNK]):
                off = 4 + i * 3
                pkt[off:off + 3] = bytes((max(0, min(255, int(r))),
                                          max(0, min(255, int(g))),
                                          max(0, min(255, int(b)))))
            self.dev.write(self.ep_out, bytes(pkt), 1000)

    def off(self) -> None:
        self.send_frame([(0, 0, 0)] * LED_COUNT)

    def close(self) -> None:
        try:
            self.off()
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass


if __name__ == "__main__":
    import sys

    if "--calibrate" in sys.argv:
        # Six static colour blocks of 10 LEDs, in chain order:
        # red, green, blue, yellow, magenta, cyan. Note where each block sits
        # on the bezel, then set [led] layout/offset/reverse in config.toml.
        # Stop pc-screens first: systemctl --user stop pc-screens
        secs = 60
        ring = LedRing()
        frame = []
        for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255),
                  (255, 255, 0), (255, 0, 255), (0, 255, 255)):
            frame += [c] * 10
        ring.send_frame(frame)
        print("red green blue yellow magenta cyan — 10 LEDs each, "
              f"{secs}s to look…")
        time.sleep(secs)
        ring.close()
        print("done")
        sys.exit(0)

    # Smoke test: sweep a white dot around the ring for two seconds, then off.
    ring = LedRing()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        pos = int(((time.monotonic() - t0) * 30) % LED_COUNT)
        frame = [(0, 0, 0)] * LED_COUNT
        for k in range(6):
            fade = 1.0 - k / 6
            frame[(pos - k) % LED_COUNT] = (int(80 * fade),) * 3
        ring.send_frame(frame)
        time.sleep(1 / 30)
    ring.close()
    print("ok")
