#!/usr/bin/env python3
import argparse
from pathlib import Path
from PIL import Image, ImageOps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--max-side", type=int, default=768)
    parser.add_argument("--quality", type=int, default=88)
    args = parser.parse_args()

    src = Path(args.input)
    dst = Path(args.output)
    dst.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((args.max_side, args.max_side), Image.Resampling.LANCZOS)
        im.save(dst, "JPEG", quality=args.quality, optimize=True)

    print(str(dst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
