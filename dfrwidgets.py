"""Widget catalogue, layout solver and strip renderer.

This module is the single source of truth for what the Touch Bar looks like.
The daemon draws with it onto a DRM dumb buffer; the editor draws with it onto
a GTK canvas. Neither has any private rendering code, so the preview cannot
lie about what the hardware will show.

Every widget class carries a SCHEMA: a declarative list of its editable
properties. The editor builds its entire property panel from that, so adding a
widget type here makes it appear in the GUI with no GUI code at all.
"""

import math
import os
import time

import cairo

import dfrtheme as T

STRIP_W, STRIP_H = 2170, 60

#: Nothing is allowed to shrink below this: a 4px widget is just a glitch.
MIN_WIDGET_W = 26.0

#: Brightness steps an LED cell is drawn in. Fine enough to be invisible,
#: coarse enough that an unchanged column really is bit-identical.
CELL_STEPS = 64.0


# --- schema helpers -----------------------------------------------------

def field(key, label, kind='text', default=None, **extra):
    """One editable property. `kind` drives which control the editor builds."""
    spec = {'key': key, 'label': label, 'kind': kind, 'default': default}
    spec.update(extra)
    return spec


STYLES = ['normal', 'accent', 'ghost', 'danger', 'good']

STYLE_FIELD = field('style', 'Style', 'choice', 'normal', options=STYLES,
                    help='Fill and text colour role')


# --- render environment -------------------------------------------------

class Env:
    """Everything a widget needs that is not its own configuration."""

    def __init__(self, theme, hub, config, page_id, *, now=None,
                 pressed_id=None, selected_id=None, preview=False):
        self.theme = theme
        self.hub = hub
        self.config = config
        self.page_id = page_id
        self.now = now if now is not None else time.time()
        self.pressed_id = pressed_id
        self.selected_id = selected_id
        self.preview = preview

    def feed(self, kind, **params):
        if self.hub is None:
            return {'_ok': False, '_error': 'no hub', '_rev': 0}
        return self.hub.get(kind, **params)


# --- base ---------------------------------------------------------------

class Widget:
    TYPE = 'abstract'
    LABEL = 'Widget'
    ICON = ''
    CATEGORY = 'Other'
    DESCRIPTION = ''
    SCHEMA = []
    DEFAULT_WIDTH = 120
    INTERACTIVE = False          # responds to drag, not just tap
    HAS_SURFACE = True           # draws its own rounded background

    def __init__(self, spec):
        self.spec = spec
        self.id = spec.get('id', '?')
        self.props = spec.get('props') or {}
        self.action = spec.get('action')
        self.long_action = spec.get('long_action')
        self.width = spec.get('width')
        self.flex = spec.get('flex')
        self.x = self.y = 0.0
        self.w, self.h = float(self.width or self.DEFAULT_WIDTH), float(STRIP_H)
        self._rev = None

    # -- configuration ------------------------------------------------

    def prop(self, key, default=None):
        value = self.props.get(key)
        if value is None or value == '':
            for f in self.SCHEMA:
                if f['key'] == key:
                    return f.get('default') if default is None else default
            return default
        return value

    def num(self, key, default=0.0):
        try:
            return float(self.prop(key, default))
        except (TypeError, ValueError):
            return default

    def flag(self, key, default=False):
        value = self.prop(key, default)
        return bool(value) if not isinstance(value, str) else value.lower() in (
            '1', 'true', 'yes', 'on')

    # -- lifecycle ----------------------------------------------------

    def feeds(self):
        """[(kind, params)] this widget reads, for change detection."""
        return []

    def revision(self, env):
        """Sum of feed revisions -- changes when any input changed."""
        if env.hub is None:
            return 0
        return sum(env.hub.revision(kind, **params) for kind, params in self.feeds())

    def tick_interval(self):
        """Seconds between forced repaints when no feed drives the widget."""
        return None

    def damage(self):
        """Sub-rect of this widget whose pixels changed in the last draw.

        None means "assume all of it", which is right for anything that
        redraws a whole small widget. A widget spanning the strip is a
        different matter: pushing its damage rect costs more than drawing it,
        so one that can say which part moved should. Returning an empty rect
        means nothing changed and the flush can be skipped entirely.
        """
        return None

    def natural_width(self, ctx, env):
        return float(self.width or self.DEFAULT_WIDTH)

    def contains(self, px, py):
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    # -- painting -----------------------------------------------------

    def surface_color(self, env):
        theme = env.theme
        style = self.prop('style', 'normal')
        if env.pressed_id == self.id:
            return theme.pressed
        return {
            'ghost': T.with_alpha(theme.surface, 0.0),
            'accent': T.mix(theme.surface, theme.accent, 0.22),
            'danger': T.mix(theme.surface, theme.bad, 0.22),
            'good': T.mix(theme.surface, theme.good, 0.22),
        }.get(style, theme.surface)

    def content_color(self, env):
        theme = env.theme
        return {
            'accent': theme.accent,
            'danger': theme.bad,
            'good': theme.good,
        }.get(self.prop('style', 'normal'), theme.text)

    def paint_surface(self, ctx, env):
        if not self.HAS_SURFACE:
            return
        color = self.surface_color(env)
        if color[3] > 0.01:
            T.fill_rounded(ctx, self.x, self.y, self.w, self.h,
                           env.theme.radius, color)

    def draw(self, ctx, env):
        # Clear our own rectangle first. The daemon repaints single widgets and
        # flushes only their damage rect, so a widget whose content shrank must
        # not leave the previous frame showing around the edges.
        T.set_source(ctx, env.theme.background)
        ctx.rectangle(self.x, self.y, self.w, self.h)
        ctx.fill()
        self.paint_surface(ctx, env)
        self.draw_content(ctx, env)
        if env.selected_id == self.id:
            T.stroke_rounded(ctx, self.x + 1, self.y + 1, self.w - 2, self.h - 2,
                             env.theme.radius, env.theme.accent, 2.0)

    def draw_content(self, ctx, env):
        pass

    # -- interaction --------------------------------------------------

    def tap(self, env, point):
        """Return an action dict for a tap, or None."""
        return self.action

    def long_press(self, env, point):
        return self.long_action

    def drag(self, env, point):
        return None

    # -- helpers ------------------------------------------------------

    def pad(self):
        return 10.0

    def inner(self):
        p = self.pad()
        return self.x + p, self.y, self.w - 2 * p, self.h


# --- static content -----------------------------------------------------

class ButtonWidget(Widget):
    TYPE, LABEL, ICON = 'button', 'Button', 'apps'
    CATEGORY = 'Basics'
    DESCRIPTION = 'A key or launcher. Icon, text, or both.'
    DEFAULT_WIDTH = 110
    SCHEMA = [
        field('label', 'Label', 'text', ''),
        field('icon', 'Icon', 'icon', '', help='Nerd Font glyph or emoji'),
        field('image', 'Image', 'file', '', help='Optional PNG/SVG instead of an icon'),
        STYLE_FIELD,
        field('layout', 'Arrangement', 'choice', 'auto',
              options=['auto', 'icon only', 'text only', 'icon above', 'icon left']),
        field('font_size', 'Text size', 'int', 0, min=0, max=48,
              help='0 follows the theme'),
    ]

    def natural_width(self, ctx, env):
        if self.width:
            return float(self.width)
        theme = env.theme
        label = str(self.prop('label', ''))
        size = self.num('font_size') or theme.label_size
        w = 0.0
        if label:
            w += T.text_size(ctx, label, theme.font, size, 'semibold')[0]
        if self.prop('icon') or self.prop('image'):
            w += theme.icon_size + (11 if label else 0)
        return max(52.0, w + 30)

    def _arrangement(self):
        mode = self.prop('layout', 'auto')
        icon = T.resolve_icon(self.prop('icon', ''))
        image = str(self.prop('image', '') or '')
        label = str(self.prop('label', '') or '')
        has_icon = bool(icon or image)
        if mode == 'icon only':
            return has_icon, False, True
        if mode == 'text only':
            return False, bool(label), False
        if mode == 'icon above':
            return has_icon, bool(label), False
        if mode == 'icon left':
            return has_icon, bool(label), False
        return has_icon, bool(label), not label     # auto

    def draw_content(self, ctx, env):
        theme = env.theme
        color = self.content_color(env)
        icon = T.resolve_icon(self.prop('icon', ''))
        image = str(self.prop('image', '') or '')
        label = str(self.prop('label', '') or '')
        mode = self.prop('layout', 'auto')
        show_icon, show_label, icon_centered = self._arrangement()
        x, y, w, h = self.inner()
        size = self.num('font_size') or theme.label_size

        if show_icon and image:
            draw_image(ctx, image, x, y + 6, w if not show_label else min(w, h - 12),
                       h - 12, fit='contain')
            if show_label:
                T.draw_text(ctx, label, x + h - 6, y, w - h + 6, h,
                            font=theme.font, size=size, color=color,
                            weight='semibold', align=T.ALIGN_LEFT)
            return

        if show_icon and show_label and mode == 'icon above':
            T.draw_text(ctx, icon, x, y + 2, w, h * 0.55, font=theme.icon_font,
                        size=theme.icon_size * 0.8, color=color)
            T.draw_text(ctx, label, x, y + h * 0.52, w, h * 0.44 - 4,
                        font=theme.font, size=size * 0.72, color=color,
                        weight='semibold')
        elif show_icon and show_label:
            gw = theme.icon_size + 4
            T.draw_text(ctx, icon, x, y, gw, h, font=theme.icon_font,
                        size=theme.icon_size, color=color)
            T.draw_text(ctx, label, x + gw + 7, y, w - gw - 7, h,
                        font=theme.font, size=size, color=color,
                        weight='semibold', align=T.ALIGN_LEFT)
        elif show_icon:
            T.draw_text(ctx, icon, x, y, w, h, font=theme.icon_font,
                        size=theme.icon_size, color=color)
        elif show_label:
            T.draw_text(ctx, label, x, y, w, h, font=theme.font, size=size,
                        color=color, weight='semibold')


class LabelWidget(Widget):
    TYPE, LABEL, ICON = 'label', 'Text', 'text'
    CATEGORY = 'Basics'
    DESCRIPTION = 'Static text with no background.'
    DEFAULT_WIDTH = 160
    HAS_SURFACE = False
    SCHEMA = [
        field('text', 'Text', 'text', 'Hello'),
        field('size', 'Size', 'int', 20, min=8, max=48),
        field('weight', 'Weight', 'choice', 'semibold',
              options=['normal', 'semibold', 'bold']),
        field('align', 'Align', 'choice', 'center',
              options=['left', 'center', 'right']),
        field('color', 'Colour', 'choice', 'text',
              options=['text', 'text_dim', 'accent', 'good', 'warn', 'bad']),
    ]

    def natural_width(self, ctx, env):
        if self.width:
            return float(self.width)
        w = T.text_size(ctx, str(self.prop('text', '')), env.theme.font,
                        self.num('size', 20), self.prop('weight'))[0]
        return max(30.0, w + 16)

    def draw_content(self, ctx, env):
        x, y, w, h = self.inner()
        T.draw_text(ctx, str(self.prop('text', '')), x, y, w, h,
                    font=env.theme.font, size=self.num('size', 20),
                    color=env.theme.color(self.prop('color', 'text')),
                    weight=self.prop('weight', 'semibold'),
                    align=self.prop('align', 'center'))


class SpacerWidget(Widget):
    TYPE, LABEL, ICON = 'spacer', 'Spacer', 'space'
    CATEGORY = 'Basics'
    DESCRIPTION = 'Empty space. Set flex to push neighbours apart.'
    DEFAULT_WIDTH = 40
    HAS_SURFACE = False
    SCHEMA = [field('line', 'Show divider', 'bool', False)]

    def draw_content(self, ctx, env):
        if self.flag('line'):
            cx = self.x + self.w / 2
            T.set_source(ctx, T.with_alpha(env.theme.text_dim, 0.4))
            ctx.set_line_width(1.0)
            ctx.move_to(cx, self.y + 14)
            ctx.line_to(cx, self.y + self.h - 14)
            ctx.stroke()

    def tap(self, env, point):
        return self.action


_IMAGE_CACHE = {}


def draw_image(ctx, path, x, y, w, h, fit='contain', opacity=1.0):
    """Draw a PNG/JPEG/SVG scaled into the box. Missing files draw nothing."""
    if not path or w <= 0 or h <= 0:
        return False
    path = os.path.expanduser(str(path))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    key = (path, mtime)
    entry = _IMAGE_CACHE.get(key)
    if entry is None:
        entry = _load_image(path)
        if entry is None:
            return False
        _IMAGE_CACHE[key] = entry
        if len(_IMAGE_CACHE) > 64:
            _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))

    kind, payload, iw, ih = entry
    if not iw or not ih:
        return False
    if fit == 'fill':
        sx, sy = w / iw, h / ih
    else:
        scale = min(w / iw, h / ih) if fit == 'contain' else max(w / iw, h / ih)
        sx = sy = scale
    dw, dh = iw * sx, ih * sy
    ox, oy = x + (w - dw) / 2, y + (h - dh) / 2

    ctx.save()
    ctx.rectangle(x, y, w, h)
    ctx.clip()
    ctx.translate(ox, oy)
    ctx.scale(sx, sy)
    if kind == 'svg':
        import gi
        gi.require_version('Rsvg', '2.0')
        from gi.repository import Rsvg
        vp = Rsvg.Rectangle()
        vp.x, vp.y, vp.width, vp.height = 0, 0, iw, ih
        payload.render_document(ctx, vp)
    else:
        ctx.set_source_surface(payload, 0, 0)
        if opacity >= 0.999:
            ctx.paint()
        else:
            ctx.paint_with_alpha(opacity)
    ctx.restore()
    return True


def _load_image(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == '.svg':
            import gi
            gi.require_version('Rsvg', '2.0')
            from gi.repository import Rsvg
            handle = Rsvg.Handle.new_from_file(path)
            dim = handle.get_intrinsic_dimensions()
            iw = dim.out_width.length if dim.has_width else 100
            ih = dim.out_height.length if dim.has_height else 100
            if not iw or not ih:
                iw = ih = 100
            return ('svg', handle, float(iw), float(ih))
        if ext == '.png':
            surf = cairo.ImageSurface.create_from_png(path)
            return ('raster', surf, float(surf.get_width()), float(surf.get_height()))
        import gi
        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file(path)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pb.get_width(), pb.get_height())
        tmp = cairo.Context(surf)
        import gi as _gi
        _gi.require_version('Gdk', '4.0')
        from gi.repository import Gdk
        Gdk.cairo_set_source_pixbuf(tmp, pb, 0, 0)
        tmp.paint()
        return ('raster', surf, float(pb.get_width()), float(pb.get_height()))
    except Exception:                                  # noqa: BLE001
        return None


class ImageWidget(Widget):
    TYPE, LABEL, ICON = 'image', 'Image', 'image'
    CATEGORY = 'Basics'
    DESCRIPTION = 'Any PNG, JPEG or SVG -- logo, avatar, artwork.'
    DEFAULT_WIDTH = 100
    SCHEMA = [
        field('path', 'File', 'file', ''),
        field('fit', 'Fit', 'choice', 'contain', options=['contain', 'cover', 'fill']),
        field('opacity', 'Opacity', 'float', 1.0, min=0.05, max=1.0, step=0.05),
        field('inset', 'Inset', 'int', 6, min=0, max=24),
        field('background', 'Draw background', 'bool', False),
    ]

    def paint_surface(self, ctx, env):
        if self.flag('background') or env.pressed_id == self.id:
            T.fill_rounded(ctx, self.x, self.y, self.w, self.h,
                           env.theme.radius, self.surface_color(env))

    def draw_content(self, ctx, env):
        inset = self.num('inset', 6)
        ok = draw_image(ctx, self.prop('path'), self.x + inset, self.y + inset,
                        self.w - 2 * inset, self.h - 2 * inset,
                        fit=self.prop('fit', 'contain'),
                        opacity=self.num('opacity', 1.0))
        if not ok:
            T.stroke_rounded(ctx, self.x + 2, self.y + 2, self.w - 4, self.h - 4,
                             env.theme.radius, T.with_alpha(env.theme.text_dim, 0.5), 1.0)
            T.draw_text(ctx, 'no image', self.x, self.y, self.w, self.h,
                        font=env.theme.font, size=13, color=env.theme.text_dim)


# --- time ---------------------------------------------------------------

class ClockWidget(Widget):
    TYPE, LABEL, ICON = 'clock', 'Clock', 'clock'
    CATEGORY = 'Time'
    DESCRIPTION = 'Time and date, strftime formats.'
    DEFAULT_WIDTH = 118
    SCHEMA = [
        field('format', 'Format', 'text', '%H:%M', help='strftime, e.g. %H:%M:%S'),
        field('subformat', 'Second line', 'text', '', help='Optional, e.g. %a %d %b'),
        field('align', 'Align', 'choice', 'center', options=['left', 'center', 'right']),
        STYLE_FIELD,
    ]

    def tick_interval(self):
        fmt = str(self.prop('format', '')) + str(self.prop('subformat', ''))
        return 0.5 if '%S' in fmt else 5.0

    def draw_content(self, ctx, env):
        theme = env.theme
        x, y, w, h = self.inner()
        align = self.prop('align', 'center')
        now = time.localtime(env.now)
        main = time.strftime(str(self.prop('format', '%H:%M')), now)
        sub = str(self.prop('subformat', '') or '')
        color = self.content_color(env)
        if sub:
            T.draw_text(ctx, main, x, y + 4, w, h * 0.55 - 2, font=theme.font,
                        size=theme.label_size, color=color, weight='bold', align=align)
            T.draw_text(ctx, time.strftime(sub, now), x, y + h * 0.52, w, h * 0.44 - 4,
                        font=theme.font, size=13, color=theme.text_dim, align=align)
        else:
            T.draw_text(ctx, main, x, y, w, h, font=theme.font,
                        size=theme.label_size + 3, color=color,
                        weight='bold', align=align)


# --- markets ------------------------------------------------------------

def _format_price(value, decimals=None):
    if value is None:
        return '--'
    if decimals is None:
        decimals = 0 if abs(value) >= 1000 else (2 if abs(value) >= 1 else 4)
    return f'{value:,.{int(decimals)}f}'


class _MarketWidget(Widget):
    """Shared rendering for anything with a price, a change and a series."""
    DEFAULT_WIDTH = 190

    def _payload(self, env):
        raise NotImplementedError

    def draw_content(self, ctx, env):
        """Symbol and change share the top line; the price owns the bottom.

        The earlier version put price and change side by side on one line,
        which ellipsized the change away as soon as the price grew a digit.
        Stacking them means a five-figure price and a percentage both fit.
        """
        theme = env.theme
        data = self._payload(env)
        x, y, w, h = self.inner()
        symbol = data.get('symbol') or '--'
        price = data.get('price')
        change = data.get('change')
        series = data.get('series') or []
        stale = data.get('_age') is not None and data['_age'] > 900
        failed = not data.get('_ok') and price is None
        trend = theme.text_dim if change is None else (
            theme.good if change >= 0 else theme.bad)

        spark_w = 0.0
        if self.flag('sparkline', True) and series and w > 120:
            spark_w = min(62.0, w * 0.32)
            T.draw_sparkline(ctx, series, x + w - spark_w, y + 13, spark_w,
                             h - 26, trend)
            spark_w += 8

        tw = w - spark_w
        top_h, bot_h = h * 0.44, h * 0.50

        sym_w = T.draw_text(ctx, symbol, x, y + 4, tw, top_h, font=theme.font,
                            size=13, color=theme.text_dim, weight='semibold',
                            align=T.ALIGN_LEFT)
        if change is not None and not failed and tw - sym_w > 40:
            arrow = T.ICONS['trending-up'] if change >= 0 else T.ICONS['trending-down']
            T.draw_text(ctx, f'{abs(change):.2f}%', x, y + 4, tw, top_h,
                        font=theme.font, size=13, color=trend,
                        align=T.ALIGN_RIGHT)
            T.draw_text(ctx, arrow, x, y + 4,
                        tw - T.text_size(ctx, f'{abs(change):.2f}%',
                                         theme.font, 13)[0] - 3, top_h,
                        font=theme.icon_font, size=11, color=trend,
                        align=T.ALIGN_RIGHT)

        if failed:
            T.draw_text(ctx, 'offline', x, y + top_h, tw, bot_h, font=theme.font,
                        size=15, color=theme.text_dim, align=T.ALIGN_LEFT,
                        valign='top')
            return

        price_text = _format_price(price, self.prop('decimals'))
        if self.flag('show_currency', False) and data.get('currency'):
            price_text += f" {data['currency']}"
        T.draw_text(ctx, price_text, x, y + top_h - 2, tw, bot_h,
                    font=theme.font, size=theme.label_size + 1,
                    color=T.with_alpha(theme.text, 0.55 if stale else 1.0),
                    weight='bold', align=T.ALIGN_LEFT, valign='top')


class CryptoWidget(_MarketWidget):
    TYPE, LABEL, ICON = 'crypto', 'Crypto price', 'bitcoin'
    CATEGORY = 'Live data'
    DESCRIPTION = 'CoinGecko price, 24h change and sparkline.'
    SCHEMA = [
        field('coin', 'Coin id', 'text', 'bitcoin',
              help='CoinGecko id: bitcoin, ethereum, solana, dogecoin…'),
        field('symbol', 'Display name', 'text', 'BTC'),
        field('vs', 'Currency', 'choice', 'usd',
              options=['usd', 'eur', 'gbp', 'jpy', 'idr', 'sgd', 'aud', 'btc']),
        field('sparkline', 'Sparkline', 'bool', True),
        field('show_currency', 'Show currency', 'bool', False),
        field('decimals', 'Decimals', 'int', None, min=0, max=8,
              help='Blank picks automatically'),
    ]

    def feeds(self):
        return [('crypto', {'coin': str(self.prop('coin', 'bitcoin')),
                            'vs': str(self.prop('vs', 'usd'))})]

    def _payload(self, env):
        kind, params = self.feeds()[0]
        data = dict(env.feed(kind, **params))
        data['symbol'] = self.prop('symbol') or params['coin'][:4].upper()
        return data


class StockWidget(_MarketWidget):
    TYPE, LABEL, ICON = 'stock', 'Stock ticker', 'chart'
    CATEGORY = 'Live data'
    DESCRIPTION = 'Yahoo Finance quote with intraday sparkline.'
    SCHEMA = [
        field('symbol', 'Symbol', 'text', 'AAPL',
              help='AAPL, MSFT, ^GSPC, BTC-USD, IDR=X …'),
        field('label', 'Display name', 'text', ''),
        field('range', 'Range', 'choice', '1d',
              options=['1d', '5d', '1mo', '6mo', '1y']),
        field('sparkline', 'Sparkline', 'bool', True),
        field('decimals', 'Decimals', 'int', 2, min=0, max=6),
    ]

    def feeds(self):
        return [('stock', {'symbol': str(self.prop('symbol', 'AAPL')),
                           'range': str(self.prop('range', '1d'))})]

    def _payload(self, env):
        kind, params = self.feeds()[0]
        data = dict(env.feed(kind, **params))
        if self.prop('label'):
            data['symbol'] = self.prop('label')
        data.setdefault('symbol', params['symbol'])
        return data


class WeatherWidget(Widget):
    TYPE, LABEL, ICON = 'weather', 'Weather', 'cloud'
    CATEGORY = 'Live data'
    DESCRIPTION = 'Open-Meteo conditions for any city.'
    DEFAULT_WIDTH = 170
    SCHEMA = [
        field('place', 'City', 'text', '',
              help='Any city name -- resolved by Open-Meteo, no API key'),
        field('units', 'Units', 'choice', 'metric', options=['metric', 'imperial']),
        field('show_range', 'Show high/low', 'bool', True),
    ]

    def feeds(self):
        place = str(self.prop('place', '') or '')
        if not place:
            return []
        return [('weather', {'place': place,
                             'units': str(self.prop('units', 'metric'))})]

    def draw_content(self, ctx, env):
        theme = env.theme
        x, y, w, h = self.inner()
        specs = self.feeds()
        if not specs:
            T.draw_text(ctx, 'Set a city', x, y, w, h, font=theme.font,
                        size=15, color=theme.text_dim)
            return
        data = env.feed(specs[0][0], **specs[0][1])
        if not data.get('_ok') and data.get('temp') is None:
            T.draw_text(ctx, 'weather --', x, y, w, h, font=theme.font,
                        size=15, color=theme.text_dim)
            return
        gw = 34
        T.draw_text(ctx, T.resolve_icon(data.get('glyph', 'cloud')), x, y, gw, h,
                    font=theme.icon_font, size=theme.icon_size, color=theme.accent)
        unit = data.get('unit', '°C')
        temp = data.get('temp')
        rest_x, rest_w = x + gw + 4, w - gw - 4
        if self.flag('show_range', True) and data.get('high') is not None:
            T.draw_text(ctx, f'{temp:.0f}{unit}' if temp is not None else '--',
                        rest_x, y + 4, rest_w, h * 0.55 - 2, font=theme.font,
                        size=theme.label_size, color=theme.text, weight='bold',
                        align=T.ALIGN_LEFT)
            T.draw_text(ctx, f"{data['low']:.0f}° / {data['high']:.0f}°",
                        rest_x, y + h * 0.52, rest_w, h * 0.44 - 4,
                        font=theme.font, size=13, color=theme.text_dim,
                        align=T.ALIGN_LEFT)
        else:
            T.draw_text(ctx, f'{temp:.0f}{unit}' if temp is not None else '--',
                        rest_x, y, rest_w, h, font=theme.font,
                        size=theme.label_size + 2, color=theme.text,
                        weight='bold', align=T.ALIGN_LEFT)


# --- system meters ------------------------------------------------------

class _MeterWidget(Widget):
    """A labelled 0..1 meter, drawn as a graph, bar or plain number."""
    DEFAULT_WIDTH = 150
    METER_LABEL = ''
    SCHEMA = [
        field('style', 'Style', 'choice', 'graph', options=['graph', 'bar', 'text']),
        field('label', 'Label', 'text', ''),
        field('warn_at', 'Warn above %', 'int', 80, min=0, max=100),
    ]

    def _reading(self, env):
        """(fraction, text, history or None)."""
        raise NotImplementedError

    def draw_content(self, ctx, env):
        theme = env.theme
        fraction, text, history = self._reading(env)
        x, y, w, h = self.inner()
        label = str(self.prop('label') or self.METER_LABEL)
        warn = self.num('warn_at', 80) / 100.0
        color = theme.bad if fraction >= warn else (
            theme.warn if fraction >= warn * 0.75 else theme.accent)
        style = self.prop('style', 'graph')

        label_w = 0.0
        if label:
            label_w = T.text_size(ctx, label, theme.font, 13, 'semibold')[0] + 6
            T.draw_text(ctx, label, x, y, label_w, h, font=theme.font, size=13,
                        color=theme.text_dim, weight='semibold', align=T.ALIGN_LEFT)

        value_w = T.text_size(ctx, text, theme.font, theme.value_size, 'bold')[0] + 8
        gx = x + label_w
        gw = max(0.0, w - label_w - value_w)

        if style == 'graph' and history:
            T.draw_bars(ctx, history, gx, y + 12, gw, h - 24,
                        T.with_alpha(color, 0.55), hot=color, hot_at=warn)
        elif style == 'bar':
            T.draw_track(ctx, gx, y + h / 2 - 5, gw, 10, fraction,
                         track_rgba=T.with_alpha(theme.text_dim, 0.25),
                         fill_rgba=color)
        T.draw_text(ctx, text, x + w - value_w, y, value_w, h, font=theme.font,
                    size=theme.value_size, color=theme.text, weight='bold',
                    align=T.ALIGN_RIGHT)


class CpuWidget(_MeterWidget):
    TYPE, LABEL, ICON = 'cpu', 'CPU', 'cpu'
    CATEGORY = 'System'
    DESCRIPTION = 'Processor load with rolling history.'
    METER_LABEL = 'CPU'
    DEFAULT_WIDTH = 200

    def feeds(self):
        return [('cpu', {})]

    def _reading(self, env):
        data = env.feed('cpu')
        load = data.get('load', 0.0)
        return load, f'{load * 100:.0f}%', data.get('history')


class MemoryWidget(_MeterWidget):
    TYPE, LABEL, ICON = 'memory', 'Memory', 'memory'
    CATEGORY = 'System'
    DESCRIPTION = 'RAM in use.'
    METER_LABEL = 'RAM'
    SCHEMA = _MeterWidget.SCHEMA + [
        field('show', 'Show', 'choice', 'percent', options=['percent', 'gigabytes']),
    ]

    def feeds(self):
        return [('memory', {})]

    def _reading(self, env):
        data = env.feed('memory')
        frac = data.get('fraction', 0.0)
        if self.prop('show', 'percent') == 'gigabytes':
            text = f"{data.get('used', 0) / 2**30:.1f}G"
        else:
            text = f'{frac * 100:.0f}%'
        return frac, text, None

    def draw_content(self, ctx, env):
        if self.prop('style') == 'graph':
            self.props = dict(self.props, style='bar')     # no history for RAM
        super().draw_content(ctx, env)


class TemperatureWidget(_MeterWidget):
    TYPE, LABEL, ICON = 'temperature', 'Temperature', 'thermometer'
    CATEGORY = 'System'
    DESCRIPTION = 'Hottest thermal zone, or one you name.'
    METER_LABEL = 'TEMP'
    DEFAULT_WIDTH = 140
    SCHEMA = _MeterWidget.SCHEMA + [
        field('sensor', 'Sensor filter', 'text', '',
              help='Substring of the thermal zone type, e.g. x86_pkg'),
        field('max_celsius', 'Scale max °C', 'int', 100, min=40, max=120),
    ]

    def feeds(self):
        return [('temperature', {'sensor': str(self.prop('sensor', '') or '')})]

    def _reading(self, env):
        data = env.feed(*self.feeds()[0][:1], **self.feeds()[0][1])
        celsius = data.get('celsius')
        if celsius is None:
            return 0.0, '--', None
        return celsius / self.num('max_celsius', 100), f'{celsius:.0f}°', None


class DiskWidget(_MeterWidget):
    TYPE, LABEL, ICON = 'disk', 'Disk', 'disk'
    CATEGORY = 'System'
    DESCRIPTION = 'Filesystem usage.'
    METER_LABEL = 'DISK'
    SCHEMA = _MeterWidget.SCHEMA + [field('path', 'Mount point', 'text', '/')]

    def feeds(self):
        return [('disk', {'path': str(self.prop('path', '/'))})]

    def _reading(self, env):
        data = env.feed('disk', path=str(self.prop('path', '/')))
        frac = data.get('fraction', 0.0)
        return frac, f"{data.get('free', 0) / 2**30:.0f}G free", None


class NetworkWidget(Widget):
    TYPE, LABEL, ICON = 'network', 'Network', 'network'
    CATEGORY = 'System'
    DESCRIPTION = 'Live up/down throughput.'
    DEFAULT_WIDTH = 155
    SCHEMA = [field('unit', 'Unit', 'choice', 'auto', options=['auto', 'Mbit', 'MB'])]

    def feeds(self):
        return [('network', {})]

    @staticmethod
    def _fmt(rate, unit):
        if unit == 'Mbit':
            return f'{rate * 8 / 1e6:.1f}'
        if unit == 'MB':
            return f'{rate / 1e6:.2f}'
        for div, suffix in ((1e6, 'M'), (1e3, 'K')):
            if rate >= div:
                return f'{rate / div:.1f}{suffix}'
        return f'{rate:.0f}B'

    def draw_content(self, ctx, env):
        theme = env.theme
        data = env.feed('network')
        x, y, w, h = self.inner()
        unit = self.prop('unit', 'auto')
        for i, (glyph, key, color) in enumerate((
                (T.ICONS['arrow-down'], 'rx_rate', theme.good),
                (T.ICONS['arrow-up'], 'tx_rate', theme.accent))):
            row_y = y + 3 + i * (h - 6) / 2
            row_h = (h - 6) / 2
            T.draw_text(ctx, glyph, x, row_y, 18, row_h, font=theme.icon_font,
                        size=13, color=color)
            T.draw_text(ctx, self._fmt(data.get(key, 0.0), unit), x + 18,
                        row_y, w - 18, row_h, font=theme.font, size=14,
                        color=theme.text, weight='semibold', align=T.ALIGN_RIGHT)


class BatteryWidget(Widget):
    TYPE, LABEL, ICON = 'battery', 'Battery', 'battery'
    CATEGORY = 'System'
    DESCRIPTION = 'Charge level, state and time remaining.'
    DEFAULT_WIDTH = 110
    SCHEMA = [
        field('name', 'Device', 'text', 'BAT0'),
        field('show_time', 'Show time left', 'bool', False),
        field('low_at', 'Low below %', 'int', 20, min=1, max=99),
    ]

    def feeds(self):
        return [('battery', {'name': str(self.prop('name', 'BAT0'))})]

    def draw_content(self, ctx, env):
        theme = env.theme
        data = env.feed('battery', name=str(self.prop('name', 'BAT0')))
        x, y, w, h = self.inner()
        percent = data.get('percent')
        if percent is None:
            T.draw_text(ctx, 'no batt', x, y, w, h, font=theme.font, size=14,
                        color=theme.text_dim)
            return
        charging = data.get('charging')
        low = percent <= self.num('low_at', 20)
        color = theme.good if charging else (theme.bad if low else theme.text)

        # A drawn battery reads faster than a glyph at this size.
        bw, bh = 26.0, 13.0
        bx, by = x, y + (h - bh) / 2
        T.stroke_rounded(ctx, bx, by, bw, bh, 3, T.with_alpha(theme.text_dim, 0.8), 1.4)
        ctx.rectangle(bx + bw + 1.5, by + bh / 2 - 3, 2.5, 6)
        T.set_source(ctx, T.with_alpha(theme.text_dim, 0.8))
        ctx.fill()
        inner_w = (bw - 4) * max(0.0, min(1.0, percent / 100))
        if inner_w > 0:
            T.fill_rounded(ctx, bx + 2, by + 2, inner_w, bh - 4, 1.5, color)
        if charging:
            T.draw_text(ctx, T.ICONS['thunder'], bx, by - 1, bw, bh,
                        font=theme.icon_font,
                        size=11, color=theme.background)

        text = f'{percent}%'
        if self.flag('show_time') and data.get('hours'):
            text += f"  {data['hours']:.1f}h"
        T.draw_text(ctx, text, bx + bw + 8, y, w - bw - 8, h, font=theme.font,
                    size=theme.value_size, color=color, weight='bold',
                    align=T.ALIGN_LEFT)


# --- interactive --------------------------------------------------------

class _SliderWidget(Widget):
    """Drag anywhere on the widget to set a level."""
    INTERACTIVE = True
    DEFAULT_WIDTH = 220
    GLYPH_LOW, GLYPH_HIGH = 'volume-down', 'volume-up'
    SCHEMA = [
        field('show_value', 'Show percentage', 'bool', True),
        field('icon', 'Icon', 'icon', ''),
    ]

    def _level(self, env):
        raise NotImplementedError

    def _set_action(self, fraction):
        raise NotImplementedError

    def _fraction_at(self, point):
        x0 = self.x + 12 + 26
        span = max(1.0, self.w - 24 - 26 - (44 if self.flag('show_value', True) else 0))
        return max(0.0, min(1.0, (point[0] - x0) / span))

    def draw_content(self, ctx, env):
        theme = env.theme
        level, muted = self._level(env)
        x, y, w, h = self.x + 12, self.y, self.w - 24, self.h
        glyph = T.resolve_icon(self.prop('icon') or
                              (self.GLYPH_LOW if (muted or level < 0.5)
                               else self.GLYPH_HIGH))
        T.draw_text(ctx, glyph, x, y, 24, h, font=theme.icon_font,
                    size=theme.icon_size - 3,
                    color=theme.text_dim if muted else theme.text)
        value_w = 44 if self.flag('show_value', True) else 0
        tx = x + 26
        tw = max(10.0, w - 26 - value_w)
        T.draw_track(ctx, tx, y + h / 2 - 7, tw, 14, 0.0 if muted else level,
                     track_rgba=T.with_alpha(theme.text_dim, 0.22),
                     fill_rgba=theme.text_dim if muted else theme.accent)
        if not muted and level > 0.02:
            knob_x = tx + tw * level
            T.fill_rounded(ctx, knob_x - 3, y + h / 2 - 11, 6, 22, 3, theme.text)
        if value_w:
            T.draw_text(ctx, 'mute' if muted else f'{level * 100:.0f}%',
                        x + w - value_w, y, value_w, h, font=theme.font,
                        size=14, color=theme.text_dim, weight='semibold',
                        align=T.ALIGN_RIGHT)

    def tap(self, env, point):
        return self._set_action(self._fraction_at(point))

    def drag(self, env, point):
        return self._set_action(self._fraction_at(point))


class VolumeWidget(_SliderWidget):
    TYPE, LABEL, ICON = 'volume', 'Volume slider', 'volume-up'
    CATEGORY = 'Controls'
    DESCRIPTION = 'Drag to set output volume. Tap the icon area to mute.'
    GLYPH_LOW, GLYPH_HIGH = 'mute', 'volume-up'

    def feeds(self):
        return [('volume', {})]

    def _level(self, env):
        data = env.feed('volume')
        return data.get('level', 0.0), bool(data.get('muted'))

    def _set_action(self, fraction):
        return {'type': 'volume', 'set': round(fraction, 3)}

    def tap(self, env, point):
        if point[0] < self.x + 34:                    # the speaker glyph
            return {'type': 'volume', 'toggle_mute': True}
        return super().tap(env, point)


class BrightnessWidget(_SliderWidget):
    TYPE, LABEL, ICON = 'brightness', 'Brightness slider', 'sun'
    CATEGORY = 'Controls'
    DESCRIPTION = 'Drag to set display backlight.'
    GLYPH_LOW, GLYPH_HIGH = 'brightness-down', 'brightness-up'
    SCHEMA = _SliderWidget.SCHEMA + [
        field('min_percent', 'Minimum %', 'int', 3, min=0, max=50,
              help='Stops a drag to zero from blanking the panel'),
    ]

    def feeds(self):
        return [('brightness', {})]

    def _level(self, env):
        return env.feed('brightness').get('level', 0.0), False

    def _set_action(self, fraction):
        floor = self.num('min_percent', 3) / 100.0
        return {'type': 'brightness', 'set': round(max(floor, fraction), 3)}


class MediaWidget(Widget):
    TYPE, LABEL, ICON = 'media', 'Now playing', 'music'
    CATEGORY = 'Controls'
    DESCRIPTION = 'MPRIS track, with progress. Tap to play/pause.'
    DEFAULT_WIDTH = 320
    SCHEMA = [
        field('player', 'Prefer player', 'text', '', help='Substring, blank = any'),
        field('show_progress', 'Show progress', 'bool', True),
        field('idle_text', 'When idle', 'text', 'Nothing playing'),
        field('tap', 'On tap', 'choice', 'playpause',
              options=['playpause', 'next', 'nothing']),
    ]

    def feeds(self):
        return [('mpris', {'player': str(self.prop('player', '') or '')})]

    def draw_content(self, ctx, env):
        theme = env.theme
        data = env.feed('mpris', player=str(self.prop('player', '') or ''))
        x, y, w, h = self.inner()
        title = data.get('title') or ''
        if not title:
            T.draw_text(ctx, str(self.prop('idle_text', 'Nothing playing')),
                        x, y, w, h, font=theme.font, size=15,
                        color=theme.text_dim, align=T.ALIGN_LEFT)
            return

        glyph = T.ICONS['play' if data.get('playing') else 'pause']
        T.draw_text(ctx, glyph, x, y, 22, h, font=theme.icon_font, size=15,
                    color=theme.accent)
        tx, tw = x + 24, w - 24
        artist = data.get('artist') or data.get('album') or ''
        show_progress = self.flag('show_progress', True) and data.get('length')

        if artist:
            T.draw_text(ctx, title, tx, y + 3, tw, h * 0.5 - 2, font=theme.font,
                        size=16, color=theme.text, weight='semibold',
                        align=T.ALIGN_LEFT)
            T.draw_text(ctx, artist, tx, y + h * 0.48, tw,
                        h * 0.5 - (8 if show_progress else 2), font=theme.font,
                        size=13, color=theme.text_dim, align=T.ALIGN_LEFT)
        else:
            T.draw_text(ctx, title, tx, y, tw, h - (8 if show_progress else 0),
                        font=theme.font, size=17, color=theme.text,
                        weight='semibold', align=T.ALIGN_LEFT)

        if show_progress:
            frac = min(1.0, (data.get('position') or 0) / max(1.0, data['length']))
            T.draw_track(ctx, tx, y + h - 8, tw, 3, frac,
                         track_rgba=T.with_alpha(theme.text_dim, 0.25),
                         fill_rgba=theme.accent, radius=1.5)

    def tap(self, env, point):
        if self.action:
            return self.action
        choice = self.prop('tap', 'playpause')
        if choice == 'nothing':
            return None
        return {'type': 'key', 'keys': ['PLAYPAUSE' if choice == 'playpause'
                                        else 'NEXTSONG']}


class WorkspacesWidget(Widget):
    TYPE, LABEL, ICON = 'workspaces', 'Workspaces', 'workspace'
    CATEGORY = 'Controls'
    DESCRIPTION = 'Hyprland workspaces. Tap one to switch to it.'
    DEFAULT_WIDTH = 260
    HAS_SURFACE = False
    SCHEMA = [
        field('pill_width', 'Pill width', 'int', 42, min=24, max=90),
        field('show_empty', 'Show empty', 'bool', True),
    ]

    def feeds(self):
        return [('workspaces', {})]

    def _pills(self, env):
        data = env.feed('workspaces')
        spaces = data.get('spaces') or []
        if not self.flag('show_empty', True):
            spaces = [s for s in spaces
                      if s.get('windows') or s.get('id') == data.get('active')]
        pw = self.num('pill_width', 42)
        gap = 6.0
        total = len(spaces) * pw + max(0, len(spaces) - 1) * gap
        start = self.x + (self.w - total) / 2
        return [(start + i * (pw + gap), pw, s) for i, s in enumerate(spaces)], \
            data.get('active')

    def draw_content(self, ctx, env):
        theme = env.theme
        pills, active = self._pills(env)
        for px, pw, space in pills:
            is_active = space['id'] == active
            color = theme.accent if is_active else (
                theme.surface if space.get('windows') else
                T.with_alpha(theme.surface, 0.45))
            T.fill_rounded(ctx, px, self.y + 10, pw, self.h - 20,
                           theme.radius - 2, color)
            T.draw_text(ctx, space['name'], px, self.y + 10, pw, self.h - 20,
                        font=theme.font, size=15,
                        color=T.readable_on(color) if is_active else theme.text,
                        weight='bold')

    def tap(self, env, point):
        pills, _ = self._pills(env)
        for px, pw, space in pills:
            if px <= point[0] < px + pw:
                return {'type': 'hypr',
                        'dispatch': f"hl.dsp.focus({{ workspace = \"{space['id']}\" }})"}
        return None


class ScriptWidget(Widget):
    TYPE, LABEL, ICON = 'script', 'Script output', 'terminal'
    CATEGORY = 'Live data'
    DESCRIPTION = 'Runs a command on a timer and shows its first line.'
    DEFAULT_WIDTH = 180
    SCHEMA = [
        field('command', 'Command', 'multiline', 'date +%s'),
        field('interval', 'Every (seconds)', 'int', 10, min=1, max=3600),
        field('icon', 'Icon', 'icon', ''),
        field('label', 'Label', 'text', ''),
        field('size', 'Text size', 'int', 17, min=8, max=40),
        STYLE_FIELD,
    ]

    def feeds(self):
        return [('script', {'command': str(self.prop('command', 'true')),
                            'interval': int(self.num('interval', 10))})]

    def draw_content(self, ctx, env):
        theme = env.theme
        kind, params = self.feeds()[0]
        data = env.feed(kind, **params)
        x, y, w, h = self.inner()
        icon = T.resolve_icon(self.prop('icon', ''))
        label = str(self.prop('label', '') or '')
        text = data.get('text') or ('…' if data.get('_age') is None else '')
        color = self.content_color(env)
        if data.get('rc') not in (0, None):
            color = theme.bad

        if icon:
            T.draw_text(ctx, icon, x, y, 22, h, font=theme.icon_font,
                        size=theme.icon_size - 4, color=color)
            x, w = x + 24, w - 24
        if label:
            T.draw_text(ctx, label, x, y + 4, w, h * 0.44, font=theme.font,
                        size=12, color=theme.text_dim, weight='semibold',
                        align=T.ALIGN_LEFT)
            T.draw_text(ctx, text, x, y + h * 0.44, w, h * 0.5, font=theme.font,
                        size=self.num('size', 17), color=color, weight='bold',
                        align=T.ALIGN_LEFT)
        else:
            T.draw_text(ctx, text, x, y, w, h, font=theme.font,
                        size=self.num('size', 17), color=color,
                        weight='semibold', align=T.ALIGN_LEFT)


class KittWidget(Widget):
    """Spectrum of the machine's own output, as a red LED matrix.

    Named for what it looks like rather than what it measures: the Knight
    Rider scanner is a row of red cells, and that is the whole visual idea --
    discrete lit pixels with dark ones still visible between them, not a smooth
    bar. Drawing the unlit cells is the point; a gradient with no grid reads as
    a progress bar, and the grid is what makes it read as hardware.

    Mirrored about the centre by default, with the bass at the two outer ends
    and the treble meeting in the middle. On a strip 2170px wide and 60 tall
    that puts the loudest, slowest-moving part of the signal at the far edges,
    where it reads as the strip breathing, and leaves the fine detail in the
    middle where there is room for it.
    """
    TYPE, LABEL, ICON = 'kitt', 'KITT visualiser', 'music'
    CATEGORY = 'Live data'
    DESCRIPTION = 'Audio spectrum of this machine\'s output, as a red LED matrix.'
    DEFAULT_WIDTH = 640
    HAS_SURFACE = False

    SCHEMA = [
        field('bands', 'Bands', 'int', 48, min=4, max=64,
              help='Mirrored, so the strip shows twice this many columns'),
        field('rows', 'Rows', 'int', 9, min=3, max=11),
        field('mirror', 'Mirror from centre', 'bool', True),
        field('grow', 'Grow', 'choice', 'center', options=['center', 'up'],
              help='Cells light outward from the middle row, or up from the floor'),
        field('color', 'Colour', 'color', '#ff1e0a'),
        field('peaks', 'Peak hold', 'bool', True),
        field('when_silent', 'When silent', 'choice', 'sweep',
              options=['sweep', 'grid', 'dark'],
              help='Knight Rider scan, unlit matrix, or nothing at all'),
        field('auto_gain', 'Auto gain', 'bool', True,
              help='Ride the level so quiet system volume still fills the strip'),
        field('gain', 'Gain dB', 'int', 0, min=-12, max=24,
              help='Trim on top of auto gain'),
        field('fps', 'Frames per second', 'int', 30, min=10, max=60,
              help='Every frame is a USB transfer; 30 is smooth and cheap'),
    ]

    def __init__(self, spec):
        super().__init__(spec)
        self._previous = None
        self._damage = None
        self._dark = False

    def silent_style(self):
        """What to show when nothing is playing.

        `idle_scan` was a bool before there were three answers; a config
        written against it still means what it said.
        """
        if self.props.get('when_silent'):
            return str(self.props['when_silent'])
        if 'idle_scan' in self.props and not self.flag('idle_scan', True):
            return 'grid'
        return str(self.prop('when_silent', 'sweep'))

    def feeds(self):
        return [('audio', {'bands': int(self.num('bands', 48)),
                           'gain': int(self.num('gain', 0)),
                           'auto_gain': bool(self.flag('auto_gain', True))})]

    def tick_interval(self):
        # Animation, not data: the sweep has to move while the feed is silent
        # and unchanging, so this widget asks to be repainted on a clock.
        #
        # Except when it is dark, where there is nothing to animate and asking
        # for 30 repaints a second would clear a strip-wide rectangle into the
        # framebuffer thirty times a second to no effect. Silence makes the
        # feed's snapshot stop changing too, so with no clock either the widget
        # goes properly idle -- no CPU, no USB -- until a sound restarts it.
        if self._dark:
            return None
        return 1.0 / max(10.0, min(60.0, self.num('fps', 30)))

    # -- colour ---------------------------------------------------------

    def _ramp(self, base, heat, lit, peak=False):
        """Cell colour. `heat` is 0..1 for how hard the diode is driven, `lit`
        its brightness; `peak` is the hold marker sitting above the bar.

        One hue throughout. An LED bar is a single colour of diode driven
        harder, so heat lifts it only slightly off red -- enough that a loud
        band looks hotter than a quiet one, nowhere near enough to reach
        orange. The first version of this ramp added 0.62 to green at the top
        of the column and the whole strip came out looking like fire, which is
        a different object with a different meaning.
        """
        if lit <= 0.001:
            # The unlit diode. Visible, but only just: this is what makes the
            # matrix read as a grid of pixels rather than as empty background.
            return (base[0] * 0.22, base[1] * 0.22, base[2] * 0.22, 0.5)
        if peak:
            # The only thing allowed to look hotter than the bar it caps.
            return (min(1.0, base[0] + 0.10), min(1.0, base[1] + 0.42),
                    min(1.0, base[2] + 0.34), 1.0)
        warm = min(1.0, max(0.0, heat))
        r = min(1.0, base[0] + warm * 0.05)
        g = min(1.0, base[1] + warm * 0.24)
        b = min(1.0, base[2] + warm * 0.10)
        scale = 0.40 + 0.60 * lit
        return (r * scale, g * scale, b * scale, 1.0)

    # -- geometry -------------------------------------------------------

    def _columns(self, values):
        if not self.flag('mirror', True):
            return list(values)
        # Forward on the left, reversed on the right: band 0 lands at both
        # outer ends, so the bass drives the two edges and the treble detail
        # meets in the middle.
        return list(values) + list(reversed(values))

    def draw_content(self, ctx, env):
        data = env.feed(*self.feeds()[0][:1], **self.feeds()[0][1])
        x, y, w, h = self.inner()
        base = T.parse_color(self.prop('color', '#ff1e0a'), (1.0, 0.12, 0.04, 1.0))
        rows = max(3, min(11, int(self.num('rows', 9))))

        bands = data.get('bands') or []
        peaks = data.get('peaks') or []
        if not bands:
            # No capture yet, or PipeWire refused. Draw the dark matrix anyway:
            # an empty rectangle looks like a crashed widget, while an unlit
            # grid looks like a display waiting for signal, which it is.
            bands = [0.0] * int(self.num('bands', 48))
            peaks = []

        silent = bool(data.get('silent')) or not data.get('_ok', True)
        columns = self._columns(bands)
        peak_columns = self._columns(peaks) if peaks and self.flag('peaks', True) else []

        count = len(columns)
        if count < 1 or w <= 0:
            return
        cell_w = w / count
        gap_x = min(8.0, cell_w * 0.28)
        cell_h = h / rows
        gap_y = min(3.0, cell_h * 0.22)

        previous = self._previous
        if previous is not None and len(previous) != count:
            previous = None              # geometry changed; nothing comparable
        columns_now = []
        first = last = None

        centre_grow = self.prop('grow', 'center') == 'center'
        # In centre-growth a column has (rows+1)//2 distinct steps, because the
        # rows pair up either side of the middle one.
        levels = (rows + 1) // 2 if centre_grow else rows
        middle = (rows - 1) / 2.0

        style = self.silent_style() if silent else 'sweep'
        dark = silent and style == 'dark'
        scan = None
        if silent and style == 'sweep':
            scan = self._scan_position(env, count)

        # Nothing is playing and this page is set to go dark. Widget.draw has
        # already cleared to the background, so the strip is dark by having
        # drawn nothing -- and the signature below still runs, so the first
        # dark frame is pushed once and the ones after it are not pushed at all.
        self._dark = dark

        for ci in range(count):
            value = columns[ci] if scan is None else 0.0
            peak = peak_columns[ci] if (peak_columns and scan is None) else None
            cx = x + ci * cell_w
            signature = []
            for ri in range(rows) if not dark else ():
                distance = abs(ri - middle)
                # 0 at the middle row, or 0 at the floor.
                step = int(distance + 0.5) if centre_grow else rows - 1 - ri
                level = step / max(1, levels - 1) if levels > 1 else 0.0

                is_peak = False
                if scan is not None:
                    lit = self._scan_brightness(
                        scan, ci, count, distance if centre_grow else step, levels)
                    # The sweep has no column height to take heat from, so the
                    # head itself is what runs hot.
                    heat = lit
                else:
                    reach = value * levels
                    if step + 1 <= reach:
                        lit = 1.0
                    elif step <= reach:
                        lit = reach - step               # the moving top cell
                    else:
                        lit = 0.0
                    # Heat comes from both how high the cell sits and how loud
                    # the band is, so a tall bar glows harder than a short one
                    # reaching the same row.
                    heat = level * (0.45 + 0.55 * value)
                    if peak is not None and lit < 1.0 and peak > 0.02:
                        if abs(step - int(peak * levels)) < 0.5:
                            lit, is_peak = 1.0, True

                # Quantised, and then *drawn* from the quantised values. The
                # signature below is what decides whether this column gets
                # pushed to the panel, so it has to describe the pixels
                # exactly: rounding only the comparison would leave changes
                # under half a step on screen but unflushed, which is a stale
                # display that no amount of staring at the draw code explains.
                lit_q = int(lit * CELL_STEPS + 0.5)
                heat_q = int(max(0.0, min(1.0, heat)) * CELL_STEPS + 0.5)
                signature.append((lit_q, heat_q, is_peak))

                colour = self._ramp(base, heat_q / CELL_STEPS,
                                    lit_q / CELL_STEPS, peak=is_peak)
                T.set_source(ctx, colour)
                ctx.rectangle(cx + gap_x / 2, y + ri * cell_h + gap_y / 2,
                              max(1.0, cell_w - gap_x), max(1.0, cell_h - gap_y))
                ctx.fill()

            column = tuple(signature)
            if previous is not None and ci < len(previous) and previous[ci] == column:
                pass
            else:
                first = ci if first is None else first
                last = ci
            columns_now.append(column)

        self._previous = columns_now
        if first is None:
            # Nothing moved. Common while paused, and the whole strip is 368 KB
            # to push, so it is worth not pushing it.
            self._damage = (self.x, self.y, 0.0, 0.0)
        else:
            left = x + first * cell_w
            right = x + (last + 1) * cell_w
            self._damage = (max(self.x, left - 1.0), self.y,
                            min(self.x + self.w, right + 1.0) - max(self.x, left - 1.0),
                            self.h)

    def damage(self):
        return self._damage

    def draw(self, ctx, env):
        # Skip the clear once the strip is already dark and still is. The
        # clear is a strip-wide fill straight into the framebuffer, which is
        # write-combined memory and costs far more than it looks; repeating it
        # to paint black over black is the one case worth catching.
        if self._dark and self._previous is not None:
            data = env.feed(*self.feeds()[0][:1], **self.feeds()[0][1])
            if data.get('silent') and self.silent_style() == 'dark':
                self._damage = (self.x, self.y, 0.0, 0.0)
                return
        super().draw(ctx, env)

    # -- the idle sweep -------------------------------------------------

    def _scan_position(self, env, count):
        """Where the Knight Rider head is, 0..count-1, sweeping and easing."""
        period = 2.4
        phase = (env.now % period) / period
        # Triangle, then a cosine ease so the head slows at the turns rather
        # than reversing at full speed, which is what the prop actually did.
        tri = 1.0 - abs(2.0 * phase - 1.0)
        eased = 0.5 - 0.5 * math.cos(tri * math.pi)
        return eased * (count - 1)

    @staticmethod
    def _scan_brightness(head, ci, count, distance, levels):
        """Gaussian falloff from the head, narrowed toward the outer rows."""
        spread = max(2.0, count * 0.055)
        horizontal = math.exp(-((ci - head) ** 2) / (2.0 * spread * spread))
        vertical = 1.0 - (distance / max(1, levels)) * 0.55
        value = horizontal * vertical
        return value if value > 0.02 else 0.0


class PagerWidget(Widget):
    TYPE, LABEL, ICON = 'pager', 'Page switcher', 'pages'
    CATEGORY = 'Navigation'
    DESCRIPTION = 'Cycles pages, or jumps to a named one.'
    DEFAULT_WIDTH = 74
    SCHEMA = [
        field('mode', 'On tap', 'choice', 'next',
              options=['next', 'previous', 'named']),
        field('page', 'Page', 'page', '', help='Used when mode is "named"'),
        field('icon', 'Icon', 'icon', 'pages'),
        field('show_dots', 'Show page dots', 'bool', True),
        STYLE_FIELD,
    ]

    def tick_interval(self):
        return None

    def draw_content(self, ctx, env):
        theme = env.theme
        color = self.content_color(env)
        x, y, w, h = self.inner()
        pages = env.config.get('pages', [])
        dots = self.flag('show_dots', True) and len(pages) > 1
        icon_h = h * (0.66 if dots else 1.0)
        T.draw_text(ctx, T.resolve_icon(self.prop('icon', 'pages')), x, y, w, icon_h,
                    font=theme.icon_font, size=theme.icon_size, color=color)
        if dots:
            r, gap = 2.5, 9.0
            total = len(pages) * gap - (gap - 2 * r)
            cx = x + (w - total) / 2 + r
            cy = y + h - 11
            for page in pages:
                on = page.get('id') == env.page_id
                T.set_source(ctx, theme.accent if on
                             else T.with_alpha(theme.text_dim, 0.5))
                ctx.arc(cx, cy, r if not on else r + 0.8, 0, 2 * math.pi)
                ctx.fill()
                cx += gap

    def tap(self, env, point):
        mode = self.prop('mode', 'next')
        if mode == 'named' and self.prop('page'):
            return {'type': 'page', 'page': str(self.prop('page'))}
        return {'type': 'page', 'page': '+1' if mode == 'next' else '-1'}


class EscWidget(Widget):
    """The reserved Escape key. Not user-placeable -- the daemon injects it."""
    TYPE, LABEL, ICON = 'esc', 'Escape', 'close'
    CATEGORY = 'Navigation'
    DESCRIPTION = 'Always present unless disabled in Settings.'
    DEFAULT_WIDTH = 92
    SCHEMA = [field('label', 'Label', 'text', 'esc')]

    def draw_content(self, ctx, env):
        theme = env.theme
        x, y, w, h = self.inner()
        T.draw_text(ctx, str(self.prop('label', 'esc')), x, y, w, h,
                    font=theme.font, size=theme.label_size, color=theme.text,
                    weight='semibold')

    def tap(self, env, point):
        return {'type': 'key', 'keys': ['ESC']}


WIDGET_TYPES = {}
for cls in (ButtonWidget, LabelWidget, ImageWidget, SpacerWidget, ClockWidget,
            CryptoWidget, StockWidget, WeatherWidget, ScriptWidget,
            CpuWidget, MemoryWidget, TemperatureWidget, DiskWidget,
            NetworkWidget, BatteryWidget, VolumeWidget, BrightnessWidget,
            MediaWidget, WorkspacesWidget, KittWidget, PagerWidget, EscWidget):
    WIDGET_TYPES[cls.TYPE] = cls

#: Order used by the editor's "add widget" palette.
PALETTE_ORDER = ['Basics', 'Live data', 'System', 'Controls', 'Navigation']


def build(spec):
    cls = WIDGET_TYPES.get(spec.get('type'))
    if cls is None:
        cls = LabelWidget
        spec = dict(spec, props={'text': f"? {spec.get('type')}"})
    return cls(spec)


def defaults_for(type_):
    """A fresh widget spec for the editor's palette."""
    cls = WIDGET_TYPES.get(type_)
    props = {}
    if cls:
        for f in cls.SCHEMA:
            if f.get('default') not in (None, ''):
                props[f['key']] = f['default']
    spec = {'type': type_, 'props': props}
    if cls and cls.DEFAULT_WIDTH:
        spec['width'] = cls.DEFAULT_WIDTH
    if type_ == 'button':
        spec['action'] = {'type': 'none'}
    return spec


# --- layout -------------------------------------------------------------

def layout_page(ctx, config, page, env, *, strip_w=STRIP_W, strip_h=STRIP_H):
    """Position every widget on a page. Returns the list of laid-out widgets.

    Fixed widths are honoured first; whatever is left over is shared between
    flex widgets across all three lanes. Left packs from the left edge, right
    from the right edge, and centre is centred in the space that remains --
    which is what makes a strip look deliberate rather than merely filled.
    """
    theme = env.theme
    pad = theme.padding
    gap = theme.gap
    settings = config.get('settings', {})

    lanes = {}
    for lane in ('left', 'center', 'right'):
        lanes[lane] = [build(spec) for spec in page.get(lane, [])
                       if not spec.get('hidden')]

    reserved = 0.0
    esc_widget = None
    if settings.get('always_esc', True):
        esc_widget = EscWidget({'id': '__esc__', 'type': 'esc', 'props': {}})
        esc_widget.w = float(settings.get('esc_width', 92))
        esc_widget.h = strip_h - 2 * pad
        esc_widget.x, esc_widget.y = pad, pad
        reserved = esc_widget.w + gap * 2

    avail = strip_w - 2 * pad - reserved
    all_widgets = [w for lane in lanes.values() for w in lane]

    fixed_total = 0.0
    flex_total = 0.0
    for w in all_widgets:
        if w.flex:
            flex_total += float(w.flex)
            w.w = 0.0
        else:
            w.w = max(8.0, w.natural_width(ctx, env))
            fixed_total += w.w
    for lane_items in lanes.values():
        if lane_items:
            fixed_total += gap * (len(lane_items) - 1)

    leftover = max(0.0, avail - fixed_total)
    if flex_total > 0:
        for w in all_widgets:
            if w.flex:
                w.w = max(24.0, leftover * float(w.flex) / flex_total)

    # Overflow: shrink everything proportionally rather than letting the tail
    # of the strip fall off the right edge, where it would simply be invisible
    # with nothing to explain why.
    total = sum(w.w for w in all_widgets) + sum(
        theme.gap * max(0, len(items) - 1) for items in lanes.values())
    if total > avail and total > 0:
        gaps = sum(theme.gap * max(0, len(items) - 1) for items in lanes.values())
        room = max(1.0, avail - gaps)
        widths = max(1.0, total - gaps)
        scale = room / widths
        for w in all_widgets:
            w.w = max(MIN_WIDGET_W, w.w * scale)

    height = strip_h - 2 * pad
    for w in all_widgets:
        w.y, w.h = pad, height

    def place(items, start_x):
        x = start_x
        for w in items:
            w.x = x
            x += w.w + gap
        return x - gap if items else start_x

    left_start = pad + reserved
    left_end = place(lanes['left'], left_start)

    right_width = sum(w.w for w in lanes['right']) + \
        gap * max(0, len(lanes['right']) - 1)
    right_start = strip_w - pad - right_width
    place(lanes['right'], right_start)

    center_width = sum(w.w for w in lanes['center']) + \
        gap * max(0, len(lanes['center']) - 1)
    free_lo = left_end + (gap if lanes['left'] else 0)
    free_hi = right_start - (gap if lanes['right'] else 0)
    center_start = free_lo + (free_hi - free_lo - center_width) / 2
    center_start = max(free_lo, min(center_start, free_hi - center_width))
    place(lanes['center'], center_start)

    out = lanes['left'] + lanes['center'] + lanes['right']
    if esc_widget:
        out.insert(0, esc_widget)
    return out


def render(ctx, widgets, env, *, strip_w=STRIP_W, strip_h=STRIP_H, clear=True):
    if clear:
        T.set_source(ctx, env.theme.background)
        ctx.rectangle(0, 0, strip_w, strip_h)
        ctx.fill()
    for w in widgets:
        ctx.save()
        w.draw(ctx, env)
        ctx.restore()


def render_page(ctx, config, page, env, **kw):
    """Convenience: lay out and draw in one call (used by the editor)."""
    widgets = layout_page(ctx, config, page, env,
                          strip_w=kw.get('strip_w', STRIP_W),
                          strip_h=kw.get('strip_h', STRIP_H))
    render(ctx, widgets, env, **kw)
    return widgets
