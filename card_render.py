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
        self.size = size
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


def with_drop_shadow(body, offset, pad=12, color=SHADOW):
    """Pad the body and put the design's hard shadow `offset` px below it."""
    pad_px = px(pad)
    layer = Image.new("RGBA", (body.width + 2 * pad_px, body.height + 2 * pad_px),
                      (0, 0, 0, 0))
    if offset:
        shadow = Image.new("RGBA", body.size, color[:3] + (0,))
        shadow.putalpha(body.getchannel("A").point(lambda a: a * color[3] // 255))
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
    x, y = cx - layer.width // 2, cy - layer.height // 2
    # Shapes hanging off the card's top or left edge: cut off what's outside,
    # since older Pillow refuses a negative destination.
    if x < 0 or y < 0:
        layer = layer.crop((max(0, -x), max(0, -y), layer.width, layer.height))
        x, y = max(0, x), max(0, y)
    canvas.alpha_composite(layer, (x, y))


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


def fit_text(text, face, size, max_width, *, min_size, **style):
    """The largest size from `size` down to `min_size` that fits, or None."""
    while size >= min_size:
        candidate = Text(text, face, size, **style)
        if candidate.width <= max_width:
            return candidate
        size -= 1
    return None


def ellipsize(text, face, size, max_width, **style):
    """The text at `size`, its tail cut off with an ellipsis until it fits."""
    if Text(text, face, size, **style).width <= max_width:
        return Text(text, face, size, **style)
    while text and Text(text + "…", face, size, **style).width > max_width:
        text = text[:-1]
    return Text(text.rstrip() + "…", face, size, **style)


# A codepoint no font here has, to learn what each face draws for a missing one.
_MISSING_PROBE = "\U0010FFFD"


@functools.lru_cache(maxsize=4096)
def _has_glyph(face, char):
    font = load_font(face, 20)
    missing = font.getmask(_MISSING_PROBE)
    mask = font.getmask(char)
    return mask.size != missing.size or bytes(mask) != bytes(missing)


def drawable_text(text, face):
    """The text minus characters the face can't draw (emoji, most scripts),
    so they don't come out as boxes. Whitespace is collapsed."""
    kept = "".join(c for c in str(text) if c.isspace() or _has_glyph(face, c))
    return " ".join(kept.split())


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


def cover_crop(image, width, height):
    """CSS object-fit: cover. Scale to fill (output pixels), crop the overflow evenly."""
    scale = max(width / image.width, height / image.height)
    size = (max(width, round(image.width * scale)),
            max(height, round(image.height * scale)))
    cover = image.convert("RGBA").resize(size, Image.LANCZOS)
    left, top = (cover.width - width) // 2, (cover.height - height) // 2
    return cover.crop((left, top, left + width, top + height))


def draw_background(canvas, color, image=None):
    """Fill the card: a map thumbnail, or a gradient of the player's colour.

    Returns the colour the footer text should be outlined in.
    """
    width, height = canvas.size
    # The colour goes down first in both cases, so a thumbnail with
    # transparent areas shows it rather than holes in the card.
    canvas.alpha_composite(vertical_gradient(width, height, (
        (0.0, shade(color, 0.85) + (255,)),
        (0.62, color[:3] + (255,)),
        (1.0, shade(color, 1.35) + (255,)),
    )))
    if image is not None:
        canvas.alpha_composite(cover_crop(image, width, height))
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


def framed_avatar(avatar, outer, border, radius, empty, mark_outline):
    """A square avatar in a rounded white frame, `outer` px across.

    `empty` fills the frame behind the picture (and shows through a
    transparent one); a missing avatar gets a question mark outlined in
    `mark_outline`.
    """
    tile = clip_to(solid(outer, outer, WHITE), rounded_mask(outer, outer, radius))
    inner_size = outer - 2 * border
    inner = solid(inner_size, inner_size, rgba(empty))
    if avatar is not None:
        side = min(avatar.size)
        left, top = (avatar.width - side) // 2, (avatar.height - side) // 2
        face = avatar.crop((left, top, left + side, top + side))
        inner.alpha_composite(face.resize(inner.size, Image.LANCZOS))
    else:
        # The profile's 72px mark in its 140px frame, kept in proportion.
        size = round(inner_size * 72 / 140)
        mark = Text("?", DISPLAY, size, line_height=1, stroke=size / 9,
                    stroke_color=rgba(mark_outline))
        mark.draw(ImageDraw.Draw(inner), (inner_size - mark.width) / 2,
                  (inner_size - mark.height) / 2 + size / 18)
    inner = clip_to(inner, rounded_mask(inner_size, inner_size, radius - border))
    tile.alpha_composite(inner, (px(border), px(border)))
    return tile


def draw_avatar(canvas, avatar):
    """The white-framed avatar tile, tilted, with a placeholder if missing."""
    tile = framed_avatar(avatar, 154, 7, 24, "#0F1B33", "#2A3B5E")
    place(canvas, with_drop_shadow(tile, 7), 40, 64, 154, 154, rotate=-5)


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


def fit_name_in_face(text, face, room, style, *, size=NAME_SIZE, min_size=20):
    """The name in another font, sized to sit in a `size`-tall name box.

    Fonts differ wildly in proportions, so rather than reuse the card
    font's size, the face is scaled until its capitals stand as tall as
    the card font's do, then shrunk until the name fits the width and its
    ink fits the box's height. The ink is centred in the box, so a font
    with tall flourishes or drips doesn't sit lopsided. Returns None when
    even the smallest size is too wide.
    """
    box = size
    target_cap = _cap_height(load_font(DISPLAY, box))
    for size in range(int(box * 1.6), min_size - 1, -1):
        name = Text(text, face, size, line_height=1, **style)
        if _cap_height(name.font) > target_cap * 1.02:
            continue
        left, top, right, bottom = (v / SCALE for v in name.font.getbbox(text, anchor="ls"))
        ink_height = bottom - top
        # Italic and streaked faces (Faster One) reach left of the pen; start
        # the pen that much further in so nothing slides under the avatar.
        overhang = max(0.0, -left)
        width = max(name.width, right) + overhang
        if width > room or ink_height > box + 4:
            continue
        # Lay it out in the card font's box, ink centred vertically.
        name.width = width
        name.height = box
        name.baseline = (box - ink_height) / 2 - top
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
        name = ellipsize(username, DISPLAY, 30, room, **style)
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


def stat_value_text(value, outline, room, size=30, min_size=22):
    """The stat number at the largest size that fits, compacting if needed."""
    style = dict(line_height=1, stroke=7, stroke_color=outline)
    text = value if isinstance(value, str) else format_count(value)
    fitted = fit_text(text, DISPLAY, size, room, min_size=min_size, **style)
    if fitted is None and not isinstance(value, str):
        fitted = fit_text(compact_count(value), DISPLAY, size, room, min_size=18, **style)
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


def draw_footer(canvas, left_text, right_text, dark, *, size=20, inset=40, bottom=20):
    """White footer text with a hard shadow and a soft glow behind it."""
    parts = [Text(t, HAND, size) for t in (left_text, right_text)]
    top = CARD_HEIGHT - bottom - max(p.height for p in parts)
    draw_glowing_text(canvas, [(parts[0], inset, top),
                               (parts[1], CARD_WIDTH - inset - parts[1].width, top)], dark)


def draw_glowing_text(canvas, placed, dark):
    """White text over a hard shadow 2px down and a soft glow, both `dark`:
    the design's text-shadow: 0 2px 0 dark, 0 0 6px dark. `placed` is a list
    of (Text, x, y) in design pixels."""
    # The glow is blurred over just the text's area, not the whole card.
    margin = px(12)
    boxes = [part.font.getbbox(part.text, anchor="ls") for part, _, _ in placed if part.text]
    if not boxes:
        return
    origins = [(px(x), px(y + part.baseline)) for part, x, y in placed if part.text]
    left = max(0, min(ox + b[0] for (ox, _), b in zip(origins, boxes)) - margin)
    top = max(0, min(oy + b[1] for (_, oy), b in zip(origins, boxes)) - margin)
    right = max(ox + b[2] for (ox, _), b in zip(origins, boxes)) + margin
    bottom = max(oy + b[3] for (_, oy), b in zip(origins, boxes)) + margin
    glow = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    for part, x, y in placed:
        glow_draw.text((px(x) - left, px(y + part.baseline) - top), part.text,
                       font=part.font, fill=dark, anchor="ls")
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(px(3))), (left, top))
    draw = ImageDraw.Draw(canvas)
    for part, x, y in placed:
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
    # Last seen lives in the message under the card, as a Discord timestamp
    # that stays current; baked into the picture it would go stale.
    left = f"ID {user_id}" if user_id is not None else ""
    right = f"joined {format_date(joined)}" if joined is not None else ""
    draw_footer(above, left, right, footer_outline)

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


# ---------------------------------------------------------------- map card

# The polaroid: a 606px-wide paper, tilted -3deg, at (38, 50).
POLAROID_LEFT, POLAROID_TOP, POLAROID_WIDTH = 38, 50, 606
# padding 16 + 318 photo + 12 gap + 64 caption row + 14 padding.
POLAROID_HEIGHT = 16 + 318 + 12 + 64 + 14
PHOTO_WIDTH, PHOTO_HEIGHT = POLAROID_WIDTH - 32, 318
CAPTION_TOP = 16 + PHOTO_HEIGHT + 12
# The caption row's padding: 16 paper + 6 row on each side.
CAPTION_LEFT, CAPTION_RIGHT = 22, POLAROID_WIDTH - 22
MAP_PURPLE = "#6B45C9"
# The creator's handle shrinks to stay within this, leaving the map name room.
CREATOR_MAX_WIDTH = 200
CREATOR_MIN_WIDTH = 130
CREATOR_HANDLE_SIZE = 24
CLIP_TAPE = ((3, 0), (97, 0), (100, 25), (96, 50), (100, 75), (97, 100),
             (3, 100), (0, 75), (4, 50), (0, 25))
# The stat column: left 682, right 34, from top 40, 26 apart.
# The template's gap is 26; 16 makes room for the fourth (completions) strip.
MAP_STRIPS_LEFT, MAP_STRIPS_TOP, MAP_STRIPS_GAP = 682, 40, 16
MAP_STRIPS_WIDTH = CARD_WIDTH - MAP_STRIPS_LEFT - 34

# Icon per game mode; unknown modes get the party one.
MAP_MODE_ICONS = {"PARTY": "party"}


def map_icon(name, size):
    return load_icon("map", name, size)


def solid(width, height, color):
    return Image.new("RGBA", (px(width), px(height)), color)


# Without a thumbnail: the design's sky blues. The card fades light to dark,
# the empty photo dark to light, so the two don't read as one gradient.
NO_THUMBNAIL_COLORS = ((180, 222, 255), (47, 127, 224))
NO_THUMBNAIL_PHOTO_COLORS = ((47, 127, 224), (109, 184, 250))
# The empty photo's text outline; its map icon (map-blue) is drawn in it too.
PLACEHOLDER_BLUE = "#1C468F"


# Colours closer than this (RGB distance) count as one when picking the
# backdrop's two, so a mostly-dark picture doesn't fade black to near-black.
MIN_COLOR_DISTANCE = 60


def dominant_colors(image):
    """The picture's two most common colours, (primary, secondary), as RGB.

    Counted on a small copy reduced to a 12-colour palette, with palette
    entries closer than MIN_COLOR_DISTANCE pooled into one: median cut
    splits a big dark area into several near-blacks, each of which would
    otherwise count for less than one small bright area. A one-colour
    picture gets a lighter or darker shade of its colour as the secondary.
    """
    small = image.convert("RGB")
    small.thumbnail((96, 96))
    palette = small.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    rgb = palette.getpalette()
    groups = []   # [count, colour of the group's most common entry]
    for count, index in sorted(palette.getcolors(), reverse=True):
        color = tuple(rgb[index * 3:index * 3 + 3])
        group = next((g for g in groups
                      if math.dist(g[1], color) < MIN_COLOR_DISTANCE), None)
        if group:
            group[0] += count
        else:
            groups.append([count, color])
    groups.sort(key=lambda g: g[0], reverse=True)
    primary = groups[0][1]
    if len(groups) > 1:
        return primary, groups[1][1]
    return primary, shade(primary, 1.4 if sum(primary) < 384 else 0.6)


@functools.lru_cache(maxsize=16)
def map_backdrop(primary, secondary, width=CARD_WIDTH, height=CARD_HEIGHT):
    """Primary fading down to secondary under the staggered dot pattern,
    `width` x `height` design pixels. Returns (backdrop, footer outline)."""
    width, height = px(width), px(height)
    backdrop = vertical_gradient(width, height, (
        (0.0, primary[:3] + (255,)), (1.0, secondary[:3] + (255,)),
    ))
    # Dots a shade darker than the colour mid-way, as the profile's are.
    middle = tuple((a + b) // 2 for a, b in zip(primary[:3], secondary[:3]))
    backdrop.alpha_composite(dot_pattern(width, height, shade(middle, 0.6) + (64,)))
    # The white footer text is outlined in the darker colour, deepened, so
    # it still reads where the fade ends near white.
    darker = min(primary[:3], secondary[:3], key=sum)
    return backdrop, shade(darker, 0.4) + (255,)


def draw_photo(paper, thumbnail):
    """The map thumbnail filling the photo, or a dotted sky with the map icon."""
    width, height = px(PHOTO_WIDTH), px(PHOTO_HEIGHT)
    if thumbnail is not None:
        photo = Image.new("RGBA", (width, height), rgba("#0B0B14"))
        photo.alpha_composite(cover_crop(thumbnail, width, height))
    else:
        photo = map_backdrop(*NO_THUMBNAIL_PHOTO_COLORS, PHOTO_WIDTH, PHOTO_HEIGHT)[0].copy()
        paste_icon(photo, map_icon("map-blue", 110), (PHOTO_WIDTH - 110) / 2, 84)
        note = Text("no picture yet", HAND, 26, stroke=6,
                    stroke_color=rgba(PLACEHOLDER_BLUE))
        note.draw(ImageDraw.Draw(photo), (PHOTO_WIDTH - note.width) / 2, 204)
    photo = clip_to(photo, rounded_mask(PHOTO_WIDTH, PHOTO_HEIGHT, 4))
    paper.alpha_composite(photo, (px(16), px(16)))


def round_avatar(avatar):
    """The creator's 44px headshot in a white 4px border and a 3px purple ring,
    58px across. Missing: the placeholder colour with a question mark."""
    ring = clip_to(solid(58, 58, rgba(MAP_PURPLE)), rounded_mask(58, 58, 29))
    white = clip_to(solid(52, 52, WHITE), rounded_mask(52, 52, 26))
    ring.alpha_composite(white, (px(3), px(3)))
    face = solid(44, 44, rgba("#C9A15A"))
    if avatar is not None:
        side = min(avatar.size)
        left, top = (avatar.width - side) // 2, (avatar.height - side) // 2
        square = avatar.crop((left, top, left + side, top + side))
        face.alpha_composite(square.resize(face.size, Image.LANCZOS))
    else:
        mark = Text("?", DISPLAY, 26, line_height=1, stroke=6,
                    stroke_color=rgba("#8A6A2E"))
        mark.draw(ImageDraw.Draw(face), (44 - mark.width) / 2, (44 - mark.height) / 2 + 2)
    ring.alpha_composite(clip_to(face, rounded_mask(44, 44, 22)), (px(7), px(7)))
    return ring


def creator_handle(text, room, face=None):
    """The creator's handle as Text, shrunk to `room`, never cut.

    With `face` (a username effect's font) it's laid out for the animation:
    white with the map name's purple outline, which the effect paints over,
    so a pale effect still reads on the white paper.
    """
    purple = rgba(MAP_PURPLE)
    if face is None:
        return shrink_to_fit(text, DISPLAY, CREATOR_HANDLE_SIZE, room,
                             line_height=1, fill=purple)
    style = map_name_style(CREATOR_HANDLE_SIZE, purple)
    handle = None
    if face != DISPLAY:
        no_line_height = {k: v for k, v in style.items() if k != "line_height"}
        handle = fit_name_in_face(text, face, room, no_line_height,
                                  size=CREATOR_HANDLE_SIZE, min_size=8)
    # The effect's font can't fit it (or is the card's own): the card font.
    return handle or shrink_to_fit(text, DISPLAY, CREATOR_HANDLE_SIZE, room, **style)


def draw_caption(paper, map_name, creator_name, avatar, handle_face=None):
    """The row under the photo: map icon and name left, creator right.

    With `handle_face` the handle is laid out in that font but not drawn.
    Returns the handle and where its box's top-left sits on the paper.
    """
    draw = ImageDraw.Draw(paper)
    purple = rgba(MAP_PURPLE)
    row_mid = CAPTION_TOP + 32

    # Creator first, since the map name gets whatever room it leaves. A
    # long handle shrinks rather than losing its tail.
    by = Text("by", HAND, 17, line_height=1, fill=purple)
    name_left = CAPTION_LEFT + 54 + 14
    # The handle gets up to CREATOR_MAX_WIDTH; if that would leave the map
    # name smaller than 24px, the handle shrinks into less so the name,
    # the card's title, keeps more.
    for creator_room in (CREATOR_MAX_WIDTH, CREATOR_MIN_WIDTH):
        handle = creator_handle(f"@{creator_name}" if creator_name else "unknown",
                                creator_room, handle_face)
        column = max(by.width, handle.width)
        group_left = CAPTION_RIGHT - (52 + 10 + column)
        lines = fit_map_name(map_name, group_left - 14 - name_left, purple)
        if lines[0].size >= 24:
            break
    paper.alpha_composite(round_avatar(avatar), (px(group_left - 3), px(row_mid - 26 - 3)))
    # Luckiest Guy's capitals stand taller than its line box, so the handle
    # sits 3px lower than the template's padding to clear the "y" of "by".
    text_left = group_left + 52 + 10
    text_top = row_mid - (17 + 6 + handle.height) / 2
    by.draw(draw, text_left, text_top)
    handle_at = (text_left, text_top + 17 + 6)
    if handle_face is None:
        handle.draw(draw, *handle_at)

    # In the name's purple, so the icon and the name outline match.
    paste_icon(paper, map_icon("map-purple", 54), CAPTION_LEFT, row_mid - 27)
    # A one-line box is the font size tall plus 6px padding on top, centred
    # in the 64px row, so the text's middle sits 35px down; wrapped lines
    # share that middle.
    gap = {1: 0, 2: 2}.get(len(lines), 1)
    top = CAPTION_TOP + 35 - (sum(line.height for line in lines) + gap) / 2
    for line in lines:
        line.draw(draw, name_left, top)
        top += line.height + gap
    return handle, handle_at


def shrink_to_fit(text, face, size, max_width, *, min_size=8, **style):
    """The text at the largest size that fits, never cut short."""
    return (fit_text(text, face, size, max_width, min_size=min_size, **style)
            or Text(text, face, min_size, **style))


def map_name_style(size, outline):
    """The name's outline and shadow, kept in proportion as it shrinks."""
    k = size / 58
    return dict(line_height=1, stroke=max(2.5, 9 * k), stroke_color=outline,
                shadow=max(1.5, 5 * k))


# (lines, largest size, smallest size), tried in order: the name stays as
# big as it can, and only wraps once one line would get too small.
MAP_NAME_LAYOUTS = ((1, 58, 30), (2, 30, 20), (3, 21, 14))


def _splits(words, lines):
    """Every way to break the words into `lines` consecutive lines."""
    if lines == 1:
        yield [" ".join(words)]
        return
    for i in range(1, len(words) - lines + 2):
        for rest in _splits(words[i:], lines - 1):
            yield [" ".join(words[:i])] + rest


def _widest(parts, size):
    font = load_font(DISPLAY, size)
    return max(font.getlength(part) for part in parts) / SCALE


def fit_map_name(name, room, outline):
    """The map name as one to three lines of Text that fit `room`, never cut.

    Each layout in MAP_NAME_LAYOUTS is tried from its largest size down,
    breaking at the words that keep the widest line narrowest. A name that
    fits none of them takes as many lines as it has words (up to three) at
    whatever size it needs.
    """
    words = name.split()

    def attempt(lines, sizes, force=False):
        for size in sizes:
            best = min(_splits(words, lines), key=lambda parts: _widest(parts, size))
            if force or _widest(best, size) <= room:
                style = map_name_style(size, outline)
                return [Text(part, DISPLAY, size, **style) for part in best]
        return None

    for lines, largest, smallest in MAP_NAME_LAYOUTS:
        if len(words) >= lines:
            fitted = attempt(lines, range(largest, smallest - 1, -1))
            if fitted:
                return fitted
    lines = max(1, min(3, len(words)))
    return (attempt(lines, range(29 if lines == 1 else 13, 4, -1))
            or attempt(lines, [4], force=True))


POLAROID_TILT = -3


def draw_polaroid(canvas, map_name, creator_name, avatar, thumbnail, handle_face=None):
    """Returns draw_caption's (handle, top-left on the paper)."""
    paper = clip_to(solid(POLAROID_WIDTH, POLAROID_HEIGHT, rgba("#FFFDF4")),
                    rounded_mask(POLAROID_WIDTH, POLAROID_HEIGHT, 6))
    draw_photo(paper, thumbnail)
    handle = draw_caption(paper, map_name, creator_name, avatar, handle_face)

    # Padding for the tape, which overhangs the corners, then the two
    # strips of tape, then the whole thing tilts as one.
    pad = 40
    layer = with_drop_shadow(paper, 9, pad=pad)
    tape = strip(120, 34, rgba("#FFF1B5", 217), CLIP_TAPE)
    place(layer, tape, pad - 22, pad - 10, 120, 34, rotate=-32)
    place(layer, tape, pad + POLAROID_WIDTH + 22 - 120, pad - 10, 120, 34, rotate=30)
    place(canvas, layer, POLAROID_LEFT - pad, POLAROID_TOP - pad,
          POLAROID_WIDTH + 2 * pad, POLAROID_HEIGHT + 2 * pad, rotate=POLAROID_TILT)
    return handle


def turn_point(x, y, left, top, width, height, degrees):
    """Where point (x, y) of a box at (left, top) lands once the box turns
    `degrees` (CSS: clockwise) about its centre, in design px."""
    cx, cy = width / 2, height / 2
    theta = math.radians(degrees)
    dx, dy = x - cx, y - cy
    return (left + cx + dx * math.cos(theta) - dy * math.sin(theta),
            top + cy + dx * math.sin(theta) + dy * math.cos(theta))


def paper_to_canvas(x, y):
    """Where a point on the polaroid's paper lands on the card, in design px."""
    return turn_point(x, y, POLAROID_LEFT, POLAROID_TOP, POLAROID_WIDTH, POLAROID_HEIGHT,
                      POLAROID_TILT)


def draw_map_strip(canvas, top, icon, value, caption, fill, outline, rotate):
    """One stat strip of the right-hand column. `icon` is a loaded 52px
    icon, `value` a laid-out Text. Returns its height."""
    dark = rgba(outline)
    label = Text(caption, HAND, 19, line_height=1.1, stroke=4, stroke_color=dark)
    inner = max(52, value.height + label.height)
    height = 10 + inner + 10

    def content(image, draw):
        paste_icon(image, icon, 14, 10 + (inner - 52) / 2)
        text_top = 10 + (inner - value.height - label.height) / 2
        value.draw(draw, 14 + 52 + 12, text_top)
        label.draw(draw, 14 + 52 + 12, text_top + value.height)

    body = strip(MAP_STRIPS_WIDTH, height, rgba(fill), CLIP_STAT, content)
    place(canvas, with_drop_shadow(body, 4), MAP_STRIPS_LEFT, top,
          MAP_STRIPS_WIDTH, height, rotate=rotate)
    return height


def map_tag(text, fill, outline):
    """A small torn tag (the visibility one): padding 6px 20px 3px."""
    label = Text(text, DISPLAY, 22, stroke=6, stroke_color=rgba(outline))
    width, height = 20 + label.width + 20, 6 + label.height + 3
    body = strip(width, height, rgba(fill), CLIP_MEDAL,
                 lambda image, draw: label.draw(draw, 20, 6))
    return body, width, height


def draw_map_strips(canvas, mode, plays, completions, favourites, visibility, featured):
    room = MAP_STRIPS_WIDTH - 14 - 52 - 12 - 18
    top = MAP_STRIPS_TOP

    def count(value, outline):
        return stat_value_text(value, rgba(outline), room, size=34, min_size=30)

    mode_text = drawable_text(mode or "", DISPLAY).upper() or "UNKNOWN"
    mode_value = shrink_to_fit(mode_text, DISPLAY, 32, room, min_size=12,
                               line_height=1, stroke=7, stroke_color=rgba("#5E37B8"))
    strips = (
        (map_icon(MAP_MODE_ICONS.get(mode_text, "party"), 52), mode_value, "game mode",
         "#A57CF7", "#5E37B8", 2),
        (map_icon("play", 52), count(plays, "#3F8F1E"),
         "play" if plays == 1 else "plays", "#8BE04E", "#3F8F1E", -1.5),
        # The profile's "flags reached" flag and colours: a completion is
        # reaching a map's finish.
        (profile_icon("flag", 52), count(completions, "#C23A2E"),
         "completion" if completions == 1 else "completions", "#FF7A6B", "#C23A2E", 1.5),
        (map_icon("star", 52), count(favourites, "#A87A00"),
         "favourite" if favourites == 1 else "favourites", "#FFD64F", "#A87A00", -1),
    )
    for icon, value, caption, fill, outline, rotate in strips:
        top += draw_map_strip(canvas, top, icon, value, caption, fill, outline,
                              rotate) + MAP_STRIPS_GAP

    # Visibility, then Featured beside it (or under it, if they won't fit).
    tags = []
    if visibility:
        tags.append(map_tag(str(visibility).upper(), "#7FE8C2", "#239372") + (-2,))
    if featured:
        tags.append(map_tag("FEATURED", "#FFD64F", "#A87A00") + (2,))
    left = MAP_STRIPS_LEFT
    for body, width, height, rotate in tags:
        if left > MAP_STRIPS_LEFT and left + width > CARD_WIDTH - 20:
            left, top = MAP_STRIPS_LEFT, top + height + 12
        place(canvas, with_drop_shadow(body, 4), left, top, width, height, rotate=rotate)
        left += width + 10


def compose_map_card(*, name, map_id=None, creator_name=None, creator_avatar_png=None,
                     thumbnail_png=None, mode=None, plays=0, favourites=0,
                     visibility=None, featured=False, completions=0, handle_face=None):
    """The map card image and draw_caption's (handle, top-left on the paper).

    With `handle_face` the creator's handle is left off, for the animation.
    """
    plays, favourites = max(0, plays or 0), max(0, favourites or 0)
    completions = max(0, completions or 0)
    shown_name = drawable_text(name or "", DISPLAY)
    if not shown_name:
        shown_name = f"Map {map_id}" if map_id is not None else "Unnamed Map"
    creator = drawable_text(creator_name or "", DISPLAY) or None

    thumbnail = open_image(thumbnail_png)
    colors = dominant_colors(thumbnail) if thumbnail is not None else NO_THUMBNAIL_COLORS
    # Always lightest at the top, darkest at the bottom (Rec. 601 luma).
    colors = sorted(colors, key=lambda c: 299 * c[0] + 587 * c[1] + 114 * c[2],
                    reverse=True)
    backdrop, footer_outline = map_backdrop(*colors)
    canvas = backdrop.copy()
    handle = draw_polaroid(canvas, shown_name, creator, open_image(creator_avatar_png),
                           thumbnail, handle_face)
    draw_map_strips(canvas, mode, plays, completions, favourites, visibility, featured)
    if map_id is not None:
        draw_footer(canvas, f"map ID {map_id}", "", footer_outline,
                    size=22, inset=48, bottom=22)

    return clip_to(canvas, rounded_mask(CARD_WIDTH, CARD_HEIGHT, 24)), handle


def render_map_card(**fields):
    """A community map's card as PNG bytes.

    `visibility` is Public / Unlisted / Private, or None to leave the tag
    off. The background fades top to bottom between the thumbnail's two
    most common colours; without a thumbnail, between two sky blues.
    Missing avatar or thumbnail bytes get placeholders.
    """
    card, _ = compose_map_card(**fields)
    out = io.BytesIO()
    card.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


# ---------------------------------------------------------------- leaderboard card

LEADERBOARD_WIDTH, LEADERBOARD_HEIGHT = 960, 640
# The time medals, best first, so a tier is its index (get_medal_emoji's order).
MEDAL_ICONS = ("medal-diamond", "medal-gold", "medal-silver", "medal-bronze")
PAPER = "#FFF1B5"
TIME_PURPLE = "#3D2490"
NAME_INK = "#2A2F4A"
# rgba(20,20,60,0.2), the lighter shadow under the time pills and sheet flags.
LIGHT_SHADOW = (20, 20, 60, 51)
CLIP_BLOCK = ((0, 3), (25, 0), (50, 2.5), (75, 0), (100, 3), (100, 100), (0, 100))
CLIP_SHEET = ((0, 1), (22, 0), (48, 1.2), (76, 0), (100, 0.8), (99.2, 34), (100, 66),
              (99.4, 99), (72, 100), (44, 98.8), (18, 100), (0, 99.2), (0.8, 64), (0, 30))

# The podium by place (1st, 2nd, 3rd): the block's left, top and width, its
# fill and outline, then the avatar tile's left, top, size, tilt and the
# colour behind the picture. The blocks are 320px tall and run off the card.
PODIUM = (
    (174, 370, 156, "#FFD64F", "#A87A00", (192, 246, 120, 3, "#2A2F4A")),
    (22, 420, 146, "#6CC4F4", "#2A78B8", (43, 312, 104, -4, "#9ED3FF")),
    (336, 450, 146, "#FFA851", "#C4610F", (357, 342, 104, 4, "#C9A6FF")),
)
BLOCK_HEIGHT = 320
# The ranks 4-10 sheet: left 498, top 122, 438 wide, padding 14px 22px 16px,
# rows 46px tall under a 2px dashed rule.
SHEET_LEFT, SHEET_TOP, SHEET_WIDTH, SHEET_PAD = 498, 122, 438, 22
SHEET_ROW = 46
SHEET_ROWS = 7
# The title banner shrinks from 44px to 28px, then loses its tail, to keep
# its right edge here: over the dotted strip, clear of the sheet's column.
TITLE_MAX_RIGHT = 600
# Player names, bigger than the design's 16px (podium) and 17px (sheet).
# Long names shrink to the minimum, then lose their tail.
PODIUM_NAME_SIZE, PODIUM_NAME_MIN = 22, 14
SHEET_NAME_SIZE, SHEET_NAME_MIN = 22, 16


def flag_path(code):
    """The bundled flag for an ISO 3166 alpha-2 code, or None."""
    code = str(code or "").strip().lower()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return None
    path = os.path.join(ASSETS, "flags", f"{code}.png")
    return path if os.path.exists(path) else None


@functools.lru_cache(maxsize=128)
def flag_tile(code, width, height):
    """The flag cover-cropped to width x height inside a 2px white border with
    3px corners, or None for a player with no (known) country."""
    path = flag_path(code)
    if path is None:
        return None
    with Image.open(path) as image:
        flag = image.convert("RGBA")
    outer_w, outer_h = width + 4, height + 4
    frame = clip_to(solid(outer_w, outer_h, WHITE), rounded_mask(outer_w, outer_h, 3))
    inner = clip_to(cover_crop(flag, px(width), px(height)), rounded_mask(width, height, 1))
    frame.alpha_composite(inner, (px(2), px(2)))
    return frame


def leaderboard_backdrop(thumbnail):
    """The card's background and the colour its white text is outlined in.

    The map's thumbnail fills it, as a profile card's chosen map does;
    without one, the map card's no-thumbnail sky fade and dots.
    """
    if thumbnail is not None:
        canvas = Image.new("RGBA", (px(LEADERBOARD_WIDTH), px(LEADERBOARD_HEIGHT)))
        return canvas, draw_background(canvas, (11, 11, 20), thumbnail)
    backdrop, outline = map_backdrop(*NO_THUMBNAIL_COLORS, LEADERBOARD_WIDTH,
                                     LEADERBOARD_HEIGHT)
    return backdrop.copy(), outline


def player_name(name, size, min_size, room, **style):
    """A player's name in the card font: shrunk to `min_size` to fit, then cut."""
    text = drawable_text(name or "", DISPLAY) or "Unknown"
    return (fit_text(text, DISPLAY, size, room, min_size=min_size, **style)
            or ellipsize(text, DISPLAY, min_size, room, **style))


def effect_name_text(player, size, min_size, room, style):
    """The name laid out for its username effect: in the effect's font,
    sized like the card font's (fit_name_in_face), or the card font if the
    effect has none or its font can't fit. Drawn later, by AnimatedName."""
    text = drawable_text(player.get("name") or "", DISPLAY) or "Unknown"
    face = player["effect"].get("font") or DISPLAY
    name = None
    if face != DISPLAY:
        name = fit_name_in_face(text, face, room, style, size=size, min_size=min_size)
    return name or player_name(text, size, min_size, room, **style)


def medal_tier(player):
    tier = player.get("tier")
    return tier if isinstance(tier, int) and 0 <= tier < len(MEDAL_ICONS) else None


def draw_title_banner(canvas, title):
    outline = rgba("#5E37B8")
    style = dict(line_height=1, stroke=8, stroke_color=outline, shadow=4)
    room = TITLE_MAX_RIGHT - 24 - (18 + 54 + 12 + 36)
    text = drawable_text(title or "", DISPLAY) or "Leaderboard"
    name = (fit_text(text, DISPLAY, 44, room, min_size=28, **style)
            or ellipsize(text, DISPLAY, 28, room, **style))
    # padding 10px 36px 6px 18px around a 54px trophy and the 44px text with
    # 6px on top; a shrunk title stays centred in the full-size slot.
    inner = 54
    width = 18 + 54 + 12 + name.width + 36
    height = 10 + inner + 6

    def content(image, draw):
        paste_icon(image, profile_icon("trophy", 54), 18, 10)
        name.draw(draw, 18 + 54 + 12, 10 + (inner - name.height - 6) / 2 + 6)

    body = strip(width, height, rgba("#A57CF7"), CLIP_BANNER, content)
    place(canvas, with_drop_shadow(body, 5), 24, 26, width, height, rotate=-1.5)


def time_pill(time_text, tier):
    """The cream time tag: padding 6px 12px 4px 10px, an optional 34px medal
    with -8px/-2px margins (so it spills out and is clipped), 4px gap."""
    label = shrink_to_fit(time_text, DISPLAY, 18, 112, min_size=12,
                          fill=rgba(TIME_PURPLE))
    inner = max(18 if tier is not None else 0, label.height + 3)
    width = 10 + (30 + 4 if tier is not None else 0) + label.width + 12
    height = 6 + inner + 4

    def content(image, draw):
        x = 10
        if tier is not None:
            place(image, profile_icon(MEDAL_ICONS[tier], 34), x - 2,
                  6 + (inner - 18) / 2 - 8, 34, 34)
            x += 30 + 4
        label.draw(draw, x, 6 + (inner - label.height - 3) / 2 + 3)

    return strip(width, height, rgba(PAPER), CLIP_MEDAL, content), width, height


# The podium's flag: a sticker on the avatar tile's bottom-right corner,
# hanging this far past its right edge, so the name below gets the block's
# whole width. (The design had it beside the name.)
PODIUM_FLAG = (36, 24)
PODIUM_FLAG_OVERHANG = 10
PODIUM_FLAG_TILT = 8


def draw_podium_spot(canvas, index, player, animated=None):
    """One podium place: the avatar tile above (crowned for first, with the
    player's flag on its corner), the block with rank, name and time below.
    `index` is 0 for first.

    With `animated` (a list), a player with an "effect" has their name left
    off and an AnimatedName spec for it appended instead.
    """
    left, top, width, fill, outline, avatar_box = PODIUM[index]
    a_left, a_top, a_size, tilt, empty = avatar_box
    dark = rgba(outline)

    # The crown and flag sit on the tile's corners and tilt with it.
    pad = 40
    side = a_size + 2 * pad
    layer = Image.new("RGBA", (px(side), px(side)), (0, 0, 0, 0))
    tile = with_drop_shadow(
        framed_avatar(open_image(player.get("avatar_png")), a_size, 6, 22, empty, outline), 6)
    place(layer, tile, pad, pad, a_size, a_size)
    if index == 0:
        place(layer, profile_icon("crown", 56), pad - 22, pad - 30, 56, 56, rotate=-22)
    flag = flag_tile(player.get("country"), *PODIUM_FLAG)
    if flag:
        flag_w, flag_h = PODIUM_FLAG[0] + 4, PODIUM_FLAG[1] + 4
        place(layer, with_drop_shadow(flag, 3), pad + a_size - flag_w + PODIUM_FLAG_OVERHANG,
              pad + a_size - flag_h, flag_w, flag_h, rotate=PODIUM_FLAG_TILT)
    place(canvas, layer, a_left - pad, a_top - pad, side, side, rotate=tilt)

    rank = Text(str(player.get("rank", index + 1)), DISPLAY, 60, line_height=1,
                stroke=10, stroke_color=dark, shadow=5)
    room = width - 16
    style = dict(stroke=5, stroke_color=dark)
    effect = player.get("effect") if animated is not None else None
    if effect:
        name = effect_name_text(player, PODIUM_NAME_SIZE, PODIUM_NAME_MIN, room, style)
    else:
        name = player_name(player.get("name"), PODIUM_NAME_SIZE, PODIUM_NAME_MIN, room, **style)
    row_height = name.height + 3
    pill, pill_width, pill_height = time_pill(player.get("time") or "--", medal_tier(player))
    name_at = []

    def content(image, draw):
        # A column centred under padding 12px 8px 0, 6px apart.
        rank.draw(draw, (width - rank.width) / 2, 12)
        y = 12 + rank.height + 6
        name_at.append(((width - name.width) / 2, y + 3))
        if not effect:
            name.draw(draw, *name_at[0])
        y += row_height + 6
        place(image, with_drop_shadow(pill, 3, color=LIGHT_SHADOW),
              (width - pill_width) / 2, y, pill_width, pill_height)

    body = strip(width, BLOCK_HEIGHT, rgba(fill), CLIP_BLOCK, content)
    place(canvas, with_drop_shadow(body, 6), left, top, width, BLOCK_HEIGHT)
    if effect:
        x, y = name_at[0]
        animated.append(dict(name=name, at=(left + x, top + y), effect=effect, outline=dark,
                             stroke_scale=PODIUM_NAME_SIZE / NAME_SIZE, margin=HANDLE_MARGIN))


def dashed_rule(draw, left, right, y, color, dash=6, gap=4):
    """A 2px dashed border-top."""
    x = left
    while x < right:
        end = min(x + dash, right)
        draw.rectangle((px(x), px(y), px(end) - 1, px(y + 2) - 1), fill=color)
        x += dash + gap


def draw_sheet_row(image, draw, player, top, animated=None):
    """One row of the sheet: rank, flag, name, medal, time, 10px apart.
    `animated` as for draw_podium_spot, but with the name's top-left on
    the sheet: the caller knows how the sheet turns."""
    left, right = SHEET_PAD, SHEET_WIDTH - SHEET_PAD
    middle = top + SHEET_ROW / 2

    def centred(text):
        # Luckiest Guy rows carry 3px of padding on top.
        return middle - (text.height + 3) / 2 + 3

    rank = shrink_to_fit(f"#{player.get('rank', '')}", DISPLAY, 20, 38 + 6, min_size=14,
                         fill=rgba(MAP_PURPLE))
    rank.draw(draw, left, centred(rank))
    x = left + 38 + 10

    time = shrink_to_fit(player.get("time") or "--", DISPLAY, 19, 94, min_size=12,
                         fill=rgba(TIME_PURPLE))
    time.draw(draw, right - time.width, centred(time))
    name_right = right - 94 - 10

    tier = medal_tier(player)
    if tier is not None:
        place(image, profile_icon(MEDAL_ICONS[tier], 34), name_right - 34, middle - 17, 34, 34)
        name_right -= 34 + 10

    flag = flag_tile(player.get("country"), 30, 20)
    if flag:
        place(image, with_drop_shadow(flag, 2, color=LIGHT_SHADOW), x, middle - 12, 34, 24)
        x += 34 + 10

    effect = player.get("effect") if animated is not None else None
    if effect:
        # The plain name is dark ink with no outline, which an effect's
        # white base would lose on the cream paper: give it an ink outline,
        # as the map card's handle has.
        ink = rgba(NAME_INK)
        name = effect_name_text(player, SHEET_NAME_SIZE, SHEET_NAME_MIN, name_right - x,
                                dict(stroke=4, stroke_color=ink))
        animated.append(dict(name=name, at=(x, centred(name)), effect=effect, outline=ink,
                             stroke_scale=SHEET_NAME_SIZE / NAME_SIZE, margin=SHEET_NAME_MARGIN))
        return
    name = player_name(player.get("name"), SHEET_NAME_SIZE, SHEET_NAME_MIN, name_right - x,
                       fill=rgba(NAME_INK))
    name.draw(draw, x, centred(name))


# Room round an animated sheet name: small enough that neighbouring rows'
# patches (48px apart) never overlap, since each is pasted over the frame.
SHEET_NAME_MARGIN = 8


def draw_sheet(canvas, title, players, empty_note, animated=None):
    """The cream sheet of ranks 4-10, its height following its rows. With no
    rows it shows `empty_note` in a row of its own. `animated` as for
    draw_podium_spot."""
    purple = rgba(MAP_PURPLE)
    rule = rgba("#E9D27E")
    heading = shrink_to_fit(title or "", HAND, 26, SHEET_WIDTH - 2 * SHEET_PAD, min_size=16,
                            line_height=1, fill=purple)
    rows = max(1, len(players))
    height = 14 + 26 + 6 + rows * (2 + SHEET_ROW) + 16
    on_sheet = [] if animated is not None else None

    def content(image, draw):
        heading.draw(draw, (SHEET_WIDTH - heading.width) / 2, 14 + (26 - heading.height) / 2)
        y = 14 + 26 + 6
        for player in players:
            dashed_rule(draw, SHEET_PAD, SHEET_WIDTH - SHEET_PAD, y, rule)
            draw_sheet_row(image, draw, player, y + 2, on_sheet)
            y += 2 + SHEET_ROW
        if not players:
            dashed_rule(draw, SHEET_PAD, SHEET_WIDTH - SHEET_PAD, y, rule)
            note = Text(empty_note, HAND, 24, fill=purple)
            note.draw(draw, (SHEET_WIDTH - note.width) / 2, y + 2 + (SHEET_ROW - note.height) / 2)

    body = strip(SHEET_WIDTH, height, rgba(PAPER), CLIP_SHEET, content)
    place(canvas, with_drop_shadow(body, 6), SHEET_LEFT, SHEET_TOP, SHEET_WIDTH, height,
          rotate=SHEET_TILT)
    for spec in on_sheet or ():
        spec.update(rotate=SHEET_TILT, to_canvas=lambda x, y: turn_point(
            x, y, SHEET_LEFT, SHEET_TOP, SHEET_WIDTH, height, SHEET_TILT))
        animated.append(spec)


SHEET_TILT = 1.2


def compose_leaderboard_card(*, title, subtitle="", sheet_title="", footer="", players=(),
                             thumbnail_png=None, animate=False):
    """The card as an RGBA image, and with `animate` the AnimatedName specs
    for every player with an "effect" (whose names are left off)."""
    players = list(players)[:3 + SHEET_ROWS]
    animated = [] if animate else None
    canvas, text_outline = leaderboard_backdrop(open_image(thumbnail_png))
    draw_title_banner(canvas, title)
    if subtitle:
        text = shrink_to_fit(subtitle, HAND, 26, SHEET_LEFT - 40 - 10, min_size=16)
        draw_glowing_text(canvas, [(text, 40, 112)], text_outline)

    # Second, first, third: each avatar overlaps the block to its left.
    for index in (1, 0, 2):
        if index < len(players):
            draw_podium_spot(canvas, index, players[index], animated)
    draw_sheet(canvas, sheet_title, players[3:],
               "no times yet" if not players else "nobody else yet", animated)

    if footer:
        text = shrink_to_fit(footer, HAND, 21, LEADERBOARD_WIDTH - SHEET_LEFT, min_size=14)
        draw_glowing_text(canvas, [(text, LEADERBOARD_WIDTH - 40 - text.width,
                                    LEADERBOARD_HEIGHT - 20 - text.height)], text_outline)

    card = clip_to(canvas, rounded_mask(LEADERBOARD_WIDTH, LEADERBOARD_HEIGHT, 24))
    return card, animated or []


def render_leaderboard_card_animated(**fields):
    """The leaderboard with every player's username effect playing on their
    name, as animated WebP bytes. Each player is a dict as for
    render_leaderboard_card, plus "effect" (a username_effects effect) for
    those who have one. All the effects share one loop.

    Half size, like the other animated cards: names spread over the whole
    card make every frame's changed area most of it, and libwebp takes ~4x
    as long to encode one at full size, for a picture Discord shows at
    about 600px wide anyway. Frames are drawn as the encoder takes them
    (encode_lazy_animation), so only one is held.
    """
    with ANIMATION_LOCK:
        card, specs = compose_leaderboard_card(animate=True, **fields)
        frames_count, delay, cycles = plan_shared_loop([spec["effect"] for spec in specs])
        names = [
            AnimatedName(card, spec["name"], spec["at"], spec["effect"], spec_cycles,
                         outline=spec["outline"], stroke_scale=spec["stroke_scale"],
                         to_canvas=spec.get("to_canvas", lambda x, y: (x, y)),
                         rotate=spec.get("rotate", 0), margin=spec["margin"],
                         keep_outline=True)
            for spec, spec_cycles in zip(specs, cycles)
        ]
        still = card.resize((card.width // 2, card.height // 2), Image.LANCZOS)
        del card

        def draw_frame(index, image):
            # Each patch covers its whole region, so the last frame's names
            # are simply drawn over.
            for name in names:
                image.paste(name.patch(index / frames_count), name.at)

        return encode_lazy_animation(still, frames_count, draw_frame, delay)


def render_leaderboard_card(**fields):
    """A map or Daily Cup leaderboard as PNG bytes.

    `players` is the board's top entries in the order to show them (ties
    keep it), each a dict: rank, name, time (already formatted), and
    optionally country (ISO alpha-2), tier (0 diamond .. 3 bronze, or None
    for no medal) and avatar_png (used for the top 3). The first three
    stand on the podium; the next seven go on the sheet, which says so
    when there's nobody past third. `thumbnail_png` (the map's, or the
    cup map's) fills the background; without it, the map card's sky fade.
    Username effects are ignored here; see render_leaderboard_card_animated.
    """
    card, _ = compose_leaderboard_card(**fields)
    out = io.BytesIO()
    card.save(out, format="PNG", optimize=False, compress_level=6)
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
    return _plan_rates(_loop_rates(effect))


def plan_shared_loop(effects):
    """plan_loop for several effects playing in one picture: one frame count
    and delay for all, and each effect's own cycle counts, in order."""
    rates = [((index,) + key, rate)
             for index, effect in enumerate(effects) for key, rate in _loop_rates(effect)]
    frames, delay, cycles = _plan_rates(rates)
    per_effect = [{} for _ in effects]
    for (index, *key), count in cycles.items():
        per_effect[index][tuple(key)] = count
    return frames, delay, per_effect


def _plan_rates(rates):
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
    """Draws the name banner's text for one frame of a username effect.

    `stroke_scale` thins the effect's own outline for a smaller name than
    the profile's (the map card's creator handle). `keep_outline` keeps the
    card's own outline at full width under an effect's thinner one, so a
    small name still reads when the effect's colours are close to what's
    behind it (gold on the orange podium block).
    """

    def __init__(self, banner, effect, cycles, stroke_scale=1.0, keep_outline=False):
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
            self.stroke_width = max(1, round(effect["size"] * SCALE * stroke_scale))
        # The label's box, which the gradients span.
        self.text_box = (px(tx), px(ty), px(tx + name.width), px(ty + name.height))
        outline = banner["outline"]

        outline_width = self.stroke_width
        if keep_outline:
            # The effect's stroke takes the inner half of the card's outline
            # at most, so a ring of the dark outline always shows outside it.
            outline_width = max(outline_width, round(name.stroke * SCALE / 2))
            self.stroke_width = max(1, min(self.stroke_width, outline_width // 2))
        self.fill_mask = self._mask(0)
        self.full_mask = self._mask(self.stroke_width)
        outline_mask = (self.full_mask if outline_width == self.stroke_width
                        else self._mask(outline_width))
        # Under every effect: the card's own shadow and purple outline, so
        # transparent parts of an effect fall back to the normal look.
        base = Image.new("RGBA", self.size, (0, 0, 0, 0))
        base.paste(outline, (0, 0), self._mask(outline_width, dy=px(name.shadow)))
        base.paste(outline, (0, 0), outline_mask)
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


# Room around the creator's handle for its outline, shadow and any flourish
# of an effect's font, in design px.
HANDLE_MARGIN = 14


class AnimatedName:
    """A name playing its username effect on a still card, one small patch
    per frame.

    The name is drawn upright on a patch with `margin` design px of room
    round it, turned with whatever it sits on (`rotate` degrees about
    `to_canvas`'s box) and laid over the card's own pixels there. `at` is
    the name box's top-left in that box's coordinates; `to_canvas` maps a
    point of the box to the card.
    """

    def __init__(self, card, name, at, effect, cycles, *, outline, stroke_scale,
                 to_canvas=lambda x, y: (x, y), rotate=0, margin=HANDLE_MARGIN,
                 keep_outline=False, shrink=2):
        """`shrink` is how much smaller than the card the frames are (2 for
        the half-size animations, 1 for full size)."""
        size = (px(name.width + 2 * margin), px(name.height + 2 * margin))
        size = (size[0] + size[0] % 2, size[1] + size[1] % 2)
        self.animator = NameAnimator({
            "name": name,
            "box": (0, 0) + size,
            "mask": Image.new("L", size, 255),
            "text_at": (margin, margin),
            "outline": outline,
        }, effect, cycles, stroke_scale=stroke_scale, keep_outline=keep_outline)
        self.rotate = rotate
        self.shrink = shrink

        # Where the patch's centre lands once its box has turned.
        x, y = at
        centre = to_canvas(x - margin + size[0] / SCALE / 2, y - margin + size[1] / SCALE / 2)
        turned = Image.new("L", size).rotate(-rotate, expand=True).size if rotate else size
        left = px(centre[0]) - turned[0] // 2
        top = px(centre[1]) - turned[1] // 2
        region = _even_box((left, top, left + turned[0], top + turned[1]), card.size)
        self.card_region = card.crop(region)
        self.offset = (left - region[0], top - region[1])
        # Where the (shrunk) patch goes on the (shrunk) frame.
        self.at = (region[0] // shrink, region[1] // shrink)
        self.patch_size = ((region[2] - region[0]) // shrink, (region[3] - region[1]) // shrink)

    def patch(self, phase):
        layer = self.animator.frame(phase)
        if self.rotate:
            layer = layer.rotate(-self.rotate, resample=Image.BICUBIC, expand=True)
        patch = self.card_region.copy()
        patch.alpha_composite(layer, self.offset)
        if self.shrink == 1:
            return patch
        return patch.resize(self.patch_size, Image.LANCZOS)


def encode_lazy_animation(first, count, draw_frame, delay):
    """Animated WebP bytes from `count` frames drawn as the encoder asks for
    them, so only one is ever held: `draw_frame(index, image)` paints frame
    `index` over `image`, which starts as `first` (and keeps the previous
    frame's pixels).

    Pillow's WebP writer walks a multi-frame image with seek(); a plain
    image given n_frames and a seek() that redraws it in place passes for
    one, without touching Pillow's internals.
    """
    image = first.copy()

    def seek(index):
        draw_frame(index, image)

    image.n_frames = count
    image.seek = seek
    out = io.BytesIO()
    image.save(out, format="WEBP", save_all=True, duration=delay, loop=0,
               quality=WEBP_QUALITY, method=WEBP_METHOD,
               kmin=0, kmax=0)  # see render_profile_card_animated
    return out.getvalue()


def encode_name_animation(card, names, frames_count, delay):
    """The card at half size with each AnimatedName playing, as WebP bytes.
    The names' patches must not overlap: each is pasted over the frame."""
    still = card.resize((card.width // 2, card.height // 2), Image.LANCZOS)
    frames = []
    for index in range(frames_count):
        frame = still.copy()
        for name in names:
            frame.paste(name.patch(index / frames_count), name.at)
        frames.append(frame)
    out = io.BytesIO()
    frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:],
                   duration=delay, loop=0, quality=WEBP_QUALITY, method=WEBP_METHOD,
                   kmin=0, kmax=0)  # see render_profile_card_animated
    return out.getvalue()


def render_map_card_animated(effect, **fields):
    """The map card with the creator's username effect playing on their
    handle, as animated WebP bytes (half resolution, like the profile's).

    The handle sits on the tilted polaroid, so each frame draws it upright
    on a small patch, turns the patch with the paper and lays it on the
    still card; nothing else on the card changes.
    """
    with ANIMATION_LOCK:
        card, (handle, at) = compose_map_card(
            handle_face=effect.get("font") or DISPLAY, **fields)
        frames_count, delay, cycles = plan_loop(effect)
        name = AnimatedName(card, handle, at, effect, cycles, outline=rgba(MAP_PURPLE),
                            stroke_scale=CREATOR_HANDLE_SIZE / NAME_SIZE,
                            to_canvas=paper_to_canvas, rotate=POLAROID_TILT)
        return encode_name_animation(card, [name], frames_count, delay)
