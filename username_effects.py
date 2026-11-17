"""The game's username effects, ported from the ezVisualz library.

In game, username effects are ezVisualz presets
(https://github.com/arxk/ezVisualz, MIT): a UIGradient on the name's fill
and/or one on its UIStroke, whose colours scroll (an "offset speed"),
optionally tilted, spun or wobbled, sometimes with a moving transparency
band or a pulsing stroke width. Every preset below is a transcription of
lib/EasyVisuals/Presets/<Name>.luau into data card_render can animate.

What the game passes in -- speed, size, custom colour per effect -- isn't
known to the bot yet, so DEFAULT_SPEEDS and DEFAULT_COLORS hold the values
from the library's own example script (testing/TestTextEffects.client.luau).
The library's built-in default speed (0.007) barely moves, so it's no use
here. Replace these, or add to EFFECT_OVERRIDES, once the game's numbers
are in.

Units, matching the library:
  speed      gradient offset in sequence-lengths per second (1 = one full
             cycle of the colours each second)
  angle      UIGradient.Rotation in degrees, clockwise on screen
  spin       degrees per second
  wobble     (centre, amplitude in degrees, angular frequency in rad/s)
  pulse      stroke width multiplier (low, high, angular frequency in rad/s)
"""

# The templates and presets below are ported from ezVisualz:
#
# MIT License
#
# Copyright (c) 2026 Arxk
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import re

# lib/EasyVisuals/GradientTemplates, as (colour keypoints, transparency
# keypoints or None). Keypoint times are 0-1; colours are 0-255 RGB.
TEMPLATES = {
    "Bubblegum": ([(0.0, (243, 101, 177)), (0.5, (255, 167, 227)), (1.0, (243, 101, 177))],
                  [(0.0, 0.0), (0.0989, 0.0), (0.5, 1.0), (0.8997, 0.0), (1.0, 0.0)]),
    "Chrome": ([(0.0, (0, 16, 90)), (0.0677, (175, 201, 213)), (0.1719, (224, 240, 255)),
                (0.3177, (255, 180, 255)), (0.467, (68, 0, 127)), (0.4983, (5, 1, 9)),
                (0.5035, (255, 255, 255)), (0.7188, (180, 255, 243)), (0.941, (39, 159, 175)),
                (1.0, (0, 16, 90))], None),
    "Death": ([(0.0, (153, 0, 0)), (0.125, (255, 0, 0)), (0.2552, (105, 0, 0)), (0.3767, (76, 0, 0)),
               (0.5, (176, 0, 0)), (0.6198, (76, 0, 0)), (0.7378, (255, 0, 0)), (0.8681, (76, 0, 0)),
               (1.0, (153, 0, 0))], None),
    "Electric": ([(0.0, (0, 150, 255)), (0.15, (255, 255, 255)), (0.3, (100, 200, 255)),
                  (0.45, (255, 255, 255)), (0.6, (0, 200, 255)), (0.75, (255, 255, 255)),
                  (0.9, (50, 150, 255)), (1.0, (0, 150, 255))], None),
    "Fire": ([(0.0, (255, 234, 0)), (0.151, (255, 234, 0)), (0.3073, (255, 153, 0)),
              (0.4965, (139, 94, 9)), (0.6649, (255, 170, 0)), (0.8385, (173, 125, 29)),
              (1.0, (255, 234, 0))], None),
    "Galaxy": ([(0.0, (75, 0, 130)), (0.25, (138, 43, 226)), (0.5, (25, 25, 112)),
                (0.75, (72, 61, 139)), (1.0, (75, 0, 130))], None),
    "Ghost": ([(0.0, (255, 255, 255)), (0.5, (181, 184, 188)), (1.0, (255, 255, 255))],
              [(0.0, 0.0), (0.0989, 0.0), (0.5, 1.0), (0.8997, 0.0), (1.0, 0.0)]),
    "Gold": ([(0.0, (213, 174, 56)), (0.2205, (213, 174, 56)), (0.3681, (199, 152, 12)),
              (0.4983, (217, 178, 3)), (0.5035, (255, 236, 129)), (0.6927, (213, 174, 56)),
              (0.7917, (213, 174, 56)), (1.0, (213, 174, 56))], None),
    "Green": ([(0.0, (0, 255, 0)), (0.5, (0, 255, 0)), (1.0, (0, 255, 0))],
              [(0.0, 0.0), (0.7, 1.0), (1.0, 1.0)]),
    "Ice": ([(0.0, (109, 210, 212)), (0.1719, (224, 240, 255)), (0.3177, (140, 208, 209)),
             (0.467, (109, 210, 212)), (0.4983, (109, 210, 212)), (0.5035, (255, 255, 255)),
             (0.7188, (180, 255, 243)), (1.0, (109, 210, 212))], None),
    "Lava": ([(0.0, (255, 0, 0)), (0.125, (255, 200, 0)), (0.2552, (255, 0, 0)),
              (0.3767, (255, 200, 0)), (0.5, (255, 0, 0)), (0.6198, (255, 200, 0)),
              (0.7378, (255, 0, 0)), (0.8681, (255, 200, 0)), (1.0, (255, 0, 0))], None),
    "Matrix": ([(0.0, (62, 239, 2)), (0.497, (62, 239, 2)), (0.503, (0, 0, 0)), (1.0, (0, 0, 0))], None),
    "Neon": ([(0.0, (255, 0, 255)), (0.2, (0, 255, 255)), (0.4, (255, 0, 255)),
              (0.6, (255, 0, 150)), (0.8, (0, 255, 255)), (1.0, (255, 0, 255))], None),
    "Oceanic": ([(0.0, (0, 115, 255)), (0.125, (255, 0, 0)), (0.2552, (0, 115, 255)),
                 (0.3767, (255, 0, 0)), (0.5, (0, 115, 255)), (0.6198, (255, 0, 0)),
                 (0.7375, (0, 115, 255)), (0.8681, (255, 0, 0)), (1.0, (0, 115, 255))], None),
    "Rainbow": ([(0.0, (255, 0, 0)), (0.151, (157, 0, 255)), (0.3073, (17, 0, 255)),
                 (0.4965, (0, 255, 255)), (0.6649, (4, 255, 0)), (0.8385, (255, 255, 0)),
                 (1.0, (255, 0, 0))], None),
    "Silver": ([(0.0, (213, 213, 213)), (0.2847, (187, 200, 213)), (0.4983, (85, 85, 112)),
                (0.5035, (255, 255, 255)), (0.7153, (187, 200, 213)), (1.0, (213, 213, 213))], None),
    "Sunset": ([(0.0, (255, 94, 77)), (0.25, (255, 154, 0)), (0.5, (255, 192, 203)),
                (0.75, (255, 20, 147)), (1.0, (255, 94, 77))], None),
    "Zebra": ([(0.0, (255, 255, 255)), (0.497, (255, 255, 255)), (0.503, (0, 0, 0)), (1.0, (0, 0, 0))], None),
}

# The speed each preset runs at in the library's example script.
DEFAULT_SPEEDS = {
    "Rainbow": 0.35, "RainbowStroke": 0.5, "RainbowOutline": 75,
    "Lava": 0.35, "LavaStroke": 0.35,
    "Ghost": 0.2, "GhostStroke": 0.2,
    "Gold": 0.4, "GoldStroke": 0.55,
    "Silver": 0.4, "SilverStroke": 0.45,   # Silver isn't in the example; Gold's
    "ChromeStroke": 0.35, "DeathStroke": 0.2, "IceStroke": 0.35,
    "Bubblegum": 1, "OceanicStroke": 0.2,
    "Zebra": 0.4, "Matrix": 0.5,
    "FireStroke": 0.5, "NeonStroke": 0.6, "GalaxyStroke": 0.4,
    "ElectricStroke": 0.5, "SunsetStroke": 0.45,
    "Shine": 0.65, "ShineOutline": 75, "WaveStroke": 0.55,
    "GreenOutline": 75,
}

# Presets built around a custom colour, with the example script's choice.
DEFAULT_COLORS = {
    "Shine": (187, 0, 255),
    "ShineOutline": (78, 177, 238),
    "WaveStroke": (0, 247, 255),
}

# Game effect name -> (preset, speed or None, colour or None), for names that
# don't match a preset on their own or that run at their own speed in game.
EFFECT_OVERRIDES = {}


def _lighter(color, amount):
    return tuple(round(c + (255 - c) * amount) for c in color)


def _gradient(template=None, *, colors=None, transparency=None, angle=0.0,
              speed=0.0, offset=0.0, transparency_speed=0.0):
    if template is not None:
        colors, template_transparency = TEMPLATES[template]
        transparency = transparency if transparency is not None else template_transparency
    return {
        "colors": colors,
        "transparency": transparency,
        "angle": angle,          # float, ("spin", start, deg/s) or ("wobble", centre, amp, rad/s)
        "speed": speed,          # colour offset, cycles per second
        "offset": offset,        # starting colour offset
        "transparency_speed": transparency_speed,
    }


def _spin(start, rate):
    return ("spin", start, rate)


def _wobble(centre, amplitude, rate):
    return ("wobble", centre, amplitude, rate)


def build_preset(name, speed=None, color=None):
    """The effect spec for a preset: {"fill", "stroke", "pulse"}, or None."""
    s = DEFAULT_SPEEDS.get(name) if speed is None else speed
    c = tuple(color) if color else DEFAULT_COLORS.get(name, (255, 255, 255))
    if s is None:
        return None
    fill = stroke = pulse = None

    if name == "Rainbow":
        fill = _gradient("Rainbow", speed=s)
    elif name == "RainbowStroke":
        fill = _gradient("Rainbow", speed=s)
        stroke = _gradient("Rainbow", speed=-s)
    elif name == "RainbowOutline":
        stroke = _gradient("Rainbow", angle=_spin(5, s))
    elif name == "Lava":
        fill = _gradient("Lava", speed=s)
    elif name == "LavaStroke":
        fill = _gradient("Lava", speed=s)
        stroke = _gradient("Lava", speed=-s)
    elif name == "Ghost":
        fill = _gradient("Ghost", speed=s, transparency_speed=s * 0.9)
    elif name == "GhostStroke":
        fill = _gradient("Ghost", speed=s, transparency_speed=s * 0.9)
        stroke = _gradient("Ghost", speed=-s * 0.9, transparency_speed=-s * 0.9)
    elif name == "Gold":
        fill = _gradient("Gold", angle=-75, speed=s)
    elif name == "GoldStroke":
        fill = _gradient("Gold", angle=-75, speed=s)
        stroke = _gradient("Gold", angle=75, speed=-s)
    elif name == "Silver":
        fill = _gradient("Silver", angle=-75, speed=s)
    elif name == "SilverStroke":
        fill = _gradient("Silver", angle=-80, speed=s)
        stroke = _gradient("Silver", angle=-79, speed=s * 0.56)
    elif name == "ChromeStroke":
        fill = _gradient("Chrome", angle=-90, speed=s)
        stroke = _gradient("Chrome", angle=-89, speed=s * 0.58)
    elif name == "DeathStroke":
        fill = _gradient("Death", angle=-90, speed=s)
        stroke = _gradient("Death", angle=-85, speed=-s - 0.001)
    elif name == "IceStroke":
        fill = _gradient("Ice", angle=-65, speed=s)
        stroke = _gradient("Ice", angle=-61, speed=s * 0.23)
    elif name == "Bubblegum":
        # The template's transparency isn't used: Gradient.new gets 0.
        fill = _gradient("Bubblegum", transparency=[(0, 0.0), (1, 0.0)], angle=-90, speed=s)
        stroke = _gradient("Bubblegum", transparency=[(0, 0.0), (1, 0.0)], angle=-45, speed=s * 0.9)
        # size * (0.5 .. 3.25), following sin(tick() * speed).
        pulse = (0.5, 3.25, s)
    elif name == "OceanicStroke":
        # SetRotationSpeed(0.1) replaces the -75 before it ever applies.
        fill = _gradient("Oceanic", angle=_spin(0, 0.1), speed=s)
        stroke = _gradient("Oceanic", speed=-s)
    elif name in ("Zebra", "Matrix"):
        fill = _gradient(name, angle=90, speed=s)
        stroke = _gradient(name, angle=90, offset=0.5, speed=s)
    elif name == "Shine":
        fill = _gradient(colors=[(0, c), (0.5, _lighter(c, 0.417505)), (1, c)], angle=60, speed=s * 0.6)
    elif name == "ShineOutline":
        stroke = _gradient(colors=[(0, c), (0.5, _lighter(c, 0.6)), (1, c)], angle=_spin(0.75, s))
    elif name == "GreenOutline":
        stroke = _gradient("Green", angle=_spin(5, s))
    elif name == "WaveStroke":
        stroke = _gradient(colors=[(0, c), (0.5, c), (1, c)],
                           transparency=[(0, 0.0), (0.25, 1.0), (0.5, 1.0), (0.75, 1.0), (1, 0.0)],
                           speed=s, transparency_speed=s * 0.9)
    elif name == "NeonStroke":
        fill = _gradient("Neon", angle=_spin(0, s * 30), speed=s)
        stroke = _gradient("Neon", angle=-45, speed=-s * 0.7)
    elif name == "GalaxyStroke":
        fill = _gradient("Galaxy", angle=_spin(0, s * 15), speed=s * 0.5)
        stroke = _gradient("Galaxy", angle=_spin(0, -s * 15), speed=-s * 0.3)
    elif name == "FireStroke":
        # The heartbeat loop overrides the -75 with a slow climb from 0.
        fill = _gradient("Fire", angle=_spin(0, s), speed=s)
        stroke = _gradient("Fire", angle=75, speed=-s)
    elif name == "ElectricStroke":
        fill = _gradient("Electric", angle=_wobble(-60, 30, s * 50 * 0.05), speed=s * 2.5)
        stroke = _gradient("Electric", angle=_wobble(60, -30, s * 45 * 0.05), speed=-s * 2)
        pulse = (1.0, 1.4, s * 3)
    elif name == "SunsetStroke":
        fill = _gradient("Sunset", angle=_wobble(-60, 20, s * 20 * 0.1), speed=s * 0.8)
        stroke = _gradient("Sunset", angle=_wobble(60, -20, s * 20 * 0.1), speed=-s * 0.6)
    else:
        return None
    return {"name": name, "fill": fill, "stroke": stroke, "pulse": pulse}


PRESET_NAMES = tuple(DEFAULT_SPEEDS)


def _normalize(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


_BY_NORMALIZED = {_normalize(name): name for name in PRESET_NAMES}


def resolve_effect(effect_name):
    """A game effect name (or a preset name) -> effect spec, or None.

    Overrides win; otherwise the name is matched to a preset ignoring case,
    spaces and punctuation, also trying it with "Stroke" on the end and
    without a trailing "effect"/"name".
    """
    if not effect_name:
        return None
    override = EFFECT_OVERRIDES.get(effect_name)
    if override:
        preset, speed, color = override
        return build_preset(preset, speed, color)

    key = _normalize(effect_name)
    for suffix in ("usernameeffect", "nameeffect", "effect", "username", "name"):
        if key.endswith(suffix) and len(key) > len(suffix):
            key = key[: -len(suffix)]
            break
    for candidate in (key, key + "stroke"):
        if candidate in _BY_NORMALIZED:
            return build_preset(_BY_NORMALIZED[candidate])
    return None
