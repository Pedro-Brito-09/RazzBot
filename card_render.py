"""Image cards drawn with Pillow, in the Party Chaos menu style.

The designs are authored as HTML/CSS (torn paper strips, chunky outlined
lettering, a sky scene) and redrawn here by hand, because a headless browser
doesn't fit in the VPS's memory budget. Coordinates are written in the
design's CSS pixels and scaled by SCALE when drawn, so the numbers can be
checked against the HTML template directly.

Everything here is synchronous and CPU-bound: call it through
asyncio.to_thread so a render never blocks the gateway.
"""

import functools
import io
import math
import os
import threading
from datetime import datetime, timezone

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
SCALE = 2
# Polygons and rounded corners are drawn this many times larger and scaled
# down, since Pillow draws them without antialiasing.
SUPERSAMPLE = 4

DISPLAY = "LuckiestGuy-Regular.ttf"
HAND = "PatrickHand-Regular.ttf"
WHITE = (255, 255, 255, 255)
# rgba(20,20,60,0.25), the design's hard drop shadow.
SHADOW = (20, 20, 60, 64)

# Torn-edge outlines from the template, as clip-path percentages.
CLIP_BANNER = ((2, 0), (98, 0), (100, 18), (97, 40), (100, 62), (98, 100),
               (2, 100), (0, 80), (2.5, 58), (0, 36), (2, 14))
CLIP_STARS = ((2, 0), (98, 0), (100, 18), (97.5, 40), (100, 62), (98, 100),
              (2, 100), (0, 80), (2.5, 58), (0, 36), (2, 14))
CLIP_TAG = ((3, 0), (97, 0), (100, 20), (96, 45), (100, 70), (97, 100),
            (3, 100), (0, 78), (4, 52), (0, 26))
CLIP_MEDAL = ((4, 0), (96, 0), (100, 22), (95, 48), (100, 74), (96, 100),
              (4, 100), (0, 76), (5, 50), (0, 24))
CLIP_STAT = ((3, 0), (97, 0), (100, 20), (96.5, 45), (100, 70), (97, 100),
             (3, 100), (0, 78), (3.5, 52), (0, 26))


def px(value):
    """Design pixels to output pixels."""
    return round(value * SCALE)


def rgba(hex_color, alpha=255):
    hex_color = hex_color.lstrip("#")
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16),
            int(hex_color[4:6], 16), alpha)


@functools.lru_cache(maxsize=None)
def load_font(face, size):
    """A font from assets/fonts. `face` is a file, or (file, named weight)
    for a variable font, e.g. ("effects/JosefinSans-Variable.ttf", "Bold")."""
    file, weight = face if isinstance(face, tuple) else (face, None)
    font = ImageFont.truetype(os.path.join(ASSETS, "fonts", file), px(size))
    if weight:
        font.set_variation_by_name(weight)
    return font


@functools.lru_cache(maxsize=None)
def load_icon(folder, name, size):
    path = os.path.join(ASSETS, folder, f"{name}.png")
    with Image.open(path) as image:
        return image.convert("RGBA").resize((px(size), px(size)), Image.LANCZOS)


class Text:
    """One line of text laid out the way the browser would.

    Sizes are CSS pixels. `stroke` is the -webkit-text-stroke width; with
    paint-order: stroke fill only its outer half shows, so half is drawn.
    `shadow` is a hard text-shadow straight down, in the stroke colour.
    """

    def __init__(self, text, face, size, *, line_height=None, fill=WHITE,
                 stroke=0, stroke_color=None, shadow=0):
        self.text = text
        self.font = load_font(face, size)
        self.fill = fill
        self.stroke = stroke
        self.stroke_color = stroke_color
        self.shadow = shadow
        ascent, descent = self.font.getmetrics()
        content = (ascent + descent) / SCALE
        self.width = self.font.getlength(text) / SCALE
        self.height = size * line_height if line_height else content
        # The line box centres the font's content area, as CSS half-leading does.
        self.baseline = (self.height - content) / 2 + ascent / SCALE
        # How far right of the box's left edge the pen starts, for fonts
        # whose letters reach left of their origin.
        self.x_offset = 0

    def draw(self, draw, x, y):
        """Draw with the line box's top-left at design point (x, y)."""
        origin = (px(x + self.x_offset), px(y + self.baseline))
        width = round(self.stroke * SCALE / 2)
        if self.shadow:
            draw.text((origin[0], origin[1] + px(self.shadow)), self.text,
                      font=self.font, fill=self.stroke_color, anchor="ls",
                      stroke_width=width, stroke_fill=self.stroke_color)
        draw.text(origin, self.text, font=self.font, fill=self.fill,
                  anchor="ls", stroke_width=width, stroke_fill=self.stroke_color)


def shape_mask(width, height, paint):
    """An antialiased L mask; `paint(draw, k)` draws at k output pixels per design pixel."""
    k = SCALE * SUPERSAMPLE
    big = Image.new("L", (round(width * k), round(height * k)), 0)
    paint(ImageDraw.Draw(big), k)
    return big.resize((px(width), px(height)), Image.LANCZOS)


# Masks are only ever read, and most shapes repeat card to card (the eight
# stat strips are one shape), so each is drawn at 8x and scaled down once.
@functools.lru_cache(maxsize=128)
def polygon_mask(width, height, clip):
    def paint(draw, k):
        draw.polygon([(x / 100 * width * k, y / 100 * height * k)
                      for x, y in clip], fill=255)
    return shape_mask(width, height, paint)


@functools.lru_cache(maxsize=32)
def rounded_mask(width, height, radius):
    def paint(draw, k):
        draw.rounded_rectangle((0, 0, width * k - 1, height * k - 1),
                               radius=radius * k, fill=255)
    return shape_mask(width, height, paint)


def clip_to(image, mask):
    """Multiply the image's alpha by the mask, like CSS clip-path."""
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    return image


def with_drop_shadow(body, offset, pad=12):
    """Pad the body and put the design's hard shadow `offset` px below it."""
    pad_px = px(pad)
    layer = Image.new("RGBA", (body.width + 2 * pad_px, body.height + 2 * pad_px),
                      (0, 0, 0, 0))
    if offset:
        shadow = Image.new("RGBA", body.size, SHADOW[:3] + (0,))
        shadow.putalpha(body.getchannel("A").point(lambda a: a * SHADOW[3] // 255))
        layer.alpha_composite(shadow, (pad_px, pad_px + px(offset)))
    layer.alpha_composite(body, (pad_px, pad_px))
    return layer


def place(canvas, layer, left, top, width, height, rotate=0):
    """Composite a layer so its centre lands on the element box's centre.

    The layer may be padded (for shadows) but must be padded evenly, so its
    centre is the element's. CSS rotates clockwise, Pillow anticlockwise.
    """
    if rotate:
        layer = layer.rotate(-rotate, resample=Image.BICUBIC, expand=True)
    cx, cy = px(left + width / 2), px(top + height / 2)
    canvas.alpha_composite(layer, (cx - layer.width // 2, cy - layer.height // 2))


def strip(width, height, color, clip, draw_content=None):
    """A torn paper strip: a coloured box clipped to a ragged outline."""
    body = Image.new("RGBA", (px(width), px(height)), color)
    if draw_content:
        draw_content(body, ImageDraw.Draw(body))
    return clip_to(body, polygon_mask(width, height, clip))


def paste_icon(image, icon, x, y):
    image.alpha_composite(icon, (px(x), px(y)))


# ---------------------------------------------------------------- formatting

def format_count(value):
    """Thousands separators, one decimal only when there is one."""
    if value is None:
        return "—"
    if isinstance(value, float):
        value = round(value, 1)
        if value.is_integer():
            value = int(value)
    return f"{value:,}"


def compact_count(value):
    """1.2M / 34.5K, for numbers too wide for their strip."""
    if not isinstance(value, (int, float)):
        return format_count(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            short = f"{value / size:.1f}".rstrip("0").rstrip(".")
            return f"{short}{suffix}"
    return format_count(value)


def format_date(timestamp):
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    return f"{moment:%b} {moment.day}, {moment.year}"


def format_ago(timestamp, now=None):
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    seconds = max(0, int(now - timestamp))
    for size, unit in ((365 * 86400, "year"), (30 * 86400, "month"),
                       (7 * 86400, "week"), (86400, "day"), (3600, "hour"),
                       (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def fit_text(text, face, size, max_width, *, min_size, **style):
    """The largest size from `size` down to `min_size` that fits, or None."""
    while size >= min_size:
        candidate = Text(text, face, size, **style)
        if candidate.width <= max_width:
            return candidate
        size -= 1
    return None


# ---------------------------------------------------------------- profile card

CARD_WIDTH, CARD_HEIGHT = 960, 560

# (icon, label, strip colour, outline colour), in grid order.
PROFILE_STATS = (
    ("gamepad", "games played", "#6CC4F4", "#2A78B8"),
    ("trophy", "wins", "#FFD64F", "#A87A00"),
    ("fire", "win streak", "#FFA851", "#C4610F"),
    ("flag", "flags reached", "#FF7A6B", "#C23A2E"),
    ("swords", "kills", "#A57CF7", "#5E37B8"),
    ("skull", "deaths", "#8D9BB8", "#3E4A66"),
    ("trend", "furthest round", "#8BE04E", "#3F8F1E"),
    # Like the others: a light, bright strip in the hue of the icon's outline
    # (#9A4A06), with a mid shade of it outlining the text.
    ("calendar", "login streak", "#F6B85A", "#B6670C"),
)

# (icon, strip colour, outline colour), in the card's left-to-right order.
PROFILE_MEDALS = (
    ("medal-gold", "#FFD64F", "#A87A00"),
    ("medal-silver", "#B3B8C2", "#5E6573"),
    ("medal-bronze", "#D98A5B", "#8F4A1F"),
    ("medal-diamond", "#A988D9", "#5E3C93"),
)


def profile_icon(name, size):
    return load_icon("profile", name, size)


# Roblox's classic chat name colours, in the order its hash indexes them.
ROBLOX_NAME_COLORS = (
    (253, 41, 67),     # Bright red
    (1, 162, 255),     # Bright blue
    (2, 184, 87),      # Earth green
    (107, 50, 124),    # Bright violet
    (218, 133, 65),    # Bright orange
    (245, 205, 48),    # Bright yellow
    (232, 186, 200),   # Light reddish violet
    (215, 197, 154),   # Brick yellow
)


def roblox_name_color(username):
    """The colour Roblox's classic chat gave this username.

    A port of the legacy chat's GetNameValue: character codes summed, with
    the sign flipping every two characters counted from the end.
    """
    data = str(username).encode("utf-8")
    value = 0
    for index, code in enumerate(data, start=1):
        reverse_index = len(data) - index + 1
        if len(data) % 2 == 1:
            reverse_index -= 1
        value += -code if reverse_index % 4 >= 2 else code
    # Python's % is floored like Lua's, so negative sums land the same way.
    return ROBLOX_NAME_COLORS[value % len(ROBLOX_NAME_COLORS)]


def shade(color, factor):
    """Darken (factor < 1) towards black or lighten (> 1) towards white."""
    if factor <= 1:
        return tuple(round(c * factor) for c in color[:3])
    mix = factor - 1
    return tuple(round(c + (255 - c) * mix) for c in color[:3])


def vertical_gradient(width, height, stops):
    """An RGBA image fading through (position 0-1, rgba) stops, top to bottom."""
    column = Image.new("RGBA", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0
                column.putpixel((0, y), tuple(round(a + (b - a) * f)
                                              for a, b in zip(c0, c1)))
                break
    return column.resize((width, height))


def draw_background(canvas, color, image=None):
    """Fill the card: a map thumbnail, or a gradient of the player's colour.

    Returns the colour the footer text should be outlined in.
    """
    width, height = px(CARD_WIDTH), px(CARD_HEIGHT)
    # The colour goes down first in both cases, so a thumbnail with
    # transparent areas shows it rather than holes in the card.
    canvas.alpha_composite(vertical_gradient(width, height, (
        (0.0, shade(color, 0.85) + (255,)),
        (0.62, color[:3] + (255,)),
        (1.0, shade(color, 1.35) + (255,)),
    )))
    if image is not None:
        # Cover: scale to fill, crop the overflow evenly.
        scale = max(width / image.width, height / image.height)
        size = (max(width, round(image.width * scale)),
                max(height, round(image.height * scale)))
        cover = image.convert("RGBA").resize(size, Image.LANCZOS)
        left, top = (cover.width - width) // 2, (cover.height - height) // 2
        canvas.alpha_composite(cover.crop((left, top, left + width, top + height)))
        # Darken the bottom edge so the footer reads over any scene.
        canvas.alpha_composite(vertical_gradient(width, height, (
            (0.0, (0, 0, 0, 0)), (0.7, (0, 0, 0, 0)), (1.0, (0, 0, 0, 120)),
        )))
        return (16, 18, 30, 255)

    canvas.alpha_composite(dot_pattern(width, height, shade(color, 0.6) + (64,)))
    return shade(color, 0.4) + (255,)


# Staggered polka dots: one at the tile's corner, one in its middle.
DOT_TILE = 28
DOT_RADIUS = 3.2


def dot_pattern(width, height, fill):
    """A full-size RGBA layer of staggered dots, tiled from one antialiased tile."""
    def paint(draw, k):
        for cx, cy in ((0, 0), (DOT_TILE, 0), (0, DOT_TILE), (DOT_TILE, DOT_TILE),
                       (DOT_TILE / 2, DOT_TILE / 2)):
            draw.ellipse(((cx - DOT_RADIUS) * k, (cy - DOT_RADIUS) * k,
                          (cx + DOT_RADIUS) * k, (cy + DOT_RADIUS) * k), fill=255)
    tile = Image.new("RGBA", (px(DOT_TILE), px(DOT_TILE)), fill[:3] + (0,))
    tile.putalpha(shape_mask(DOT_TILE, DOT_TILE, paint).point(lambda a: a * fill[3] // 255))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for x in range(0, width, tile.width):
        for y in range(0, height, tile.height):
            layer.paste(tile, (x, y))
    return layer


def draw_avatar(canvas, avatar):
    """The white-framed avatar tile, tilted, with a placeholder if missing."""
    outer, border, radius = 154, 7, 24
    tile = Image.new("RGBA", (px(outer), px(outer)), WHITE)
    tile = clip_to(tile, rounded_mask(outer, outer, radius))
    inner_size = outer - 2 * border
    inner = Image.new("RGBA", (px(inner_size), px(inner_size)), rgba("#0F1B33"))
    if avatar is not None:
        side = min(avatar.size)
        left, top = (avatar.width - side) // 2, (avatar.height - side) // 2
        face = avatar.crop((left, top, left + side, top + side))
        inner.alpha_composite(face.resize(inner.size, Image.LANCZOS))
    else:
        mark = Text("?", DISPLAY, 72, line_height=1, stroke=8,
                    stroke_color=rgba("#2A3B5E"))
        mark.draw(ImageDraw.Draw(inner), (inner_size - mark.width) / 2,
                  (inner_size - mark.height) / 2 + 4)
    inner = clip_to(inner, rounded_mask(inner_size, inner_size, radius - border))
    tile.alpha_composite(inner, (px(border), px(border)))
    place(canvas, with_drop_shadow(tile, 7), 40, 64, outer, outer, rotate=-5)


def draw_stars_strip(canvas, stars, stars_all_time):
    outline = rgba("#C4610F")
    value = Text(format_count(stars), DISPLAY, 40, line_height=1, stroke=8,
                 stroke_color=outline, shadow=4)
    caption = Text(f"stars · {format_count(stars_all_time)} all time", HAND, 19,
                   line_height=1.1, stroke=4, stroke_color=outline)
    column = value.height + caption.height
    inner = max(56, column)
    width = 16 + 56 + 12 + max(value.width, caption.width) + 24
    height = 10 + inner + 10

    def content(image, draw):
        paste_icon(image, profile_icon("star", 56), 16, 10 + (inner - 56) / 2)
        top = 10 + (inner - column) / 2
        value.draw(draw, 16 + 56 + 12, top)
        caption.draw(draw, 16 + 56 + 12, top + value.height)

    left = CARD_WIDTH - 40 - width
    body = strip(width, height, rgba("#FFA851"), CLIP_STARS, content)
    place(canvas, with_drop_shadow(body, 5), left, 34, width, height, rotate=2)
    return left


# The name banner's text box, in the card's own font at its full size.
NAME_SIZE = 52


def _cap_height(font):
    """Height of a capital H above the baseline, in design pixels."""
    return -font.getbbox("H", anchor="ls")[1] / SCALE


def fit_name_in_face(text, face, room, style):
    """The name in another font, sized to sit in the card's name box.

    Fonts differ wildly in proportions, so rather than reuse the card
    font's size, the face is scaled until its capitals stand as tall as
    the card font's do, then shrunk until the name fits the width and its
    ink fits the box's height. The ink is centred in the box, so a font
    with tall flourishes or drips doesn't sit lopsided. Returns None when
    even the smallest size is too wide.
    """
    target_cap = _cap_height(load_font(DISPLAY, NAME_SIZE))
    for size in range(int(NAME_SIZE * 1.6), 19, -1):
        name = Text(text, face, size, line_height=1, **style)
        if _cap_height(name.font) > target_cap * 1.02:
            continue
        left, top, right, bottom = (v / SCALE for v in name.font.getbbox(text, anchor="ls"))
        ink_height = bottom - top
        # Italic and streaked faces (Faster One) reach left of the pen; start
        # the pen that much further in so nothing slides under the avatar.
        overhang = max(0.0, -left)
        width = max(name.width, right) + overhang
        if width > room or ink_height > NAME_SIZE + 4:
            continue
        # Lay it out in the card font's box, ink centred vertically.
        name.width = width
        name.height = NAME_SIZE
        name.baseline = (NAME_SIZE - ink_height) / 2 - top
        name.x_offset = overhang
        return name
    return None


def draw_name_banner(canvas, username, max_right, with_text=True, face=DISPLAY):
    """The purple name strip. Long names shrink, then lose their tail.

    `face` swaps the name's font -- a username effect plays in the font the
    game gives it. Returns where it landed, so an animated card can redraw
    the name alone.
    """
    outline = rgba("#5E37B8")
    room = max_right - 130 - 74 - 36
    style = dict(line_height=1, stroke=8, stroke_color=outline, shadow=5)
    if face == DISPLAY:
        name = fit_text(username, DISPLAY, NAME_SIZE, room, min_size=30, **style)
    else:
        no_line_height = {k: v for k, v in style.items() if k != "line_height"}
        name = fit_name_in_face(username, face, room, no_line_height)
    if name is None:
        # Too long even small: the card font, cut short.
        text = username
        while text and Text(text + "…", DISPLAY, 30, **style).width > room:
            text = text[:-1]
        name = Text(text.rstrip() + "…", DISPLAY, 30, **style)
    width = 74 + name.width + 36
    height = 12 + name.height + 6
    body = strip(width, height, rgba("#A57CF7"), CLIP_BANNER,
                 (lambda image, draw: name.draw(draw, 74, 12)) if with_text else None)
    top = 86 + (52 - name.height) / 2
    layer = with_drop_shadow(body, 5)
    place(canvas, layer, 130, top, width, height)
    # Where place() put the strip itself, inside the padded shadow layer.
    x0 = px(130 + width / 2) - layer.width // 2 + px(12)
    y0 = px(top + height / 2) - layer.height // 2 + px(12)
    return {
        "name": name,
        "box": (x0, y0, x0 + body.width, y0 + body.height),
        "mask": polygon_mask(width, height, CLIP_BANNER),
        "text_at": (74, 12),
        "outline": outline,
    }


def draw_tags(canvas, tags):
    """The small pills under the name: (text, fill, outline, icon name or None)."""
    left = 200
    for text, fill, outline, icon in tags:
        label = Text(text, DISPLAY, 20, stroke=5, stroke_color=rgba(outline))
        if icon:
            # padding 4px 18px 4px 12px, a 30px icon, 6px gap, text nudged 3px down.
            inner = max(30, label.height + 3)
            width = 12 + 30 + 6 + label.width + 18
            height = 4 + inner + 4

            def content(image, draw, label=label, inner=inner, icon=icon):
                paste_icon(image, profile_icon(icon, 30), 12, 4 + (inner - 30) / 2)
                label.draw(draw, 12 + 30 + 6, 4 + (inner - label.height - 3) / 2 + 3)
        else:
            width = 18 + label.width + 18
            height = 7 + label.height + 4

            def content(image, draw, label=label):
                label.draw(draw, 18, 7)
        body = strip(width, height, rgba(fill), CLIP_TAG, content)
        place(canvas, with_drop_shadow(body, 4), left, 170, width, height)
        left += width + 10


def draw_medals(canvas, counts):
    pills = []
    for (icon, fill, outline), count in zip(PROFILE_MEDALS, counts):
        label = Text(format_count(count), DISPLAY, 24, stroke=6,
                     stroke_color=rgba(outline))
        pills.append((icon, fill, label))
    inner = max(24, max(label.height for _, _, label in pills) + 3)
    height = 8 + inner + 8
    widths = [14 + 34 + 7 + label.width + 14 for _, _, label in pills]
    left = CARD_WIDTH - 40 - (sum(widths) + 10 * (len(pills) - 1))
    for (icon, fill, label), width in zip(pills, widths):
        def content(image, draw, icon=icon, label=label):
            # A 42px medal with -9px/-4px margins: it spills over the strip's
            # edges and gets clipped by them, as in the design.
            paste_icon(image, profile_icon(icon, 42), 14 - 4, 8 + (inner - 24) / 2 - 9)
            label.draw(draw, 14 + 34 + 7, 8 + (inner - label.height - 3) / 2 + 3)
        body = strip(width, height, rgba(fill), CLIP_MEDAL, content)
        place(canvas, with_drop_shadow(body, 4), left, 190, width, height)
        left += width + 10


def stat_value_text(value, outline, room):
    """The stat number at the largest size that fits, compacting if needed."""
    style = dict(line_height=1, stroke=7, stroke_color=outline)
    text = value if isinstance(value, str) else format_count(value)
    fitted = fit_text(text, DISPLAY, 30, room, min_size=22, **style)
    if fitted is None and not isinstance(value, str):
        fitted = fit_text(compact_count(value), DISPLAY, 30, room, min_size=18, **style)
    return fitted or Text(text, DISPLAY, 18, **style)


def draw_stat_grid(canvas, values):
    columns, gap_x, gap_y, cell_height = 4, 14, 18, 96
    cell_width = (CARD_WIDTH - 80 - gap_x * (columns - 1)) / columns
    for index, ((icon, label, fill, outline), value) in enumerate(zip(PROFILE_STATS, values)):
        dark = rgba(outline)
        number = stat_value_text(value, dark, cell_width - 14 - 54 - 10 - 14)
        caption = Text(label, HAND, 18, line_height=1.1, stroke=4, stroke_color=dark)

        def content(image, draw, number=number, caption=caption, icon=icon):
            paste_icon(image, profile_icon(icon, 54), 14, (cell_height - 54) / 2)
            top = (cell_height - number.height - caption.height) / 2
            number.draw(draw, 14 + 54 + 10, top)
            caption.draw(draw, 14 + 54 + 10, top + number.height)

        body = strip(cell_width, cell_height, rgba(fill), CLIP_STAT, content)
        row, column = divmod(index, columns)
        left = 40 + column * (cell_width + gap_x)
        top = 264 + row * (cell_height + gap_y)
        place(canvas, with_drop_shadow(body, 5), left, top, cell_width, cell_height)


def draw_footer(canvas, left_text, right_text, dark):
    """White footer text with a hard shadow and a soft glow behind it."""
    parts = [Text(t, HAND, 20) for t in (left_text, right_text)]
    top = CARD_HEIGHT - 20 - max(p.height for p in parts)
    positions = [(40, top), (CARD_WIDTH - 40 - parts[1].width, top)]

    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    for part, (x, y) in zip(parts, positions):
        glow_draw.text((px(x), px(y + part.baseline)), part.text, font=part.font,
                       fill=dark, anchor="ls")
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(px(3))))
    draw = ImageDraw.Draw(canvas)
    for part, (x, y) in zip(parts, positions):
        draw.text((px(x), px(y + part.baseline + 2)), part.text, font=part.font,
                  fill=dark, anchor="ls")
        part.draw(draw, x, y)


def open_image(data):
    """Decode image bytes to RGBA, or None for missing or unreadable data."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.convert("RGBA")
    except Exception:
        return None


def compose_profile_card(*, username, avatar_png=None, tag=None, stars=0,
                         stars_all_time=0, medals=(0, 0, 0, 0), games_played=0,
                         wins=None, win_streak=0, flags_reached=0, kills=0,
                         deaths=0, furthest_round=0, login_streak=0,
                         login_streak_best=0, user_id=None, joined=None,
                         last_seen=None, background_png=None, with_name=True,
                         name_face=DISPLAY):
    """The card as two layers either side of the name: (below, above, banner).

    The avatar tile overlaps the name banner, so an animated name has to go
    between them. With with_name the name is drawn into `below` as usual.
    """
    avatar = open_image(avatar_png)
    background = open_image(background_png)
    size = (px(CARD_WIDTH), px(CARD_HEIGHT))

    below = Image.new("RGBA", size, (0, 0, 0, 0))
    footer_outline = draw_background(below, roblox_name_color(username), background)
    stars_left = draw_stars_strip(below, stars, stars_all_time)
    banner = draw_name_banner(below, username, stars_left - 16, with_text=with_name,
                              face=name_face)

    above = Image.new("RGBA", size, (0, 0, 0, 0))
    draw_avatar(above, avatar)
    if tag:
        draw_tags(above, [(str(tag).upper(), "#C9A6FF", "#7A52CC", None)])
    draw_medals(above, medals)
    draw_stat_grid(above, [
        games_played, wins, win_streak, flags_reached, kills, deaths,
        furthest_round, f"{format_count(login_streak)} / {format_count(login_streak_best)}",
    ])
    left = [f"ID {user_id}"] if user_id is not None else []
    if joined is not None:
        left.append(f"joined {format_date(joined)}")
    right = f"last seen {format_ago(last_seen)}" if last_seen is not None else ""
    draw_footer(above, " · ".join(left), right, footer_outline)

    corners = rounded_mask(CARD_WIDTH, CARD_HEIGHT, 24)
    return clip_to(below, corners), clip_to(above, corners), banner


def render_profile_card(**fields):
    """The /profile card as PNG bytes.

    `medals` is (gold, silver, bronze, diamond). `joined` and `last_seen`
    are Unix timestamps or None. `wins` is None when the board was down.
    `background_png` (a map thumbnail) replaces the backdrop, which is
    otherwise the username's Roblox chat colour.
    """
    below, above, _ = compose_profile_card(**fields)
    below.alpha_composite(above)
    out = io.BytesIO()
    below.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


# ---------------------------------------------------------------- animated name

# One animated render at a time: its frames are held in memory until the
# encoder has them all, and the VPS caps the bot at 400 MB.
ANIMATION_LOCK = threading.Lock()
# Frame counts and per-frame delays tried when fitting a seamless loop.
LOOP_FRAMES = range(20, 49)
LOOP_DELAYS_MS = (50, 60, 70, 80, 90, 100)
# A motion that would turn less than this many times even in the longest
# loop is held still: rounding it up to a whole turn would at least double
# its speed, which looks further from the game than not moving.
MIN_LOOP_CYCLES = 0.6
# Effect gradients are smooth, so they're computed at this fraction of the
# card's resolution and scaled up.
GRADIENT_RESOLUTION = 0.5
# Sequence lookup-table size.
GRADIENT_SAMPLES = 512
# Animated WebP quality; the card is flat colour, so it compresses well.
WEBP_QUALITY = 88
# libwebp's speed/size trade-off, 0-6. 2 is as sharp as 4 here at ~60% of
# the time; the file is a few percent bigger.
WEBP_METHOD = 2


def _sequence_table(stops, kind):
    """GRADIENT_SAMPLES values of a keypoint sequence, wrapping like ezVisualz.

    ezVisualz scrolls a gradient by moving every keypoint along and wrapping
    it round, so the sequence behaves as if it repeats: past the last
    keypoint it carries on from the first one again.
    """
    points = sorted(stops)
    extended = ([(points[-1][0] - 1, points[-1][1])] + points
                + [(points[0][0] + 1, points[0][1])])
    table = []
    for i in range(GRADIENT_SAMPLES):
        u = i / GRADIENT_SAMPLES
        for (t0, v0), (t1, v1) in zip(extended, extended[1:]):
            if t0 <= u < t1:
                f = (u - t0) / (t1 - t0)
                if kind == "color":
                    table.append(bytes(round(a + (b - a) * f) for a, b in zip(v0, v1)))
                else:
                    # Transparency 0-1 -> opacity 255-0.
                    table.append(bytes((round((1 - (v0 + (v1 - v0) * f)) * 255),)))
                break
    return table


def _gradient_image(table, mode, angle, offset, box, size):
    """A UIGradient-like fill for a `size` image: colours from `table`, along
    `angle` (degrees clockwise), scrolled by `offset`, spanning `box`.

    Like a UIGradient it spans the label's box, not the image, so a stroke
    spilling outside the box gets the end colours.
    """
    k = GRADIENT_RESOLUTION
    width, height = max(1, round(size[0] * k)), max(1, round(size[1] * k))
    bx0, by0, bx1, by1 = (v * k for v in box)
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    theta = math.radians(angle)
    # Half the box's extent along the gradient's direction.
    reach = max(1.0, (bx1 - bx0) / 2 * abs(math.cos(theta))
                + (by1 - by0) / 2 * abs(math.sin(theta)))
    samples = len(table)
    if angle % 360 == 0:
        # Not tilted: one row across the image, no rotation needed.
        row = bytearray()
        for x in range(width):
            t = min(1.0, max(0.0, (x + 0.5 - cx + reach) / (2 * reach)))
            row += table[int(((t - offset) % 1.0) * samples) % samples]
        line = Image.frombytes(mode, (width, 1), bytes(row))
        return line.resize(size, Image.BILINEAR)

    far = max(math.hypot(x - cx, y - cy) for x in (0, width) for y in (0, height))
    side = int(far * 2) + 4

    row = bytearray()
    for x in range(side):
        t = min(1.0, max(0.0, (x + 0.5 - side / 2 + reach) / (2 * reach)))
        row += table[int(((t - offset) % 1.0) * samples) % samples]
    line = Image.frombytes(mode, (side, 1), bytes(row))
    square = line.resize((side, side), Image.NEAREST).rotate(-angle, resample=Image.BILINEAR)
    left, top = round(side / 2 - cx), round(side / 2 - cy)
    crop = square.crop((left, top, left + width, top + height))
    return crop.resize(size, Image.BILINEAR)


def _loop_rates(effect):
    """Every repeating motion in the effect, as (key, cycles per second)."""
    rates = []
    for part in ("fill", "stroke"):
        layer = effect.get(part)
        if not layer:
            continue
        rates.append(((part, "speed"), layer["speed"]))
        if layer["transparency"]:
            rates.append(((part, "transparency_speed"), layer["transparency_speed"]))
        angle = layer["angle"]
        if isinstance(angle, tuple) and angle[0] == "spin":
            rates.append(((part, "spin"), angle[2] / 360))
        elif isinstance(angle, tuple) and angle[0] == "wobble":
            rates.append(((part, "wobble"), angle[3] / (2 * math.pi)))
    if effect.get("pulse"):
        rates.append((("pulse", "pulse"), effect["pulse"][2] / (2 * math.pi)))
    return [(key, rate) for key, rate in rates if rate]


def plan_loop(effect):
    """Frames, delay (ms) and whole-cycle counts per motion for a seamless loop.

    Each motion is rounded to a whole number of cycles over the loop (or
    held still if it would barely move), picking the frame count and delay
    that change the effect's speeds the least.
    """
    rates = _loop_rates(effect)
    # A motion that can't get far even in the longest loop is held still;
    # every other one has to move, whatever loop length is picked.
    longest = max(LOOP_FRAMES) * max(LOOP_DELAYS_MS) / 1000
    moving = {key for key, rate in rates if abs(rate) * longest >= MIN_LOOP_CYCLES}
    best = None
    for delay in LOOP_DELAYS_MS:
        for frames in LOOP_FRAMES:
            duration = frames * delay / 1000
            # Fewer frames save memory, but long delays look choppy; the
            # latter matters more.
            cost, cycles = frames * 0.001 + (delay - 50) * 0.002, {}
            for key, rate in rates:
                if key not in moving:
                    cycles[key] = 0
                    continue
                actual = abs(rate) * duration
                whole = max(1, round(actual))
                cost += abs(whole - actual) / actual
                cycles[key] = whole if rate > 0 else -whole
            if best is None or cost < best[0]:
                best = (cost, frames, delay, cycles)
    _, frames, delay, cycles = best
    return frames, delay, cycles


def _layer_state(layer, part, cycles, phase):
    """Angle, colour offset and transparency offset at loop position `phase` (0-1)."""
    angle = layer["angle"]
    if isinstance(angle, tuple) and angle[0] == "spin":
        angle = angle[1] + 360 * cycles.get((part, "spin"), 0) * phase
    elif isinstance(angle, tuple) and angle[0] == "wobble":
        _, centre, amplitude, _ = angle
        angle = centre + amplitude * math.sin(
            2 * math.pi * cycles.get((part, "wobble"), 0) * phase)
    offset = layer["offset"] + cycles.get((part, "speed"), 0) * phase
    transparency_offset = cycles.get((part, "transparency_speed"), 0) * phase
    return angle, offset, transparency_offset


def _stroke_factor(effect, cycles, phase):
    """The card outline's width multiplier for a pulsing stroke.

    ezVisualz pulses a thin in-game stroke by up to 3x; the card's outline
    is already chunky, so the swing is toned down to stay readable.
    """
    pulse = effect.get("pulse")
    if not pulse:
        return 1.0
    low, high, _ = pulse
    wave = (math.sin(2 * math.pi * cycles.get(("pulse", "pulse"), 0) * phase) + 1) / 2
    multiplier = low + (high - low) * wave
    return min(1.8, max(0.6, 1 + (multiplier - 1) * 0.35))


class NameAnimator:
    """Draws the name banner's text for one frame of a username effect."""

    def __init__(self, banner, effect, cycles):
        self.effect = effect
        self.cycles = cycles
        name = banner["name"]
        self.name = name
        x0, y0, x1, y1 = banner["box"]
        self.size = (x1 - x0, y1 - y0)
        self.clip = banner["mask"]
        tx, ty = banner["text_at"]
        self.origin = (px(tx + name.x_offset), px(ty + name.baseline))
        self.stroke_width = round(name.stroke * SCALE / 2)
        # An effect's own outline is as thick as the game makes it (its UIStroke
        # Thickness, in pixels) -- the card's chunky outline would fill in the
        # fine detail of faces like Faster One or Monoton.
        if effect.get("stroke") and effect.get("size"):
            self.stroke_width = max(1, round(effect["size"] * SCALE))
        # The label's box, which the gradients span.
        self.text_box = (px(tx), px(ty), px(tx + name.width), px(ty + name.height))
        outline = banner["outline"]

        self.fill_mask = self._mask(0)
        self.full_mask = self._mask(self.stroke_width)
        # Under every effect: the card's own shadow and purple outline, so
        # transparent parts of an effect fall back to the normal look.
        base = Image.new("RGBA", self.size, (0, 0, 0, 0))
        base.paste(outline, (0, 0), self._mask(self.stroke_width, dy=px(name.shadow)))
        base.paste(outline, (0, 0), self.full_mask)
        if not effect.get("fill"):
            base.paste(WHITE, (0, 0), self.fill_mask)
        self.base = base

        self.tables = {}
        for part in ("fill", "stroke"):
            layer = effect.get(part)
            if layer:
                self.tables[part] = (
                    _sequence_table(layer["colors"], "color"),
                    _sequence_table(layer["transparency"], "alpha")
                    if layer["transparency"] else None,
                )

    def _mask(self, stroke_width, dy=0):
        mask = Image.new("L", self.size, 0)
        ImageDraw.Draw(mask).text(
            (self.origin[0], self.origin[1] + dy), self.name.text, font=self.name.font,
            fill=255, anchor="ls", stroke_width=stroke_width, stroke_fill=255,
        )
        return mask

    def _paint(self, image, part, mask, phase):
        layer = self.effect[part]
        colors, alphas = self.tables[part]
        angle, offset, transparency_offset = _layer_state(layer, part, self.cycles, phase)
        color = _gradient_image(colors, "RGB", angle, offset, self.text_box, self.size)
        if alphas is not None:
            mask = ImageChops.multiply(mask, _gradient_image(
                alphas, "L", angle, transparency_offset, self.text_box, self.size))
        image.paste(color, (0, 0), mask)

    def frame(self, phase):
        """The text layer (banner-sized, clipped to the strip) at `phase` 0-1."""
        image = self.base.copy()
        if self.effect.get("stroke"):
            factor = _stroke_factor(self.effect, self.cycles, phase)
            width = max(1, round(self.stroke_width * factor))
            mask = self.full_mask if width == self.stroke_width else self._mask(width)
            self._paint(image, "stroke", mask, phase)
            if not self.effect.get("fill"):
                image.paste(WHITE, (0, 0), self.fill_mask)
        if self.effect.get("fill"):
            self._paint(image, "fill", self.fill_mask, phase)
        return clip_to(image, self.clip)


def _even_box(box, size):
    """Grow a box to even coordinates, so it halves exactly."""
    x0, y0, x1, y1 = box
    x0, y0 = x0 - x0 % 2, y0 - y0 % 2
    x1, y1 = min(size[0], x1 + x1 % 2), min(size[1], y1 + y1 % 2)
    return x0, y0, x1, y1


def render_profile_card_animated(effect, **fields):
    """The card with the username effect playing, as animated WebP bytes.

    WebP rather than GIF: it keeps the full colour of the gradients and the
    soft rounded corners, where GIF has 256 colours and on/off transparency.
    Rendered at half the PNG's resolution, since every frame is held in
    memory until encoded. Only the name banner changes between frames, so
    it's the only part redrawn.
    """
    with ANIMATION_LOCK:
        # The name plays in the font the game gives the effect.
        below, above, banner = compose_profile_card(
            with_name=False, name_face=effect.get("font") or DISPLAY, **fields)
        frames_count, delay, cycles = plan_loop(effect)
        animator = NameAnimator(banner, effect, cycles)

        full_size = below.size
        half_size = (full_size[0] // 2, full_size[1] // 2)
        region = _even_box(banner["box"], full_size)
        bx0, by0 = banner["box"][:2]
        below_region, above_region = below.crop(region), above.crop(region)
        still = below.copy()
        still.alpha_composite(above)
        still = still.resize(half_size, Image.LANCZOS)
        del below, above

        at = (region[0] // 2, region[1] // 2)
        patch_size = ((region[2] - region[0]) // 2, (region[3] - region[1]) // 2)
        frames = []
        for index in range(frames_count):
            patch = below_region.copy()
            patch.alpha_composite(animator.frame(index / frames_count),
                                  (bx0 - region[0], by0 - region[1]))
            patch.alpha_composite(above_region)
            frame = still.copy()
            frame.paste(patch.resize(patch_size, Image.LANCZOS), at)
            frames.append(frame)

        out = io.BytesIO()
        frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:],
                       duration=delay, loop=0, quality=WEBP_QUALITY, method=WEBP_METHOD,
                       # No periodic keyframes: only the name changes, so
                       # every frame after the first stores just that patch.
                       # The defaults re-encode the whole card every few
                       # frames -- 6x slower and 3-4x bigger, for nothing.
                       kmin=0, kmax=0)
        return out.getvalue()
