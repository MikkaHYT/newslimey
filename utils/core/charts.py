"""Pillow rendering for the Last.fm image charts.

Two shapes only: a grid of covers (``,lf chartartists`` / ``,lf chartalbums``)
and a stacked iceberg (``,lf iceberg``). Everything is drawn on the server's
dark accent colour so the cards match the rest of the bot.

Pillow is optional, exactly as it is for the quote card: if it isn't installed
the callers fall back to a text card, so nothing here is required for the bot to
boot.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Sequence

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:  # Pillow isn't installed for this interpreter
    PIL_AVAILABLE = False

logger = logging.getLogger("slimey.charts")

BACKGROUND = (43, 45, 49)  # the bot's #2b2d31 accent
TILE_BACKGROUND = (58, 61, 66)
TITLE_COLOR = (255, 255, 255)
LABEL_COLOR = (235, 235, 235)
SUB_COLOR = (160, 162, 168)
ICEBERG_COLORS = (
    (88, 101, 242),
    (87, 136, 242),
    (86, 168, 224),
    (93, 197, 190),
    (126, 217, 158),
    (0x2b2d31),
)

FONT_ROOTS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype",
    "/usr/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
    "C:/Windows/Fonts",
    "C:\\Windows\\Fonts",
)
FONT_NAMES = {
    True: ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"),
    False: ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf", "arial.ttf"),
}


def load_font(size: int, *, bold: bool = False):
    """The first usable system font at ``size``, else Pillow's bitmap default."""
    if not PIL_AVAILABLE:
        return None
    for root in FONT_ROOTS:
        for name in FONT_NAMES[bold]:
            path = os.path.join(root, name)
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    continue
    return ImageFont.load_default()


def _fit(text: str, draw, font, limit: int) -> str:
    """Trim ``text`` with an ellipsis until it fits ``limit`` pixels."""
    text = str(text or "").strip()
    if not text:
        return ""
    if draw.textlength(text, font=font) <= limit:
        return text
    while text and draw.textlength(text + "…", font=font) > limit:
        text = text[:-1]
    return text + "…"


def _rounded(image: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *image.size), radius=radius, fill=255)
    rounded = Image.new("RGBA", image.size, (0, 0, 0, 0))
    rounded.paste(image, (0, 0), mask)
    return rounded


def _square(raw: bytes, size: int) -> Image.Image:
    """Cover-fit ``raw`` into a ``size`` x ``size`` RGB tile."""
    with Image.open(io.BytesIO(raw)) as handle:
        image = handle.convert("RGB")
    return _cover(image, size)


def _cover(image: Image.Image, size: int) -> Image.Image:
    """Center-crop ``image`` to a square and resize it down to ``size``."""
    width, height = image.size
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    cropped = image.crop((left, top, left + edge, top + edge))
    return cropped.resize((size, size), Image.LANCZOS)


def grid(
    items: Sequence[tuple[bytes | None, str, str]],
    *,
    columns: int = 3,
    tile: int = 336,
    gap: int = 18,
    padding: int = 28,
) -> io.BytesIO | None:
    """A cover grid: ``(image_bytes, label, sublabel)`` per entry."""
    if not PIL_AVAILABLE or not items:
        return None

    rows = (len(items) + columns - 1) // columns
    width = padding * 2 + columns * tile + (columns - 1) * gap
    label_h = 66
    height = padding * 2 + rows * (tile + label_h) + (rows - 1) * gap

    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    label_font = load_font(26, bold=True)
    sub_font = load_font(21)
    radius = 20

    for index, (raw, label, sublabel) in enumerate(items):
        column = index % columns
        row = index // columns
        x = padding + column * (tile + gap)
        y = padding + row * (tile + label_h + gap)

        if raw:
            try:
                cover = _rounded(_square(raw, tile), radius)
                canvas.paste(cover, (x, y), cover)
            except Exception:
                logger.debug("Could not draw a chart tile", exc_info=True)
                raw = None
        if not raw:
            placeholder = Image.new("RGB", (tile, tile), TILE_BACKGROUND)
            draw.rounded_rectangle(
                (x, y, x + tile, y + tile), radius=radius, fill=TILE_BACKGROUND
            )

        draw.text(
            (x + 4, y + tile + 8),
            _fit(label, draw, label_font, tile - 8),
            font=label_font,
            fill=LABEL_COLOR,
        )
        if sublabel:
            draw.text(
                (x + 4, y + tile + 38),
                _fit(sublabel, draw, sub_font, tile - 8),
                font=sub_font,
                fill=SUB_COLOR,
            )

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def _wrap(draw, text: str, font, limit: int, max_lines: int = 2) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= limit:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        lines = [""]
    if len(lines) == max_lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > limit:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


#: Sky (top) to horizon, then ocean down to the seabed. Positions are fractions
#: of the canvas height and get stretched to fit whichever waterline is used.
SKY_TOP = (120, 199, 236)
SKY_HORIZON = (203, 231, 249)
OCEAN_TOP = (46, 108, 178)
OCEAN_BOTTOM = (6, 20, 48)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _gradient(width: int, height: int, stops: Sequence[tuple[float, tuple[int, int, int]]]) -> Image.Image:
    """A vertical gradient built from ``(position, colour)`` stops."""
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    for y in range(height):
        t = y / max(1, height - 1)
        colour = stops[-1][1]
        for index in range(len(stops) - 1):
            pos_a, colour_a = stops[index]
            pos_b, colour_b = stops[index + 1]
            if pos_a <= t <= pos_b:
                span = pos_b - pos_a
                colour = _lerp(colour_a, colour_b, 0.0 if span <= 0 else (t - pos_a) / span)
                break
        pixels[0, y] = colour
    return strip.resize((width, height), Image.BILINEAR)


def _draw_cloud(draw, box: tuple[int, int, int, int], fill: tuple[int, int, int] = (255, 255, 255)) -> None:
    """A fluffy cloud: overlapping circles with a flat bottom at ``y1``."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    puffs = (
        (0.02, 0.34, 0.30, 1.00),
        (0.15, 0.03, 0.52, 1.00),
        (0.38, 0.12, 0.74, 1.00),
        (0.62, 0.00, 0.94, 1.00),
        (0.78, 0.28, 1.00, 1.00),
        (0.08, 0.50, 0.94, 1.00),
    )
    for px0, py0, px1, py1 in puffs:
        draw.ellipse(
            (x0 + int(w * px0), y0 + int(h * py0), x0 + int(w * px1), y0 + int(h * py1)),
            fill=fill,
        )


def _draw_iceberg(
    canvas: Image.Image,
    cx: int,
    apex_y: int,
    waterline: int,
    height: int,
) -> None:
    """The iceberg itself: a bright mass above the water, a translucent one below."""
    width = canvas.width
    left = cx - int(width * 0.225)
    right = cx + int(width * 0.205)
    up = waterline - apex_y

    above = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ad = ImageDraw.Draw(above)
    peak = (cx - int(width * 0.02), apex_y)
    outline = [
        (left, waterline),
        (cx - int(width * 0.17), waterline - int(up * 0.34)),
        (cx - int(width * 0.11), waterline - int(up * 0.62)),
        peak,
        (cx + int(width * 0.07), waterline - int(up * 0.56)),
        (cx + int(width * 0.14), waterline - int(up * 0.28)),
        (right, waterline),
    ]
    ad.polygon(outline, fill=(249, 252, 255, 255))
    ad.line(outline + [outline[0]], fill=(163, 203, 234, 255), width=3, joint="curve")
    ad.polygon(
        [peak, (cx + int(width * 0.07), waterline - int(up * 0.56)), (cx, waterline),
         (cx - int(width * 0.11), waterline - int(up * 0.62))],
        fill=(214, 233, 247, 255),
    )
    ad.polygon(
        [(left, waterline), (cx - int(width * 0.17), waterline - int(up * 0.34)),
         (cx - int(width * 0.08), waterline)],
        fill=(226, 240, 250, 255),
    )
    canvas.alpha_composite(above)

    below = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(below)
    tip_y = int(height * 0.60)
    span = tip_y - waterline
    bd.polygon(
        [
            (cx - int(width * 0.25), waterline),
            (cx - int(width * 0.15), waterline + int(span * 0.45)),
            (cx - int(width * 0.03), tip_y),
            (cx + int(width * 0.03), waterline + int(span * 0.72)),
            (cx + int(width * 0.13), waterline + int(span * 0.40)),
            (cx + int(width * 0.24), waterline),
        ],
        fill=(152, 201, 236, 165),
    )
    bd.polygon(
        [(cx - int(width * 0.25), waterline), (cx - int(width * 0.03), tip_y),
         (cx - int(width * 0.06), waterline)],
        fill=(178, 218, 243, 140),
    )
    bd.polygon(
        [(cx + int(width * 0.24), waterline), (cx + int(width * 0.13), waterline + int(span * 0.40)),
         (cx + int(width * 0.05), waterline)],
        fill=(120, 176, 221, 135),
    )
    canvas.alpha_composite(below)

    # .fm flag on the peak
    pole_top = apex_y - 52
    ImageDraw.Draw(canvas).line([(cx - 2, apex_y), (cx - 2, pole_top)], fill=(90, 94, 104), width=3)
    flag = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    fd = ImageDraw.Draw(flag)
    fd.rounded_rectangle((cx, pole_top, cx + 58, pole_top + 30), radius=5, fill=(74, 82, 184, 255))
    canvas.alpha_composite(flag)
    flag_font = load_font(max(15, int(width * 0.019)), bold=True)
    ImageDraw.Draw(canvas).text(
        (cx + 29, pole_top + 15), ".fm", font=flag_font, fill=(255, 255, 255), anchor="mm"
    )


def iceberg(
    names: Sequence[str],
    *,
    title: str = "",
    subtitle: str = "",
    above: int | None = None,
    width: int = 1000,
) -> io.BytesIO | None:
    """The taste iceberg, in the classic ``.fm`` style.

    ``names`` is the ranked list, most played first. They fill bands of three
    (left, right, then across the iceberg) running down the image. The first
    ``above`` bands float above the waterline and the rest sink below it, so
    the top of the list is the visible tip and the deep cuts are submerged.
    """
    if not PIL_AVAILABLE or not names:
        return None

    names = [str(name).strip() for name in names if str(name).strip()]
    if not names:
        return None

    per_band = 3
    band_h = 52
    header_h = 250
    width = max(720, int(width))
    cx = int(width * 0.52)

    bands = (len(names) + per_band - 1) // per_band
    if above is None:
        above = (bands + 1) // 2
    above = max(1, min(int(above), bands))
    waterline = header_h + above * band_h
    names_bottom = header_h + bands * band_h
    # Enough ocean below the last band for the iceberg to taper off without
    # leaving a huge void when a member only has a handful of artists.
    height = max(900, names_bottom + 440)

    horizon = waterline / height
    canvas = _gradient(
        width,
        height,
        [
            (0.0, SKY_TOP),
            (horizon, SKY_HORIZON),
            (min(1.0, horizon + 0.0001), OCEAN_TOP),
            (0.58, (20, 62, 120)),
            (1.0, OCEAN_BOTTOM),
        ],
    ).convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # -- clouds and title ---------------------------------------------
    _draw_cloud(draw, (int(width * 0.02), 16, int(width * 0.98), 150))
    title_font = load_font(max(40, int(width * 0.068)), bold=True)
    sub_font = load_font(max(24, int(width * 0.040)), bold=True)
    if title:
        draw.text((cx, 54), _fit(f"{title}'s", draw, title_font, int(width * 0.8)),
                  font=title_font, fill=(12, 12, 12), anchor="mm")
        draw.text((cx + int(width * 0.03), 116), "iceberg", font=sub_font, fill=(12, 12, 12), anchor="mm")
    if subtitle:
        _draw_cloud(draw, (int(width * 0.60), 152, int(width * 0.96), 226))
        draw.text((int(width * 0.78), 190), subtitle, font=sub_font, fill=(12, 12, 12), anchor="mm")

    # -- iceberg ------------------------------------------------------
    _draw_iceberg(canvas, cx, header_h + 24, waterline, height)

    # -- tier separator lines -----------------------------------------
    lines = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for band in range(bands + 1):
        y = header_h + band * band_h
        ld.line([(int(width * 0.02), y), (int(width * 0.98), y)], fill=(255, 255, 255, 105), width=2)
    canvas.alpha_composite(lines)

    # -- names --------------------------------------------------------
    name_font = load_font(max(19, int(width * 0.026)), bold=True)
    left_x, right_x = int(width * 0.055), int(width * 0.945)
    # Sized so a long left/right name can never run into the centre column.
    centre_limit = int(width * 0.30)
    left_limit = (cx - centre_limit // 2 - 18) - left_x
    right_limit = right_x - (cx + centre_limit // 2 + 18)

    for band in range(bands):
        y0 = header_h + band * band_h
        centre_y = y0 + band_h // 2
        chunk = names[band * per_band : (band + 1) * per_band]
        submerged = centre_y >= waterline
        fill = (255, 255, 255) if submerged else (12, 12, 12)
        stroke = (0, 0, 0) if submerged else None
        for slot, name in zip(("left", "right", "centre"), chunk):
            if slot == "left":
                x, anchor, limit = left_x, "lm", left_limit
            elif slot == "right":
                x, anchor, limit = right_x, "rm", right_limit
            else:
                x, anchor, limit = cx, "mm", centre_limit
            draw.text(
                (x, centre_y),
                _fit(name, draw, name_font, limit),
                font=name_font,
                fill=fill,
                anchor=anchor,
                stroke_width=3 if stroke else 0,
                stroke_fill=stroke,
            )

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer
