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

import cmath
import json
import math
import os
import shutil
import struct
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

    def close(self):
        """Release anything this feed owns. Called on retirement and shutdown.

        Almost every feed here is a function of /proc or an HTTP call and has
        nothing to release, so the default is a no-op -- but the audio feed
        holds a live capture process, and a retired feed that kept PipeWire
        recording would be a leak nobody would ever notice.
        """

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


# --- audio --------------------------------------------------------------

#: Capture format. 22050 Hz mono reaches 11 kHz, which is the whole of the
#: useful display range, at a quarter of the samples 44.1 kHz stereo would
#: cost -- and this runs on a 2016 laptop with a fragile GPU beside it.
AUDIO_RATE = 22050
AUDIO_FFT = 1024                 # 46ms window, 21.5 Hz per bin
AUDIO_ANALYSIS_HZ = 60.0         # analysis cadence, independent of the render loop
AUDIO_SILENCE = 1.2e-3           # absolute floor under which it is always silence
AUDIO_SILENCE_HOLD = 0.75        # seconds under the threshold before we believe it
AUDIO_FLOOR_WINDOW = 20.0        # seconds the noise floor is measured over
AUDIO_FLOOR_MARGIN = 3.2         # how far above the floor still counts as silence
AUDIO_FLOOR_MAX = 6e-3           # a "floor" louder than this (~-44 dBFS) is music
AUDIO_WARMUP = 0.25              # seconds of capture discarded at stream start
AUDIO_TARGET_DB = -16.0          # where auto-gain tries to put the loudest band
AUDIO_AGC_RANGE = (-8.0, 36.0)   # how far auto-gain may push, in dB


def _fft_tables(n):
    """Bit-reversal permutation and twiddle factors for a radix-2 FFT."""
    levels = n.bit_length() - 1
    rev = [0] * n
    for i in range(n):
        r, x = 0, i
        for _ in range(levels):
            r = (r << 1) | (x & 1)
            x >>= 1
        rev[i] = r
    return rev, [cmath.exp(-2j * math.pi * k / n) for k in range(n // 2)]


def _fft(samples, rev, twiddles):
    """In-place iterative radix-2 FFT.

    Pure Python on purpose: numpy is not installed here and this is the only
    thing in dfrd that would want it, so a 1024-point transform measured at
    1.7ms -- about 5% of one core at 30fps -- is a much smaller price than a
    dependency the daemon would then need at boot.
    """
    n = len(samples)
    out = [samples[rev[i]] for i in range(n)]
    size = 2
    while size <= n:
        half, step = size >> 1, n // size
        for i in range(0, n, size):
            k = 0
            for j in range(i, i + half):
                t = twiddles[k] * out[j + half]
                u = out[j]
                out[j] = u + t
                out[j + half] = u - t
                k += step
        size <<= 1
    return out


class AudioFeed(Feed):
    """Spectrum of what this machine is actually playing.

    Not a poller like every other feed here. PipeWire is asked once for a
    continuous monitor stream and a reader thread owns it for the feed's whole
    life; `fetch` only hands back the newest analysis. The Feed machinery still
    drives retirement and error reporting, which is why this is a Feed at all
    rather than something bolted to the daemon -- leave the visualiser page and
    the capture stops on its own, exactly like the crypto poller does.

    The source is the *monitor* of the default sink, so it hears the mix
    leaving the machine: every application, post-volume, and nothing from the
    microphone. Following the default sink rather than a fixed device name
    means switching output to headphones does not silently freeze the display.
    """
    kind, interval = 'audio', 0.0    # analysed on its own thread; never polled

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bands = max(4, min(64, int(self.params.get('bands') or 24)))
        self.gain_db = float(self.params.get('gain') or 0.0)
        self.auto_gain = self.params.get('auto_gain', True) not in (
            False, 0, '0', 'false', 'no', 'off')
        self._lock = threading.Lock()
        self._proc = None
        self._thread = None
        self._stopping = threading.Event()
        self._snapshot = {}
        self._fail = None
        self._rev, self._tw = _fft_tables(AUDIO_FFT)
        self._window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (AUDIO_FFT - 1))
                        for i in range(AUDIO_FFT)]
        self._edges = self._band_edges()
        self._smooth = [0.0] * self.bands
        self._peaks = [0.0] * self.bands
        self._level = 0.0
        # Silence has to persist before it counts. An analog loopback never
        # reads a true zero -- this machine idles around -55 dBFS -- and a rest
        # between two phrases is not the end of the music, so a bare threshold
        # would flip the display into its idle animation mid-track.
        self._quiet_since = None
        # The measured noise floor, as a sliding minimum. An analog loopback
        # never reads a true zero -- this machine idles near -55 dBFS -- and
        # how far above zero it sits is a property of the hardware, so it is
        # learned rather than hardcoded. Getting this wrong is not cosmetic:
        # with auto-gain riding, a floor mistaken for signal is amplified into
        # a full display of nothing, and the idle sweep never appears.
        self._floor = None
        self._floor_candidate = None
        self._floor_rotated = 0.0
        # Auto-gain, in dB. The sink monitor is *post-volume*: at 24% system
        # volume -- which is where this machine actually sits -- a track that
        # sounds normal measures around -50 dBFS, and a fixed window put the
        # whole display one cell above the floor. Every real visualiser rides
        # the gain for this reason. It only ever changes what the display is
        # scaled to, never which frequencies are there.
        self._agc = 0.0

    # -- setup ----------------------------------------------------------

    def _band_edges(self):
        """Log-spaced bin ranges. Even spacing would spend most of the strip
        on 5-11 kHz, where music has almost nothing to show."""
        lo, hi = 40.0, min(10000.0, AUDIO_RATE / 2 - 100)
        ratio = (hi / lo) ** (1.0 / self.bands)
        edges = []
        for i in range(self.bands):
            f0, f1 = lo * ratio ** i, lo * ratio ** (i + 1)
            k0 = max(1, int(f0 * AUDIO_FFT / AUDIO_RATE))
            k1 = max(k0 + 1, int(f1 * AUDIO_FFT / AUDIO_RATE))
            edges.append((k0, min(k1, AUDIO_FFT // 2), f0, f1))
        return edges

    def _source(self):
        """The monitor of whatever sink is currently the default."""
        override = self.params.get('source')
        if override:
            return str(override)
        rc, out, _ = self.hub.session.run(['pactl', 'get-default-sink'])
        name = (out or '').strip()
        if rc != 0 or not name:
            raise RuntimeError('no default sink (is PipeWire running?)')
        return f'{name}.monitor'

    def _start(self):
        source = self._source()
        argv = ['pw-record', '--target', source,
                '--rate', str(AUDIO_RATE), '--channels', '1',
                '--format', 's16', '-']
        self._proc = self.hub.session.stream(argv)
        self._thread = threading.Thread(target=self._reader, name='dfrd-audio',
                                        daemon=True)
        self._thread.start()
        return source

    # -- capture --------------------------------------------------------

    def _reader(self):
        """Own the capture pipe: refill a ring buffer, analyse at a fixed rate."""
        ring = [0.0] * AUDIO_FFT
        write = 0
        chunk = 1024                       # bytes; 512 frames, ~23ms
        next_analysis = 0.0
        # Held locally: close() drops the feed's reference to release the pipe,
        # and this thread must not race it into an AttributeError on None.
        proc = self._proc
        stdout = proc.stdout
        # Every stream here opens with a burst: the first ~100ms measures around
        # -7 dBFS on an idle sink, then it settles to the real floor near -60.
        # Whatever it is -- a stale buffer, a resampler priming -- it is not the
        # machine's audio, and letting it through both spikes the display on
        # arrival and teaches the floor detector that silence is loud.
        skip = int(AUDIO_RATE * AUDIO_WARMUP)
        try:
            while not self._stopping.is_set():
                block = stdout.read(chunk)
                if not block:
                    break
                count = len(block) // 2
                if skip > 0:
                    skip -= count
                    continue
                if count:
                    for value in struct.unpack(f'<{count}h', block[:count * 2]):
                        ring[write] = value / 32768.0
                        write = (write + 1) % AUDIO_FFT
                now = time.monotonic()
                if now >= next_analysis:
                    next_analysis = now + 1.0 / AUDIO_ANALYSIS_HZ
                    self._analyse(ring, write, now)
        except (OSError, ValueError) as exc:
            with self._lock:
                self._fail = f'capture ended: {exc}'
        else:
            if not self._stopping.is_set():
                try:
                    err = (proc.stderr.read(400) or b'').decode(
                        'utf-8', 'replace').strip() if proc.stderr else ''
                except (OSError, ValueError):
                    err = ''
                with self._lock:
                    self._fail = err.splitlines()[-1] if err else 'capture ended'

    def _analyse(self, ring, write, now):
        """Ring buffer -> per-band 0..1, smoothed, with falling peak holds."""
        ordered = ring[write:] + ring[:write]
        rms = math.sqrt(sum(v * v for v in ordered) / AUDIO_FFT)
        spectrum = _fft([complex(ordered[i] * self._window[i], 0.0)
                         for i in range(AUDIO_FFT)], self._rev, self._tw)

        # Attack fast, release slow: a meter that falls as quickly as it rises
        # reads as noise. Coefficients are per-second so the look does not
        # change with the analysis rate.
        dt = 1.0 / AUDIO_ANALYSIS_HZ
        attack = 1.0 - math.exp(-dt / 0.020)
        release = 1.0 - math.exp(-dt / 0.180)
        peak_fall = dt / 0.9

        # Two passes: the band levels are measured before auto-gain is applied,
        # so the gain is driven by the signal rather than by its own output.
        raw = []
        for k0, k1, f0, _f1 in self._edges:
            power = 0.0
            for k in range(k0, k1):
                c = spectrum[k]
                power += c.real * c.real + c.imag * c.imag
            mag = math.sqrt(power) / (AUDIO_FFT / 2.0)
            db = 20.0 * math.log10(mag + 1e-10)
            # Music falls off with frequency, so an untilted display is a wall
            # of bass and nothing else. +3 dB per octave above the low edge
            # evens it out without inventing detail that is not there.
            raw.append(db + 3.0 * math.log2(max(f0, 40.0) / 40.0) + self.gain_db)

        values = []
        for index, db in enumerate(raw):
            value = min(1.0, max(0.0, (db + self._agc + 62.0) / 56.0))
            previous = self._smooth[index]
            coeff = attack if value > previous else release
            smoothed = previous + (value - previous) * coeff
            self._smooth[index] = smoothed
            self._peaks[index] = (smoothed if smoothed >= self._peaks[index]
                                  else max(smoothed, self._peaks[index] - peak_fall))
            values.append(smoothed)

        self._level += (min(1.0, rms * 6.0) - self._level) * (
            attack if rms * 6.0 > self._level else release)

        # Sliding minimum: the quietest moment in the last window or two. It
        # cannot drift upward past the quietest thing actually heard, so a long
        # loud passage does not teach it that loud is normal.
        self._floor_candidate = (rms if self._floor_candidate is None
                                 else min(self._floor_candidate, rms))
        # The very first window is short. Until a floor exists everything looks
        # like signal, so auto-gain winds up and the display shows amplified
        # hiss; three seconds of that at startup is tolerable, twenty is not.
        window = 3.0 if self._floor is None else AUDIO_FLOOR_WINDOW
        if now - self._floor_rotated >= window:
            self._floor_rotated = now
            # Only believe a plausible floor. Starting the feed while music is
            # already playing -- which is exactly when someone switches to this
            # page -- makes the window's minimum the *music*, and a floor
            # learned from music silences everything quieter than it. Rejecting
            # it just defers the measurement to a window that contains a gap.
            if self._floor_candidate is not None and \
                    self._floor_candidate <= AUDIO_FLOOR_MAX:
                self._floor = self._floor_candidate
            self._floor_candidate = rms
        floor = self._floor
        threshold = max(AUDIO_SILENCE, (floor or 0.0) * AUDIO_FLOOR_MARGIN)

        if rms >= threshold:
            self._quiet_since = None
        elif self._quiet_since is None:
            self._quiet_since = now
        silent = (self._quiet_since is not None and
                  now - self._quiet_since >= AUDIO_SILENCE_HOLD)

        if self.auto_gain and not silent and raw:
            # Come down fast, go up slowly. A loud passage that pins every band
            # should stop pinning them within a beat or two, but the gain must
            # not creep up through a quiet passage and then detonate on the
            # next chorus. Frozen while silent, so the floor is never amplified
            # into a display of nothing.
            wanted = AUDIO_TARGET_DB - max(raw)
            coeff = 1.0 - math.exp(-dt / (0.25 if wanted < self._agc else 2.5))
            self._agc += (wanted - self._agc) * coeff
            self._agc = min(AUDIO_AGC_RANGE[1], max(AUDIO_AGC_RANGE[0], self._agc))

        # Silence is reported as silence. The gain is frozen but still high
        # from whatever last played, so the bands at this point are the noise
        # floor multiplied by 30 dB -- a full and entirely fictional spectrum.
        # A consumer that ignores `silent` should still not be handed that.
        if silent:
            values = [0.0] * self.bands
            self._peaks = [0.0] * self.bands

        # While silent the snapshot is held *identical* frame to frame, rather
        # than carrying a live timestamp and a jittering rms. The hub only
        # bumps a feed's revision when its value actually changes, so a stable
        # snapshot is what lets a reader stop being woken -- which is the
        # difference between a strip that is dark and a strip that is dark and
        # still being redrawn thirty times a second.
        with self._lock:
            self._snapshot = {
                'bands': [round(v, 4) for v in values],
                'peaks': [round(v, 4) for v in self._peaks],
                'level': 0.0 if silent else round(self._level, 4),
                'rms': 0.0 if silent else round(rms, 6),
                'silent': silent,
                'gain_db': round(self._agc, 1),
                'floor': round(floor or 0.0, 6),
                'count': self.bands,
                'at': 0.0 if silent else now,
            }

    # -- Feed interface -------------------------------------------------

    def fetch(self):
        if self._proc is None:
            source = self._start()
            self.interval = 0.0
            return {'bands': [0.0] * self.bands, 'peaks': [0.0] * self.bands,
                    'level': 0.0, 'silent': True, 'count': self.bands,
                    'source': source, 'starting': True}
        with self._lock:
            failure, snapshot = self._fail, dict(self._snapshot)
        if failure:
            # Tear the capture down and back off. Left alone this feed is asked
            # for a value every render tick, so a dead PipeWire would raise 20
            # times a second, and every raise bumps the revision and repaints
            # the strip -- a failure that costs more than the feature. The next
            # attempt rebuilds the stream, so unplugging headphones recovers.
            self.close()
            self._stopping.clear()
            with self._lock:
                self._fail = None
            self.interval = 5.0
            raise RuntimeError(failure)
        if not snapshot:
            return {'bands': [0.0] * self.bands, 'peaks': [0.0] * self.bands,
                    'level': 0.0, 'silent': True, 'count': self.bands,
                    'starting': True}
        return snapshot

    def close(self):
        self._stopping.set()
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:                              # noqa: BLE001
            try:
                proc.kill()
            except Exception:                          # noqa: BLE001
                pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:                          # noqa: BLE001
                pass


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
    CryptoFeed, StockFeed, WeatherFeed, AudioFeed,
)}


# --- hub ----------------------------------------------------------------

def _safe_close(feed):
    try:
        feed.close()
    except Exception:                                  # noqa: BLE001
        pass


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
        """Whether a feed has changed since last asked. Counts as a read.

        Marking this as interest matters: the render loop asks every widget
        for its revision on every pass, but only *draws* the ones that changed,
        and drawing is what calls get(). A widget that is up to date and stays
        up to date -- the visualiser once it has gone dark -- would otherwise
        let its feed go idle and be retired out from under it, taking the audio
        capture with it. Nothing would then be left to notice a sound, so the
        strip could never come back.
        """
        with self._lock:
            feed = self._feeds.get(Feed.make_key(kind, params))
            if feed is None:
                return -1
            feed.last_read = time.monotonic()
            return feed.revision

    def pump(self):
        """Submit any feed that is due. Cheap; call once per render tick."""
        if self._closed:
            return
        now = time.monotonic()
        with self._lock:
            for key, feed in list(self._feeds.items()):
                if now - feed.last_read > IDLE_RETIRE:
                    del self._feeds[key]
                    _safe_close(feed)
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
        with self._lock:
            feeds, self._feeds = list(self._feeds.values()), {}
        for feed in feeds:
            _safe_close(feed)
        self._pool.shutdown(wait=False, cancel_futures=True)
