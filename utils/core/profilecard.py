"""Pillow rendering for the ``,profilestats`` card.

One entry point - :func:`render` - draws a member's last two weeks of activity:
who they are, when they joined, their message and voice ranks, the 1d/7d/14d
totals, the channels they spend their time in, and a per-day chart of both.

This module only draws. Everything it shows is handed in by the caller, so the
database reads and the avatar download stay with the cog that owns them.

Pillow is optional, exactly as it is for the Last.fm charts: when it isn't
installed :func:`render` returns ``None`` and the caller posts a text card
instead, so nothing here is required for the bot to boot.
"""

from __future__ import annotations

import io
import logging
import os
import math
from typing import Sequence

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:  # Pillow isn't installed for this interpreter
    PIL_AVAILABLE = False

logger = logging.getLogger("slimey.profilecard")

#: Canvas geometry. Everything below is sized off these, so the card can be
#: widened without touching the individual panels.
WIDTH = 1200
PADDING = 28
GAP = 18
RADIUS = 18

PAGE = (30, 31, 34)
CARD = (43, 45, 49)  # the bot's #2b2d31 accent
INNER = (35, 36, 40)
CHIP = (58, 61, 66)
TEXT = (255, 255, 255)
LABEL = (205, 207, 212)
MUTED = (160, 162, 168)
GRID = (52, 54, 59)
GREEN = (59, 165, 93)  # messages
PINK = (235, 69, 158)  # voice

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
FONT_FILES = {
    "regular": ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf", "arial.ttf"),
    "bold": ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"),
    "italic": (
        "DejaVuSans-Oblique.ttf",
        "LiberationSans-Italic.ttf",
        "Arial Italic.ttf",
        "ariali.ttf",
    ),
}


def _font(size: int, style: str = "regular"):
    """The first usable system font at ``size``, else Pillow's bitmap default."""
    if not PIL_AVAILABLE:
        return None
    for root in FONT_ROOTS:
        for name in FONT_FILES.get(style, FONT_FILES["regular"]):
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


def _significant(value: int, digits: int = 3) -> str:
    """``value`` rounded to ``digits`` significant figures, as a clean string.

    This is the Statbot house style for counts - ``916``, ``8.59K``, ``12.6K`` -
    where the short form keeps three figures rather than two decimal places.
    """
    value = abs(int(value or 0))
    if value < 1000:
        return f"{value:,}"
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value < limit:
            continue
        scaled = value / limit
        places = max(0, digits - 1 - int(math.floor(math.log10(scaled))))
        text = f"{scaled:.{places}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text + suffix
    return f"{value:,}"


def _hours(seconds: int) -> str:
    """Voice time in hours, two decimals - ``4.08``, ``118.08``."""
    return f"{int(seconds or 0) / 3600:.2f}"


def _initials(name: str) -> str:
    """One or two letters for the fallback avatar tile."""
    parts = [part for part in str(name or "").split() if part]
    if not parts:
        return "?"
    return "".join(part[0] for part in parts[:2]).upper()


def _columns(x0: int, width: int, count: int, gap: int) -> list[tuple[int, int]]:
    """``count`` side-by-side ``(left, width)`` boxes filling ``width`` exactly."""
    usable = width - gap * (count - 1)
    step = usable / count
    boxes: list[tuple[int, int]] = []
    for index in range(count):
        left = x0 + round(index * (step + gap))
        right = x0 + round((index + 1) * step + index * gap)
        boxes.append((left, right - left))
    return boxes


def _circle(raw: bytes, size: int) -> "Image.Image":
    """Cover-fit ``raw`` into a circular ``size`` x ``size`` RGBA tile."""
    with Image.open(io.BytesIO(raw)) as handle:
        image = handle.convert("RGBA")
    width, height = image.size
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    image = image.crop((left, top, left + edge, top + edge)).resize(
        (size, size), Image.LANCZOS
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(image, (0, 0), mask)
    return tile


def _icon(draw, kind: str, x: int, y: int, size: int, colour) -> None:
    """A small vector glyph, since the bundled system fonts have no emoji."""
    if kind == "hash":
        draw.text(
            (x + size / 2, y + size / 2),
            "#",
            font=_font(int(size * 1.35), "bold"),
            fill=colour,
            anchor="mm",
        )
        return

    if kind == "trophy":
        s = size
        draw.polygon(
            [
                (x + 0.20 * s, y + 0.08 * s),
                (x + 0.80 * s, y + 0.08 * s),
                (x + 0.72 * s, y + 0.48 * s),
                (x + 0.50 * s, y + 0.60 * s),
                (x + 0.28 * s, y + 0.48 * s),
            ],
            fill=colour,
        )
        draw.arc(
            (x + 0.00 * s, y + 0.04 * s, x + 0.46 * s, y + 0.46 * s),
            start=90,
            end=270,
            fill=colour,
            width=max(2, int(s * 0.10)),
        )
        draw.arc(
            (x + 0.54 * s, y + 0.04 * s, x + 1.00 * s, y + 0.46 * s),
            start=270,
            end=90,
            fill=colour,
            width=max(2, int(s * 0.10)),
        )
        draw.line((x + 0.50 * s, y + 0.60 * s, x + 0.50 * s, y + 0.80 * s), fill=colour, width=max(2, int(s * 0.10)))
        draw.line((x + 0.28 * s, y + 0.86 * s, x + 0.72 * s, y + 0.86 * s), fill=colour, width=max(2, int(s * 0.12)))
        return

    if kind == "voice":
        s = size
        draw.polygon(
            [
                (x + 0.04 * s, y + 0.34 * s),
                (x + 0.26 * s, y + 0.34 * s),
                (x + 0.50 * s, y + 0.10 * s),
                (x + 0.50 * s, y + 0.90 * s),
                (x + 0.26 * s, y + 0.66 * s),
                (x + 0.04 * s, y + 0.66 * s),
            ],
            fill=colour,
        )
        width = max(2, int(s * 0.10))
        draw.arc((x + 0.44 * s, y + 0.22 * s, x + 0.78 * s, y + 0.78 * s), start=-55, end=55, fill=colour, width=width)
        draw.arc((x + 0.50 * s, y + 0.04 * s, x + 1.00 * s, y + 0.96 * s), start=-52, end=52, fill=colour, width=width)
        return

    # "chart": a rising line, which is also the icon for the charts panel.
    s = size
    points = [
        (x + 0.05 * s, y + 0.82 * s),
        (x + 0.34 * s, y + 0.50 * s),
        (x + 0.58 * s, y + 0.66 * s),
        (x + 0.95 * s, y + 0.18 * s),
    ]
    draw.line(points, fill=colour, width=max(2, int(s * 0.12)), joint="curve")
    for px, py in points:
        r = s * 0.06
        draw.ellipse((px - r, py - r, px + r, py + r), fill=colour)


def _card(draw, x: int, y: int, w: int, h: int, title: str, icon: str | None) -> None:
    """The panel background plus its heading and corner icon."""
    draw.rounded_rectangle((x, y, x + w, y + h), radius=RADIUS, fill=CARD)
    draw.text((x + 18, y + 20), title, font=_font(26, "bold"), fill=TEXT, anchor="lm")
    if icon:
        _icon(draw, icon, x + w - 20 - 24, y + 20 - 12, 24, MUTED)


def _metric_rows(draw, x: int, y: int, w: int, rows: Sequence[tuple[str, str, str]]) -> None:
    """The 1d/7d/14d rows: a day chip, a big bold value and its italic unit."""
    chip_font = _font(22, "bold")
    value_font = _font(28, "bold")
    unit_font = _font(22, "italic")
    for index, (label, value, unit) in enumerate(rows):
        row_y = y + index * 48
        draw.rounded_rectangle((x, row_y + 2, x + 54, row_y + 36), radius=10, fill=INNER)
        draw.text((x + 27, row_y + 19), label, font=chip_font, fill=LABEL, anchor="mm")
        value_x = x + 54 + 14
        draw.text((value_x, row_y + 19), value, font=value_font, fill=TEXT, anchor="lm")
        unit_x = value_x + draw.textlength(value, font=value_font) + 8
        if unit_x < x + w:
            draw.text((unit_x, row_y + 21), unit, font=unit_font, fill=MUTED, anchor="lm")


def _rank_rows(draw, x: int, y: int, w: int, rows: Sequence[tuple[str, str]]) -> None:
    """The ranks panel: a label on the left of an inner box, the rank on the right."""
    label_font = _font(23, "bold")
    value_font = _font(25, "bold")
    for index, (label, value) in enumerate(rows):
        row_y = y + index * 48
        draw.rounded_rectangle((x, row_y, x + w, row_y + 42), radius=12, fill=INNER)
        draw.text((x + 18, row_y + 21), label, font=label_font, fill=LABEL, anchor="lm")
        draw.text((x + w - 18, row_y + 21), value, font=value_font, fill=TEXT, anchor="rm")


def _channel_rows(
    draw,
    x: int,
    y: int,
    w: int,
    rows: Sequence[tuple[str, int, bool]],
) -> None:
    """The channels panel: a kind icon, a chip holding the name, then the amount."""
    name_font = _font(22, "bold")
    value_font = _font(22, "bold")
    unit_font = _font(20, "italic")
    for index, (label, amount, is_message) in enumerate(rows):
        row_y = y + index * 56
        _icon(draw, "hash" if is_message else "voice", x, row_y + 12, 22, MUTED)

        value = _significant(amount) if is_message else _hours(amount)
        unit = "messages" if is_message else "hours"
        value_w = draw.textlength(value, font=value_font)
        unit_w = draw.textlength(unit, font=unit_font)
        total = value_w + 8 + unit_w
        start_x = x + w - total

        chip_left = x + 22 + 12
        chip_right = start_x - 16
        if chip_right > chip_left + 40:
            draw.rounded_rectangle((chip_left, row_y + 2, chip_right, row_y + 42), radius=10, fill=CHIP)
            draw.text(
                (chip_left + 12, row_y + 22),
                _fit(label, draw, name_font, chip_right - chip_left - 24),
                font=name_font,
                fill=TEXT,
                anchor="lm",
            )

        draw.text((start_x, row_y + 22), value, font=value_font, fill=TEXT, anchor="lm")
        draw.text((start_x + value_w + 8, row_y + 23), unit, font=unit_font, fill=MUTED, anchor="lm")


def _series_points(values: Sequence[int], px0: int, px1: int, py0: int, py1: int) -> list[tuple[float, float]]:
    """Scale one series onto the plot box, each series to its own maximum.

    Normalising independently is what makes both lines readable on one chart:
    a member with 900 messages and 120 hours of voice would otherwise draw the
    message line flat along the floor.
    """
    count = len(values)
    top = max(max(values) if values else 0, 1)
    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        fraction = index / (count - 1) if count > 1 else 0.0
        px = px0 + (px1 - px0) * fraction
        py = py1 - (max(0, int(value)) / top) * (py1 - py0 - 6)
        points.append((px, py))
    return points


def render(
    *,
    name: str,
    handle: str = "",
    level: int | None = None,
    created_at: str = "",
    joined_at: str = "",
    message_rank: int | None = None,
    voice_rank: int | None = None,
    windows: Sequence[tuple[str, int, int]] = (),
    top_channels: Sequence[tuple[str, int, bool]] = (),
    series: Sequence[tuple[int, int]] = (),
    avatar: bytes | None = None,
    lookback_days: int = 14,
    brand: str = "Slimey",
) -> io.BytesIO | None:
    """Draw the activity card and return it as a PNG buffer.

    ``windows`` is ``(label, messages, voice_seconds)`` per lookback window,
    ``top_channels`` is ``(label, amount, is_message)`` and ``series`` is one
    ``(messages, voice_seconds)`` pair per day, oldest first. Returns ``None``
    when Pillow isn't available or the draw fails, so the caller can post its
    text fallback.
    """
    if not PIL_AVAILABLE:
        return None

    try:
        canvas = _compose(
            name=name,
            handle=handle,
            level=level,
            created_at=created_at,
            joined_at=joined_at,
            message_rank=message_rank,
            voice_rank=voice_rank,
            windows=windows,
            top_channels=top_channels,
            series=series,
            avatar=avatar,
            lookback_days=lookback_days,
            brand=brand,
        )
    except Exception:
        logger.exception("Could not render the profile stats card")
        return None

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def _compose(**kw) -> "Image.Image":
    """Lay the card out and draw every panel. Split out to keep :func:`render` small."""
    name: str = kw["name"]
    handle: str = kw["handle"]
    level = kw["level"]
    created_at: str = kw["created_at"]
    joined_at: str = kw["joined_at"]
    message_rank = kw["message_rank"]
    voice_rank = kw["voice_rank"]
    windows: Sequence[tuple[str, int, int]] = kw["windows"]
    top_channels: Sequence[tuple[str, int, bool]] = kw["top_channels"]
    series: Sequence[tuple[int, int]] = kw["series"]
    avatar: bytes | None = kw["avatar"]
    lookback_days: int = kw["lookback_days"]
    brand: str = kw["brand"]

    columns3 = _columns(PADDING, WIDTH - PADDING * 2, 3, GAP)
    columns2 = _columns(PADDING, WIDTH - PADDING * 2, 2, GAP)

    header_top = PADDING
    header_h = 108
    row1_top = header_top + header_h + GAP
    row1_h = 214
    row2_top = row1_top + row1_h + GAP
    row2_h = 252
    footer_top = row2_top + row2_h + GAP
    height = footer_top + 30 + PADDING

    canvas = Image.new("RGB", (WIDTH, height), PAGE)
    draw = ImageDraw.Draw(canvas)

    # -- header: avatar, name, badges ---------------------------------
    avatar_size = 96
    ax, ay = PADDING, header_top + 4
    if avatar:
        try:
            tile = _circle(avatar, avatar_size)
            canvas.paste(tile, (ax, ay), tile)
        except Exception:
            logger.debug("Could not draw the profile avatar", exc_info=True)
            avatar = None
    if not avatar:
        draw.ellipse((ax, ay, ax + avatar_size, ay + avatar_size), fill=CHIP)
        draw.text(
            (ax + avatar_size / 2, ay + avatar_size / 2),
            _initials(name),
            font=_font(36, "bold"),
            fill=TEXT,
            anchor="mm",
        )

    # The badges share the header with the name, so they are kept compact: the
    # date font and padding set how much room the name gets on the left.
    badge_font = _font(19, "bold")
    badge_value_font = _font(22, "bold")
    badges = (("Created On", created_at or "—"), ("Joined On", joined_at or "—"))
    badge_pad = 14
    widths = [
        int(max(draw.textlength(label, font=badge_font), draw.textlength(value, font=badge_value_font)))
        + badge_pad * 2
        for label, value in badges
    ]
    badge_h = 80
    badge_left = WIDTH - PADDING - (sum(widths) + GAP)
    for index, ((label, value), width) in enumerate(zip(badges, widths)):
        bx = badge_left + index * (width + GAP)
        draw.rounded_rectangle((bx, ay, bx + width, ay + badge_h), radius=14, fill=CARD)
        draw.text((bx + badge_pad, ay + 15), label, font=badge_font, fill=MUTED, anchor="lm")
        draw.text((bx + badge_pad, ay + 50), value, font=badge_value_font, fill=TEXT, anchor="lm")

    text_x = ax + avatar_size + 22
    name_font = _font(46, "bold")
    room = badge_left - text_x - 20
    fitted_name = _fit(name, draw, name_font, room)
    draw.text((text_x, ay + 6), fitted_name, font=name_font, fill=TEXT, anchor="la")
    name_width = draw.textlength(fitted_name, font=name_font)
    handle_font = _font(26)
    if handle and text_x + name_width + 16 < badge_left - 60:
        draw.text(
            (text_x + name_width + 16, ay + 26),
            _fit(handle, draw, handle_font, badge_left - 20 - (text_x + name_width + 16)),
            font=handle_font,
            fill=MUTED,
            anchor="la",
        )
    subtitle = f"level {level}" if level is not None else ""
    if subtitle:
        draw.text((text_x, ay + 62), subtitle, font=_font(24), fill=MUTED, anchor="la")

    # -- row 1: ranks, messages, voice --------------------------------
    (x_ranks, w_ranks), (x_messages, w_messages), (x_voice, w_voice) = columns3
    _card(draw, x_ranks, row1_top, w_ranks, row1_h, "Server Ranks", "trophy")
    _rank_rows(
        draw,
        x_ranks + 18,
        row1_top + 64,
        w_ranks - 36,
        (
            ("Message", f"#{message_rank}" if message_rank else "—"),
            ("Voice", f"#{voice_rank}" if voice_rank else "—"),
        ),
    )

    message_rows = [(label, _significant(messages), "messages") for label, messages, _ in windows]
    voice_rows = [(label, _hours(seconds), "hours") for label, _, seconds in windows]
    _card(draw, x_messages, row1_top, w_messages, row1_h, "Messages", "hash")
    _metric_rows(draw, x_messages + 18, row1_top + 64, w_messages - 36, message_rows)
    _card(draw, x_voice, row1_top, w_voice, row1_h, "Voice Activity", "voice")
    _metric_rows(draw, x_voice + 18, row1_top + 64, w_voice - 36, voice_rows)

    # -- row 2: channels and the chart --------------------------------
    (x_channels, w_channels), (x_chart, w_chart) = columns2
    _card(draw, x_channels, row2_top, w_channels, row2_h, "Top Channels & Applications", "chart")
    _channel_rows(draw, x_channels + 20, row2_top + 64, w_channels - 40, top_channels)

    _card(draw, x_chart, row2_top, w_chart, row2_h, "Charts", None)
    legend_font = _font(21, "bold")
    legend = (("Message", GREEN), ("Voice", PINK))
    legend_width = sum(14 + 8 + draw.textlength(text, font=legend_font) + 22 for text, _ in legend)
    legend_x = x_chart + w_chart - 20 - legend_width
    for text, colour in legend:
        draw.ellipse(
            (legend_x, row2_top + 10, legend_x + 14, row2_top + 24), fill=colour
        )
        draw.text((legend_x + 22, row2_top + 17), text, font=legend_font, fill=TEXT, anchor="lm")
        legend_x += 14 + 8 + draw.textlength(text, font=legend_font) + 22

    px0, px1 = x_chart + 20, x_chart + w_chart - 20
    py0, py1 = row2_top + 66, row2_top + row2_h - 20
    for fraction in (0.0, 0.5, 1.0):
        gy = round(py1 - fraction * (py1 - py0))
        draw.line((px0, gy, px1, gy), fill=GRID, width=1)
    day_count = max(1, len(series))
    for index in range(0, day_count, 2):
        gx = px0 + (px1 - px0) * (index / (day_count - 1) if day_count > 1 else 0)
        draw.line((gx, py0, gx, py1), fill=GRID, width=1)

    if series:
        messages_series = [row[0] for row in series]
        voice_series = [row[1] for row in series]
        for values, colour in ((messages_series, GREEN), (voice_series, PINK)):
            if max(values) <= 0:
                continue
            points = _series_points(values, px0, px1, py0, py1)
            draw.line(points, fill=colour, width=3, joint="curve")

    # -- footer --------------------------------------------------------
    footer_font = _font(22)
    strong_font = _font(22, "bold")
    cursor = PADDING
    for chunk, font, colour in (
        ("Server Lookback: ", footer_font, MUTED),
        (f"Last {lookback_days} days", strong_font, TEXT),
        (" — ", footer_font, MUTED),
        ("Timezone: ", footer_font, MUTED),
        ("UTC", strong_font, TEXT),
    ):
        draw.text((cursor, footer_top + 14), chunk, font=font, fill=colour, anchor="lm")
        cursor += draw.textlength(chunk, font=font)

    tail = f"Powered by {brand}"
    tail_width = draw.textlength(tail, font=strong_font)
    prefix_width = draw.textlength("Powered by ", font=footer_font)
    tail_x = WIDTH - PADDING - tail_width
    draw.text((tail_x - prefix_width, footer_top + 14), "Powered by ", font=footer_font, fill=MUTED, anchor="lm")
    draw.text((tail_x, footer_top + 14), brand, font=strong_font, fill=TEXT, anchor="lm")

    return canvas
