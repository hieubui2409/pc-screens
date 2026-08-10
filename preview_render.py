#!/usr/bin/env python3
"""
Render any theme offline — no panel hardware needed.

Used for three things: testing a theme while developing it, eyeballing a single
frame as PNG, and recording the review GIFs at the panels' real frame rates.

Examples:
    .venv/bin/python preview_render.py --view neon-grid --size 480x1920 \
        --png /tmp/frame.png
    .venv/bin/python preview_render.py --view matrix --size 480x960 \
        --seconds 3 --fps 17 --scale 0.5 --gif /tmp/matrix.gif
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import fx
from themes import VIEWS


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--view", required=True, choices=sorted(VIEWS))
    p.add_argument("--size", default="480x1920", metavar="WxH",
                   help="canvas size (the size the viewer sees, after rotation)")
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--fps", type=float, default=19.0)
    p.add_argument("--scale", type=float, default=1.0,
                   help="downscale factor for the GIF (1.0 = native)")
    p.add_argument("--palette", help="palette name or custom:#hex,... "
                                     "(default: theme's own)")
    p.add_argument("--anim", choices=["calm", "normal", "intense"], default="normal")
    p.add_argument("--gif", metavar="OUT.gif", help="record an animated GIF")
    p.add_argument("--png", metavar="OUT.png", help="save a single frame")
    p.add_argument("--frames-dir", metavar="DIR", help="dump every frame as PNG")
    p.add_argument("--owner-name", default="Lucas Bui")
    p.add_argument("--owner-role", default="Senior AI Engineer")
    p.add_argument("--owner-date", default="24/09/1997")
    args = p.parse_args()

    if not (args.gif or args.png or args.frames_dir):
        p.error("pick at least one output: --gif, --png or --frames-dir")

    w, h = (int(x) for x in args.size.lower().split("x"))
    fx.set_anim(args.anim)
    pal = fx.parse_palette(args.palette) if args.palette else None
    fx.prime_counters()
    time.sleep(0.3)          # give the delta-based counters a real window

    opts = {"owner_name": args.owner_name, "owner_role": args.owner_role,
            "owner_date": args.owner_date}
    view = VIEWS[args.view](w, h, pal, opts)

    if args.png:
        view.render().save(args.png)
        print(f"wrote {args.png}")
        if not (args.gif or args.frames_dir):
            return 0

    n = max(1, int(args.seconds * args.fps))
    frames = []
    t0 = time.monotonic()
    for i in range(n):
        # Real-time pacing: the animation clock is wall-time based, so
        # rendering as fast as possible would compress the motion.
        target = t0 + i / args.fps
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        img = view.render()
        if args.scale != 1.0:
            img = img.resize((int(w * args.scale), int(h * args.scale)))
        frames.append(img)
        if args.frames_dir:
            img.save(f"{args.frames_dir}/frame{i:04d}.png")
    render_ms = (time.monotonic() - t0 - max(0.0, args.seconds - 1 / args.fps)) * 1000

    if args.gif:
        frames[0].save(args.gif, save_all=True, append_images=frames[1:],
                       duration=int(1000 / args.fps), loop=0, optimize=True)
        print(f"wrote {args.gif} — {len(frames)} frames @ {args.fps:g} fps")
    if args.frames_dir:
        print(f"wrote {n} frames to {args.frames_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
