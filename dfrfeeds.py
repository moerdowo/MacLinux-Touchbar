"""Live data sources for Touch Bar widgets.

Widgets never fetch anything themselves. They ask the hub for a value by kind
and parameters, and get back whatever was last successfully retrieved -- so a
slow HTTP call or a flaky network can never stall the render loop or, worse,
delay the Escape key. Every feed is refreshed on a worker thread and every
reader gets a cached snapshot immediately.

Feeds are created lazily on first request and retired when nothing has asked
for them in a while, so switching to a page without a crypto widget stops the
crypto polling on its own.
"""

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = 'dfrd/1.0 (+touchbar)'
HTTP_TIMEOUT = 8
IDLE_RETIRE = 120.0          # seconds a feed may go unread before retirement


def http_json(url, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def downsample(series, target=48):
    """Reduce a series to ~target points by averaging buckets."""
    if not series or len(series) <= target:
        return list(series)
    step = len(series) / target
    out = []
    for i in range(target):
        lo, hi = int(i * step), max(int(i * step) + 1, int((i + 1) * step))
        chunk = series[lo:hi]
        if chunk:
            out.append(sum(chunk) / len(chunk))
    return out


# --- feed base ----------------------------------------------------------

class Feed:
    kind = 'abstract'
    interval = 60.0
    network = False

    def __init__(self, hub, **params):
        self.hub = hub
        self.params = params
        self.value = {}
        self.error = None
        self.updated = 0.0
        self.revision = 0
        self.last_read = time.monotonic()
        self._due = 0.0
        self._inflight = False

    @staticmethod
    def make_key(kind, params):
        bits = ','.join(f'{k}={v}' for k, v in sorted(params.items()))
        return f'{kind}:{bits}'

    def fetch(self):
        raise NotImplementedError

    def _run(self):
        try:
            got = self.fetch()
            if got != self.value:
                self.value = got
                self.revision += 1
            self.error = None
        except Exception as exc:                       # noqa: BLE001
            # Keep the last good value: a stale price beats a blank widget.
            self.error = f'{type(exc).__name__}: {exc}'
            self.revision += 1
        finally:
            self.updated = time.monotonic()
            self._due = self.updated + self.interval
            self._inflight = False


# --- local system -------------------------------------------------------

class CpuFeed(Feed):
    kind, interval = 'cpu', 1.0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._prev = self._sample()
        self._history = [0.0] * 48

    @staticmethod
    def _sample():
        with open('/proc/stat') as fh:
            parts = [int(v) for v in fh.readline().split()[1:]]
        return sum(parts), parts[3] + parts[4]

    def fetch(self):
        total, idle = self._sample()
        dt, didle = total - self._prev[0], idle - self._prev[1]
        self._prev = (total, idle)
        load = max(0.0, min(1.0, 1.0 - didle / dt)) if dt > 0 else 0.0
        self._history = self._history[1:] + [load]
        try:
            with open('/proc/loadavg') as fh:
                avg = float(fh.read().split()[0])
        except OSError:
            avg = 0.0
        return {'load': load, 'history': list(self._history), 'loadavg': avg}


class MemoryFeed(Feed):
    kind, interval = 'memory', 3.0

    def fetch(self):
        info = {}
        with open('/proc/meminfo') as fh:
            for line in fh:
                key, _, rest = line.partition(':')
                info[key] = float(rest.split()[0]) * 1024
        total = info.get('MemTotal', 1.0)
        avail = info.get('MemAvailable', total)
        used = total - avail
        swap_total = info.get('SwapTotal', 0.0)
        swap_used = swap_total - info.get('SwapFree', 0.0)
        return {'used': used, 'total': total, 'fraction': used / total,
                'swap_fraction': (swap_used / swap_total) if swap_total else 0.0}


class TemperatureFeed(Feed):
    kind, interval = 'temperature', 4.0

    def fetch(self):
        best, label = None, ''
        want = (self.params.get('sensor') or '').lower()
        for zone in sorted(os.listdir('/sys/class/thermal')):
            if not zone.startswith('thermal_zone'):
                continue
            base = f'/sys/class/thermal/{zone}'
            try:
                kind = open(f'{base}/type').read().strip()
                temp = int(open(f'{base}/temp').read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if want and want not in kind.lower():
                continue
            if best is None or temp > best:
                best, label = temp, kind
        if best is None:
            raise RuntimeError('no thermal zone')
        return {'celsius': best, 'label': label}


class BatteryFeed(Feed):
    kind, interval = 'battery', 10.0

    def fetch(self):
        base = f"/sys/class/power_supply/{self.params.get('name') or 'BAT0'}"
        def read(field, cast=str, default=None):
            try:
                return cast(open(os.path.join(base, field)).read().strip())
            except (OSError, ValueError):
                return default
        capacity = read('capacity', int, None)
        if capacity is None:
            raise RuntimeError('no battery')
        status = read('status', str, 'Unknown')
        energy = read('energy_now', float) or read('charge_now', float)
        power = read('power_now', float) or read('current_now', float)
        hours = (energy / power) if energy and power else None
        return {'percent': capacity, 'status': status,
                'charging': status in ('Charging', 'Full'),
                'hours': hours}


class DiskFeed(Feed):
    kind, interval = 'disk', 30.0

    def fetch(self):
        path = self.params.get('path') or '/'
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {'path': path, 'total': total, 'free': free,
                'fraction': (total - free) / total if total else 0.0}


class NetworkFeed(Feed):
    kind, interval = 'network', 2.0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._prev = (0, 0, time.monotonic())

    def fetch(self):
        rx = tx = 0
        with open('/proc/net/dev') as fh:
            for line in fh.readlines()[2:]:
                name, _, rest = line.partition(':')
                if name.strip() in ('lo',):
                    continue
                cols = rest.split()
                rx += int(cols[0])
                tx += int(cols[8])
        now = time.monotonic()
        prx, ptx, pt = self._prev
        dt = max(1e-3, now - pt)
        self._prev = (rx, tx, now)
        return {'rx_rate': max(0, rx - prx) / dt, 'tx_rate': max(0, tx - ptx) / dt}


# --- session-owned state ------------------------------------------------

class VolumeFeed(Feed):
    kind, interval = 'volume', 1.0

    def fetch(self):
        rc, out, _ = self.hub.session.run(
            ['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'])
        if rc != 0:
            raise RuntimeError('wpctl unavailable')
        # "Volume: 0.24" or "Volume: 0.24 [MUTED]"
        parts = out.split()
        level = float(parts[1]) if len(parts) > 1 else 0.0
        return {'level': level, 'muted': 'MUTED' in out}


class BrightnessFeed(Feed):
    kind, interval = 'brightness', 2.0

    def fetch(self):
        base = '/sys/class/backlight'
        devices = sorted(os.listdir(base))
        if not devices:
            raise RuntimeError('no backlight')
        dev = os.path.join(base, self.params.get('device') or devices[0])
        cur = int(open(os.path.join(dev, 'brightness')).read())
        mx = int(open(os.path.join(dev, 'max_brightness')).read())
        return {'level': cur / mx if mx else 0.0, 'raw': cur, 'max': mx,
                'device': os.path.basename(dev)}


class MprisFeed(Feed):
    """Now-playing over the user's session bus.

    Uses Gio rather than shelling out to playerctl, which is not installed
    here, and connects to the bus by address so it works from a root daemon.
    """
    kind, interval = 'mpris', 2.0

    def _connection(self):
        conn = getattr(self, '_conn', None)
        if conn is not None:
            return conn
        import gi
        gi.require_version('Gio', '2.0')
        from gi.repository import Gio
        address = self.hub.session.bus_address
        if not address:
            raise RuntimeError('no session bus')
        self._gio = Gio
        self._conn = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        return self._conn

    def _call(self, conn, name, iface, path, method, args):
        return conn.call_sync(name, path, iface, method, args, None,
                              self._gio.DBusCallFlags.NONE, 2000, None)

    def fetch(self):
        conn = self._connection()
        Gio = self._gio
        names = self._call(conn, 'org.freedesktop.DBus', 'org.freedesktop.DBus',
                           '/org/freedesktop/DBus', 'ListNames', None)[0]
        players = [n for n in names if n.startswith('org.mpris.MediaPlayer2.')]
        if not players:
            return {'playing': False, 'player': None}

        want = self.params.get('player')
        if want:
            players.sort(key=lambda n: 0 if want.lower() in n.lower() else 1)

        best = None
        for name in players:
            try:
                props = self._call(
                    conn, name, 'org.freedesktop.DBus.Properties',
                    '/org/mpris/MediaPlayer2', 'GetAll',
                    Gio.Variant('(s)', ('org.mpris.MediaPlayer2.Player',)))[0]
            except Exception:                          # noqa: BLE001
                continue
            meta = props.get('Metadata') or {}
            status = props.get('PlaybackStatus') or 'Stopped'
            artists = meta.get('xesam:artist') or []
            entry = {
                'playing': status == 'Playing',
                'status': status,
                'player': name.rsplit('.', 1)[-1],
                'title': meta.get('xesam:title') or '',
                'artist': ', '.join(artists) if isinstance(artists, list) else str(artists),
                'album': meta.get('xesam:album') or '',
                'art': meta.get('mpris:artUrl') or '',
                'length': (meta.get('mpris:length') or 0) / 1e6,
                'position': (props.get('Position') or 0) / 1e6,
            }
            if entry['playing']:
                return entry
            best = best or entry
        return best or {'playing': False, 'player': None}


class WorkspacesFeed(Feed):
    kind, interval = 'workspaces', 1.0

    def fetch(self):
        rc, out, _ = self.hub.session.hyprctl('workspaces', json_out=True)
        if rc != 0:
            raise RuntimeError('hyprctl unavailable')
        spaces = json.loads(out or '[]')
        rc2, out2, _ = self.hub.session.hyprctl('activeworkspace', json_out=True)
        active = json.loads(out2 or '{}').get('id') if rc2 == 0 else None
        return {
            'active': active,
            'spaces': sorted(
                [{'id': s.get('id'), 'name': str(s.get('name')),
                  'windows': s.get('windows', 0)} for s in spaces
                 if isinstance(s.get('id'), int) and s.get('id') > 0],
                key=lambda s: s['id']),
        }


class ScriptFeed(Feed):
    """Arbitrary shell command -- the escape hatch for anything not built in."""
    kind = 'script'

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.interval = max(1.0, float(self.params.get('interval') or 10))

    def fetch(self):
        command = self.params.get('command') or 'true'
        rc, out, err = self.hub.session.run(['/bin/sh', '-c', command],
                                            timeout=min(20, self.interval + 5))
        text = (out or '').strip()
        payload = None
        if text.startswith('{'):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        return {'text': text.splitlines()[0] if text else '',
                'lines': text.splitlines(), 'rc': rc,
                'json': payload, 'stderr': (err or '').strip()}


# --- network ------------------------------------------------------------

class CryptoFeed(Feed):
    """CoinGecko. One market_chart call yields price, change and sparkline."""
    kind, interval, network = 'crypto', 120.0, True

    def fetch(self):
        coin = self.params.get('coin') or 'bitcoin'
        vs = (self.params.get('vs') or 'usd').lower()
        url = (f'https://api.coingecko.com/api/v3/coins/{urllib.parse.quote(coin)}'
               f'/market_chart?vs_currency={urllib.parse.quote(vs)}&days=1')
        data = http_json(url)
        prices = [p[1] for p in data.get('prices', []) if len(p) == 2]
        if not prices:
            raise RuntimeError('no price data')
        first, last = prices[0], prices[-1]
        return {'price': last, 'currency': vs.upper(),
                'change': (last - first) / first * 100 if first else 0.0,
                'series': downsample(prices)}


class StockFeed(Feed):
    """Yahoo Finance chart endpoint: quote plus intraday series, no API key."""
    kind, interval, network = 'stock', 180.0, True

    def fetch(self):
        symbol = self.params.get('symbol') or 'AAPL'
        rng = self.params.get('range') or '1d'
        interval = '5m' if rng == '1d' else '1d'
        url = (f'https://query1.finance.yahoo.com/v8/finance/chart/'
               f'{urllib.parse.quote(symbol)}?range={rng}&interval={interval}')
        data = http_json(url)
        result = (data.get('chart', {}).get('result') or [None])[0]
        if not result:
            raise RuntimeError(f'no data for {symbol}')
        meta = result.get('meta', {})
        closes = [c for c in
                  (result.get('indicators', {}).get('quote') or [{}])[0].get('close', [])
                  if c is not None]
        price = meta.get('regularMarketPrice') or (closes[-1] if closes else None)
        prev = meta.get('chartPreviousClose') or meta.get('previousClose') or \
            (closes[0] if closes else None)
        if price is None:
            raise RuntimeError('no price')
        return {'symbol': meta.get('symbol', symbol).upper(),
                'price': price, 'currency': meta.get('currency', 'USD'),
                'change': ((price - prev) / prev * 100) if prev else 0.0,
                'series': downsample(closes)}


#: WMO weather codes -> (label, icon name from dfrtheme.ICONS)
WMO = {
    0: ('Clear', 'sun'), 1: ('Mostly clear', 'sun'), 2: ('Partly cloudy', 'cloud'),
    3: ('Overcast', 'cloud'), 45: ('Fog', 'fog'), 48: ('Rime fog', 'fog'),
    51: ('Drizzle', 'rain'), 53: ('Drizzle', 'rain'), 55: ('Drizzle', 'rain'),
    61: ('Rain', 'rain'), 63: ('Rain', 'rain'), 65: ('Heavy rain', 'rain'),
    66: ('Freezing rain', 'rain'), 67: ('Freezing rain', 'rain'),
    71: ('Snow', 'snow'), 73: ('Snow', 'snow'), 75: ('Heavy snow', 'snow'),
    77: ('Snow grains', 'snow'), 80: ('Showers', 'rain'), 81: ('Showers', 'rain'),
    82: ('Violent showers', 'rain'), 85: ('Snow showers', 'snow'),
    86: ('Snow showers', 'snow'), 95: ('Thunderstorm', 'thunder'),
    96: ('Thunderstorm', 'thunder'), 99: ('Thunderstorm', 'thunder'),
}


class WeatherFeed(Feed):
    """Open-Meteo, with city-name geocoding. No API key for either call."""
    kind, interval, network = 'weather', 900.0, True

    _geo_cache = {}

    def _geocode(self, place):
        if place in self._geo_cache:
            return self._geo_cache[place]
        url = ('https://geocoding-api.open-meteo.com/v1/search?count=1&name='
               + urllib.parse.quote(place))
        hits = http_json(url).get('results') or []
        if not hits:
            raise RuntimeError(f'unknown place: {place}')
        hit = hits[0]
        found = (hit['latitude'], hit['longitude'], hit.get('name', place))
        self._geo_cache[place] = found
        return found

    def fetch(self):
        place = self.params.get('place')
        if not place:
            raise RuntimeError('no city set')
        units = self.params.get('units') or 'metric'
        lat, lon, label = self._geocode(place)
        unit = 'celsius' if units == 'metric' else 'fahrenheit'
        url = (f'https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}'
               f'&current=temperature_2m,weather_code,relative_humidity_2m,apparent_temperature'
               f'&daily=temperature_2m_max,temperature_2m_min'
               f'&temperature_unit={unit}&timezone=auto&forecast_days=1')
        data = http_json(url)
        cur = data.get('current', {})
        daily = data.get('daily', {})
        code = int(cur.get('weather_code', 0))
        label_text, glyph = WMO.get(code, ('Unknown', 'cloud'))
        return {
            'place': label,
            'temp': cur.get('temperature_2m'),
            'feels': cur.get('apparent_temperature'),
            'humidity': cur.get('relative_humidity_2m'),
            'code': code, 'label': label_text, 'glyph': glyph,
            'high': (daily.get('temperature_2m_max') or [None])[0],
            'low': (daily.get('temperature_2m_min') or [None])[0],
            'unit': '°C' if units == 'metric' else '°F',
        }


FEED_TYPES = {cls.kind: cls for cls in (
    CpuFeed, MemoryFeed, TemperatureFeed, BatteryFeed, DiskFeed, NetworkFeed,
    VolumeFeed, BrightnessFeed, MprisFeed, WorkspacesFeed, ScriptFeed,
    CryptoFeed, StockFeed, WeatherFeed,
)}


# --- hub ----------------------------------------------------------------

class FeedHub:
    """Lazy registry of feeds, refreshed off the render thread."""

    def __init__(self, session, workers=4):
        self.session = session
        self._feeds = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix='dfrd-feed')
        self._closed = False

    def get(self, kind, **params):
        """Snapshot of a feed's value. Creates the feed on first request.

        Returns a dict with the feed's own fields plus `_ok`, `_error`,
        `_age` and `_rev`, so a widget can render a stale or failed state
        without asking any further questions.
        """
        if kind not in FEED_TYPES:
            return {'_ok': False, '_error': f'unknown feed {kind}', '_rev': 0, '_age': 0}
        key = Feed.make_key(kind, params)
        now = time.monotonic()
        with self._lock:
            feed = self._feeds.get(key)
            if feed is None:
                feed = FEED_TYPES[kind](self, **params)
                self._feeds[key] = feed
            feed.last_read = now
            snapshot = dict(feed.value)
            snapshot['_ok'] = feed.error is None and bool(feed.value)
            snapshot['_error'] = feed.error
            snapshot['_rev'] = feed.revision
            snapshot['_age'] = now - feed.updated if feed.updated else None
        return snapshot

    def revision(self, kind, **params):
        with self._lock:
            feed = self._feeds.get(Feed.make_key(kind, params))
            return feed.revision if feed else -1

    def pump(self):
        """Submit any feed that is due. Cheap; call once per render tick."""
        if self._closed:
            return
        now = time.monotonic()
        with self._lock:
            for key, feed in list(self._feeds.items()):
                if now - feed.last_read > IDLE_RETIRE:
                    del self._feeds[key]
                    continue
                if feed._inflight or now < feed._due:
                    continue
                feed._inflight = True
                self._pool.submit(feed._run)

    def refresh_now(self, kind=None, **params):
        """Force a refresh -- used right after an action changes something."""
        with self._lock:
            for key, feed in self._feeds.items():
                if kind is None or key.startswith(f'{kind}:'):
                    feed._due = 0.0

    def close(self):
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
