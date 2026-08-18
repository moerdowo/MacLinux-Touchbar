"""Palette, typography and drawing primitives for the Touch Bar.

Everything visual lives here so the daemon and the editor's preview cannot
drift apart: both import this module and call the same helpers, so what the
editor shows is what the strip renders, down to the pixel.

Text goes through PangoCairo rather than cairo's "toy" API. That buys three
things the toy API cannot do at 60px tall: real font fallback (so a Nerd Font
glyph next to Latin text resolves), colour emoji, and ellipsis on overflow --
which matters because a stock ticker or a track title has no fixed width.
"""

import math
import re

import cairo
import gi

gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo   # noqa: E402

# --- colour -------------------------------------------------------------

_HEX = re.compile(r'^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')


def parse_color(value, fallback=(1.0, 1.0, 1.0, 1.0)):
    """'#rrggbb', '#rgb', '#rrggbbaa' or an (r,g,b[,a]) tuple -> RGBA floats."""
    if isinstance(value, (tuple, list)):
        c = tuple(float(v) for v in value)
        return c if len(c) == 4 else (c + (1.0,))[:4]
    if not isinstance(value, str):
        return fallback
    m = _HEX.match(value.strip())
    if not m:
        return fallback
    h = m.group(1)
    if len(h) == 3:
        h = ''.join(ch * 2 for ch in h)
    vals = [int(h[i:i + 2], 16) / 255.0 for i in range(0, len(h), 2)]
    if len(vals) == 3:
        vals.append(1.0)
    return tuple(vals)


def to_hex(rgba):
    r, g, b = (max(0, min(255, round(c * 255))) for c in rgba[:3])
    return f'#{r:02x}{g:02x}{b:02x}'


def mix(a, b, t):
    """Linear blend of two RGBA tuples, t=0 -> a, t=1 -> b."""
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def with_alpha(rgba, alpha):
    return (rgba[0], rgba[1], rgba[2], alpha)


def luminance(rgba):
    r, g, b = rgba[:3]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def readable_on(bg):
    """Black or white, whichever stays legible on `bg`."""
    return (0.02, 0.02, 0.03, 1.0) if luminance(bg) > 0.5 else (1, 1, 1, 1)


def set_source(ctx, rgba):
    ctx.set_source_rgba(*rgba)


# --- theme --------------------------------------------------------------

#: Curated palettes. `custom` is whatever the user last edited by hand.
PRESETS = {
    'midnight': {
        'background': '#08080b', 'surface': '#16161c', 'surface_alt': '#21212a',
        'pressed': '#3a3a48', 'text': '#f0f0f5', 'text_dim': '#7d7d90',
        'accent': '#4c8dff', 'good': '#3ddc97', 'warn': '#ffb340', 'bad': '#ff5c6c',
    },
    'graphite': {
        'background': '#111111', 'surface': '#1e1e1e', 'surface_alt': '#2b2b2b',
        'pressed': '#454545', 'text': '#ededed', 'text_dim': '#8a8a8a',
        'accent': '#d4d4d4', 'good': '#9ece6a', 'warn': '#e0af68', 'bad': '#f7768e',
    },
    'nord': {
        'background': '#2e3440', 'surface': '#3b4252', 'surface_alt': '#434c5e',
        'pressed': '#4c566a', 'text': '#eceff4', 'text_dim': '#9aa5b8',
        'accent': '#88c0d0', 'good': '#a3be8c', 'warn': '#ebcb8b', 'bad': '#bf616a',
    },
    'tokyo': {
        'background': '#1a1b26', 'surface': '#24283b', 'surface_alt': '#2f334d',
        'pressed': '#414868', 'text': '#c0caf5', 'text_dim': '#787c99',
        'accent': '#7aa2f7', 'good': '#9ece6a', 'warn': '#e0af68', 'bad': '#f7768e',
    },
    'gruvbox': {
        'background': '#1d2021', 'surface': '#282828', 'surface_alt': '#3c3836',
        'pressed': '#504945', 'text': '#ebdbb2', 'text_dim': '#a89984',
        'accent': '#fabd2f', 'good': '#b8bb26', 'warn': '#fe8019', 'bad': '#fb4934',
    },
    'catppuccin': {
        'background': '#1e1e2e', 'surface': '#313244', 'surface_alt': '#45475a',
        'pressed': '#585b70', 'text': '#cdd6f4', 'text_dim': '#9399b2',
        'accent': '#cba6f7', 'good': '#a6e3a1', 'warn': '#f9e2af', 'bad': '#f38ba8',
    },
    'solarized': {
        'background': '#002b36', 'surface': '#073642', 'surface_alt': '#0d4b5a',
        'pressed': '#17616f', 'text': '#eee8d5', 'text_dim': '#93a1a1',
        'accent': '#2aa198', 'good': '#859900', 'warn': '#b58900', 'bad': '#dc322f',
    },
    'amber': {
        'background': '#0d0a06', 'surface': '#1c150c', 'surface_alt': '#2a2013',
        'pressed': '#3d2e1a', 'text': '#ffcc7a', 'text_dim': '#a17e4a',
        'accent': '#ffa726', 'good': '#c0ca33', 'warn': '#ff7043', 'bad': '#e53935',
    },
    'paper': {
        'background': '#f4f4f2', 'surface': '#ffffff', 'surface_alt': '#e8e8e4',
        'pressed': '#d0d0cc', 'text': '#1c1c1e', 'text_dim': '#75757a',
        'accent': '#0066cc', 'good': '#1a8a3f', 'warn': '#b26a00', 'bad': '#c62828',
    },
}

DEFAULT_THEME = dict(
    PRESETS['midnight'],
    preset='midnight',
    font='Noto Sans',
    icon_font='JetBrainsMono Nerd Font',
    radius=9,
    gap=6,
    padding=6,
    label_size=19,
    value_size=17,
    icon_size=22,
)


class Theme:
    """Resolved colours and metrics. Construct from the config's theme dict."""

    COLOR_KEYS = ('background', 'surface', 'surface_alt', 'pressed',
                  'text', 'text_dim', 'accent', 'good', 'warn', 'bad')
    METRIC_KEYS = ('radius', 'gap', 'padding',
                   'label_size', 'value_size', 'icon_size')

    def __init__(self, data=None):
        merged = dict(DEFAULT_THEME)
        merged.update(data or {})
        self.raw = merged
        for key in self.COLOR_KEYS:
            setattr(self, key, parse_color(merged.get(key),
                                           parse_color(DEFAULT_THEME[key])))
        for key in self.METRIC_KEYS:
            try:
                setattr(self, key, float(merged.get(key, DEFAULT_THEME[key])))
            except (TypeError, ValueError):
                setattr(self, key, float(DEFAULT_THEME[key]))
        self.font = merged.get('font') or DEFAULT_THEME['font']
        self.icon_font = merged.get('icon_font') or DEFAULT_THEME['icon_font']
        self.preset = merged.get('preset', 'custom')

    def color(self, name, fallback=None):
        """Look a colour up by role name, or parse it if it is a literal."""
        if isinstance(name, str) and name.startswith('#'):
            return parse_color(name)
        return getattr(self, name, fallback if fallback is not None else self.text)


# --- geometry -----------------------------------------------------------

def rounded_rect(ctx, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0.01:
        ctx.rectangle(x, y, w, h)
        return
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def fill_rounded(ctx, x, y, w, h, r, rgba):
    rounded_rect(ctx, x, y, w, h, r)
    set_source(ctx, rgba)
    ctx.fill()


def stroke_rounded(ctx, x, y, w, h, r, rgba, width=1.5):
    rounded_rect(ctx, x, y, w, h, r)
    set_source(ctx, rgba)
    ctx.set_line_width(width)
    ctx.stroke()


# --- text ---------------------------------------------------------------

ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT = 'left', 'center', 'right'


def _layout(ctx, text, font, size, weight, width=None, align=ALIGN_CENTER):
    layout = PangoCairo.create_layout(ctx)
    desc = Pango.FontDescription()
    desc.set_family(font)
    desc.set_absolute_size(size * Pango.SCALE)
    desc.set_weight(Pango.Weight.BOLD if weight == 'bold' else
                    Pango.Weight.SEMIBOLD if weight == 'semibold' else
                    Pango.Weight.NORMAL)
    layout.set_font_description(desc)
    layout.set_text(str(text), -1)
    layout.set_single_paragraph_mode(True)
    if width is not None:
        # Ellipsize rather than overflow: a long track title must not bleed
        # into the widget next door.
        layout.set_width(int(max(1, width) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_alignment({ALIGN_LEFT: Pango.Alignment.LEFT,
                              ALIGN_CENTER: Pango.Alignment.CENTER,
                              ALIGN_RIGHT: Pango.Alignment.RIGHT}[align])
    return layout


def text_size(ctx, text, font, size, weight='normal'):
    """Logical (width, height) of `text`, for measuring before laying out."""
    layout = _layout(ctx, text, font, size, weight)
    w, h = layout.get_pixel_size()
    return w, h


def draw_text(ctx, text, x, y, w, h, *, font, size, color,
              weight='normal', align=ALIGN_CENTER, valign='center', dy=0.0):
    """Draw one line clipped and ellipsized to the box, returning its width."""
    if text is None or text == '':
        return 0
    layout = _layout(ctx, text, font, size, weight, width=w, align=align)
    tw, th = layout.get_pixel_size()
    if valign == 'top':
        ty = y
    elif valign == 'bottom':
        ty = y + h - th
    else:
        ty = y + (h - th) / 2
    ctx.save()
    set_source(ctx, color)
    ctx.move_to(x, ty + dy)
    PangoCairo.show_layout(ctx, layout)
    ctx.restore()
    return min(tw, w)


def draw_emoji_or_icon(ctx, glyph, x, y, w, h, *, icon_font, size, color):
    """Icons are just text, but colour emoji must not be tinted.

    Pango renders colour-emoji glyphs from their own bitmaps; setting a source
    colour first would be ignored for those and applied to Nerd Font glyphs,
    so both paths go through the same call and the source is set regardless.
    """
    return draw_text(ctx, glyph, x, y, w, h, font=icon_font, size=size,
                     color=color, align=ALIGN_CENTER)


# --- data marks ---------------------------------------------------------

def draw_sparkline(ctx, values, x, y, w, h, rgba, *, fill=True, width=1.6):
    """A tiny line chart. Flat or empty series degrade to a baseline."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return
    lo, hi = min(pts), max(pts)
    span = hi - lo
    if span <= 0:
        span = 1.0
        lo -= 0.5
    step = w / (len(pts) - 1)
    coords = [(x + i * step, y + h - (v - lo) / span * h)
              for i, v in enumerate(pts)]

    if fill:
        ctx.save()
        ctx.move_to(coords[0][0], y + h)
        for px, py in coords:
            ctx.line_to(px, py)
        ctx.line_to(coords[-1][0], y + h)
        ctx.close_path()
        grad = cairo.LinearGradient(0, y, 0, y + h)
        grad.add_color_stop_rgba(0, rgba[0], rgba[1], rgba[2], 0.35)
        grad.add_color_stop_rgba(1, rgba[0], rgba[1], rgba[2], 0.0)
        ctx.set_source(grad)
        ctx.fill()
        ctx.restore()

    ctx.save()
    set_source(ctx, rgba)
    ctx.set_line_width(width)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.move_to(*coords[0])
    for px, py in coords[1:]:
        ctx.line_to(px, py)
    ctx.stroke()
    ctx.restore()


def draw_bars(ctx, values, x, y, w, h, rgba, *, hot=None, hot_at=0.75):
    """Histogram of 0..1 values -- the CPU/memory look."""
    if not values:
        return
    n = len(values)
    bw = w / n
    for i, v in enumerate(values):
        v = max(0.0, min(1.0, v or 0.0))
        bh = max(1.0, v * h)
        set_source(ctx, hot if (hot and v >= hot_at) else rgba)
        ctx.rectangle(x + i * bw, y + h - bh, max(1.0, bw - 1.0), bh)
        ctx.fill()


def draw_track(ctx, x, y, w, h, fraction, *, track_rgba, fill_rgba, radius=None):
    """A rounded progress track -- volume, brightness, battery, media seek."""
    r = h / 2 if radius is None else radius
    fill_rounded(ctx, x, y, w, h, r, track_rgba)
    frac = max(0.0, min(1.0, fraction or 0.0))
    if frac > 0:
        fw = max(h if frac > 0.02 else 0, w * frac)
        ctx.save()
        rounded_rect(ctx, x, y, w, h, r)
        ctx.clip()
        fill_rounded(ctx, x, y, fw, h, r, fill_rgba)
        ctx.restore()


#: Icons are stored as codepoints, never as literal characters: a glyph in the
#: Private Use Area does not survive every editor, terminal or copy-paste on its
#: way into this file, and a silently empty string is a very confusing bug.
#: Values are Nerd Font (Font Awesome range) codepoints, verified by rendering.
ICON_CODEPOINTS = {
    'terminal': 0xf120, 'folder': 0xf07b, 'globe': 0xf0ac, 'code': 0xf121,
    'search': 0xf002, 'settings': 0xf013, 'lock': 0xf023, 'power': 0xf011,
    'refresh': 0xf021, 'home': 0xf015, 'star': 0xf005, 'heart': 0xf004,
    'bell': 0xf0f3, 'camera': 0xf030, 'clipboard': 0xf0ea, 'book': 0xf02d,
    'play': 0xf04b, 'pause': 0xf04c, 'stop': 0xf04d, 'next': 0xf051,
    'previous': 0xf048, 'shuffle': 0xf074, 'repeat': 0xf01e,
    'volume-up': 0xf028, 'volume-down': 0xf027, 'mute': 0xf026,
    'brightness-up': 0xf185, 'brightness-down': 0xf186,
    'wifi': 0xf1eb, 'bluetooth': 0xf293, 'battery': 0xf240,
    'cpu': 0xf2db, 'memory': 0xf233, 'thermometer': 0xf2c7, 'disk': 0xf0a0,
    'clock': 0xf017, 'calendar': 0xf073, 'mail': 0xf0e0, 'chat': 0xf075,
    'chart': 0xf201, 'bitcoin': 0xf15a, 'dollar': 0xf155,
    'trending-up': 0xf062, 'trending-down': 0xf063,
    'cloud': 0xf0c2, 'sun': 0xf185, 'moon': 0xf186, 'rain': 0xf043,
    'snow': 0xf2dc, 'thunder': 0xf0e7, 'fog': 0xf0c2,
    'keyboard': 0xf11c, 'display': 0xf108, 'layers': 0xf24d, 'grid': 0xf00a,
    'arrow-left': 0xf060, 'arrow-right': 0xf061,
    'arrow-up': 0xf062, 'arrow-down': 0xf063,
    'plus': 0xf067, 'minus': 0xf068, 'close': 0xf00d, 'check': 0xf00c,
    'copy': 0xf0c5, 'undo': 0xf0e2, 'save': 0xf0c7, 'edit': 0xf044,
    'rocket': 0xf135, 'bug': 0xf188, 'flask': 0xf0c3, 'music': 0xf001,
    'image': 0xf03e, 'text': 0xf031, 'space': 0xf07e, 'sliders': 0xf1de,
    'gauge': 0xf0e4, 'network': 0xf0ec, 'workspace': 0xf009, 'pages': 0xf0c9,
    'apps': 0xf009, 'download': 0xf019, 'upload': 0xf093,
}

ICONS = {name: chr(cp) for name, cp in ICON_CODEPOINTS.items()}


def resolve_icon(value):
    """Accept a catalogue name, a literal character, or an emoji.

    Config files carry names ('terminal') rather than raw glyphs so they stay
    readable and survive editing; users pasting an emoji straight into the
    field still works, because anything short that is not a known name is
    passed through untouched.
    """
    if not value:
        return ''
    text = str(value)
    if text in ICONS:
        return ICONS[text]
    return text if len(text) <= 4 else ''
