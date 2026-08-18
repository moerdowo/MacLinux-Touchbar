"""Config file handling for dfrd.

The daemon runs as root (it owns a DRM node and /dev/uinput) but the config
belongs to the user, so every path here is resolved against a *target user*
rather than against whoever is executing -- see `resolve_user`.

Validation never raises on bad input. This daemon is the Escape key on a
machine with no physical function row, so a typo in a colour must degrade to a
default, not take the keyboard down with it. `validate()` repairs and reports.
"""

import json
import os
import pwd
import time
import uuid

CONFIG_VERSION = 1
CONFIG_NAME = 'config.json'
LANES = ('left', 'center', 'right')


# --- who owns the config ------------------------------------------------

def resolve_user(explicit=None):
    """Return the pwd entry for the human whose config we should use.

    Order: an explicit --user, then SUDO_USER/PKEXEC_UID (we were escalated
    from a desktop session), then the owner of an active graphical session,
    then whoever is running. Root's own home is the last resort because a
    config stashed in /root is invisible to the editor.
    """
    if explicit:
        try:
            return pwd.getpwnam(explicit)
        except KeyError:
            pass
    for var in ('SUDO_USER', 'PKEXEC_UID'):
        val = os.environ.get(var)
        if not val:
            continue
        try:
            return pwd.getpwuid(int(val)) if val.isdigit() else pwd.getpwnam(val)
        except (KeyError, ValueError):
            pass
    # An active session's runtime dir is a reliable hint on a single-seat box.
    try:
        for entry in sorted(os.listdir('/run/user')):
            if entry.isdigit() and int(entry) >= 1000:
                return pwd.getpwuid(int(entry))
    except OSError:
        pass
    return pwd.getpwuid(os.getuid())


def config_dir(user=None):
    user = user or resolve_user()
    base = os.environ.get('XDG_CONFIG_HOME') if user.pw_uid == os.getuid() else None
    return os.path.join(base or os.path.join(user.pw_dir, '.config'), 'dfrd')


def config_path(user=None):
    return os.path.join(config_dir(user), CONFIG_NAME)


def assets_dir(user=None):
    return os.path.join(config_dir(user), 'assets')


# --- defaults -----------------------------------------------------------

def new_id(prefix='w'):
    return f'{prefix}-{uuid.uuid4().hex[:8]}'


def widget(type_, **props):
    """Build a widget dict. `action`, `width` and `flex` are lifted out of
    props because every widget understands them."""
    w = {'id': new_id(type_[:3]), 'type': type_}
    for key in ('width', 'flex', 'action', 'long_action', 'hidden'):
        if key in props:
            w[key] = props.pop(key)
    w['props'] = props
    return w


def key_action(key):
    return {'type': 'key', 'keys': [key]}


def default_config():
    """A first-run strip that demonstrates every category of widget."""
    return {
        'version': CONFIG_VERSION,
        'theme': {'preset': 'midnight'},
        'settings': {
            'always_esc': True,
            'esc_width': 92,
            'flip_x': False,
            'flip_y': False,
            'tap_feedback': True,
            'swipe_pages': True,
            'dim_after': 0,
            'revert_on_exit': True,
        },
        'pages': [
            {
                'id': 'home', 'name': 'Home', 'icon': 'home',
                'left': [
                    widget('button', label='Term', icon='terminal',
                           action={'type': 'exec', 'command': 'alacritty'}),
                    widget('button', label='Files', icon='folder',
                           action={'type': 'exec', 'command': 'nautilus'}),
                    widget('button', label='Web', icon='globe',
                           action={'type': 'exec', 'command': 'xdg-open https://'}),
                ],
                'center': [
                    widget('media', flex=1),
                ],
                'right': [
                    widget('pager', mode='next', icon='pages'),
                    widget('crypto', coin='bitcoin', symbol='BTC',
                           sparkline=True, width=190),
                    widget('battery', width=104),
                    widget('clock', format='%H:%M', subformat='%a %d', width=104),
                ],
            },
            {
                'id': 'fn', 'name': 'Function keys', 'icon': 'keyboard',
                'left': [], 'center': [
                    widget('button', label=f'F{i}', flex=1, action=key_action(f'F{i}'))
                    for i in range(1, 13)
                ], 'right': [widget('pager', mode='next', icon='pages')],
            },
            {
                'id': 'media', 'name': 'Media', 'icon': 'music',
                'left': [
                    widget('button', icon='previous', width=78,
                           action=key_action('PREVIOUSSONG')),
                    widget('button', icon='play', width=78, style='accent',
                           action=key_action('PLAYPAUSE')),
                    widget('button', icon='next', width=78,
                           action=key_action('NEXTSONG')),
                ],
                'center': [widget('media', flex=1)],
                'right': [
                    widget('volume', width=250),
                    widget('pager', mode='next', icon='pages'),
                ],
            },
            {
                'id': 'system', 'name': 'System', 'icon': 'gauge',
                'left': [
                    widget('cpu', width=210, style='graph'),
                    widget('memory', width=150),
                    widget('temperature', width=130),
                ],
                'center': [widget('workspaces', flex=1)],
                'right': [
                    widget('brightness', width=200),
                    widget('battery', width=110),
                    widget('pager', mode='next', icon='pages'),
                ],
            },
        ],
    }


# --- load / save --------------------------------------------------------

def _coerce_widget(raw, problems, lane, index):
    if not isinstance(raw, dict):
        problems.append(f'{lane}[{index}]: not an object, dropped')
        return None
    type_ = raw.get('type')
    if not isinstance(type_, str) or not type_:
        problems.append(f'{lane}[{index}]: missing type, dropped')
        return None
    out = {
        'id': raw.get('id') or new_id(type_[:3]),
        'type': type_,
        'props': raw.get('props') if isinstance(raw.get('props'), dict) else {},
    }
    for key in ('width', 'flex'):
        if raw.get(key) is None:
            continue
        try:
            # Keep whole numbers whole: this file is meant to be hand-edited,
            # and a save that rewrites 78 as 78.0 makes every diff noisy.
            value = float(raw[key])
            out[key] = int(value) if value.is_integer() else value
        except (TypeError, ValueError):
            problems.append(f'{lane}[{index}]: bad {key}, ignored')
    for key in ('action', 'long_action'):
        if isinstance(raw.get(key), dict):
            out[key] = raw[key]
    if raw.get('hidden'):
        out['hidden'] = True
    return out


def validate(data):
    """Return (config, problems). Never raises; repairs what it can."""
    problems = []
    if not isinstance(data, dict):
        problems.append('config root is not an object; using defaults')
        return default_config(), problems

    cfg = default_config()
    cfg['version'] = CONFIG_VERSION

    if isinstance(data.get('theme'), dict):
        cfg['theme'] = data['theme']
    if isinstance(data.get('settings'), dict):
        merged = dict(cfg['settings'])
        merged.update(data['settings'])
        cfg['settings'] = merged

    pages = []
    for pi, page in enumerate(data.get('pages') or []):
        if not isinstance(page, dict):
            problems.append(f'page {pi}: not an object, dropped')
            continue
        out = {
            'id': page.get('id') or new_id('page'),
            'name': page.get('name') or f'Page {pi + 1}',
            'icon': page.get('icon') or '',
        }
        for lane in LANES:
            items = []
            for wi, raw in enumerate(page.get(lane) or []):
                got = _coerce_widget(raw, problems, f'page {pi}.{lane}', wi)
                if got:
                    items.append(got)
            out[lane] = items
        pages.append(out)

    if pages:
        cfg['pages'] = pages
    else:
        problems.append('no usable pages; using defaults')

    seen = set()
    for page in cfg['pages']:
        for lane in LANES:
            for w in page[lane]:
                while w['id'] in seen:
                    w['id'] = new_id(w['type'][:3])
                seen.add(w['id'])
    return cfg, problems


def load(path=None, user=None):
    """Load and validate. A missing file yields defaults, not an error."""
    path = path or config_path(user)
    if not os.path.exists(path):
        return default_config(), [f'{path} not found; using defaults']
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return default_config(), [f'{path}: {exc}; using defaults']
    return validate(data)


def save(cfg, path=None, user=None):
    """Atomic write, with a timestamped backup of the previous config.

    Atomic because the daemon may be watching the file: a half-written config
    read mid-save would blank the strip, and the strip is the Escape key.
    """
    user = user or resolve_user()
    path = path or config_path(user)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(assets_dir(user), exist_ok=True)

    if os.path.exists(path):
        backup = f'{path}.bak'
        try:
            os.replace(path, backup)
        except OSError:
            pass

    tmp = f'{path}.tmp.{os.getpid()}'
    with open(tmp, 'w') as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write('\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)

    # Running as root, hand ownership back so the editor can still write.
    if os.getuid() == 0:
        for target in (os.path.dirname(path), path, assets_dir(user)):
            try:
                os.chown(target, user.pw_uid, user.pw_gid)
            except OSError:
                pass
    return path


def page_by_id(cfg, page_id):
    for page in cfg.get('pages', []):
        if page.get('id') == page_id:
            return page
    return cfg['pages'][0] if cfg.get('pages') else None


def find_widget(cfg, widget_id):
    """Return (page, lane, index, widget) or (None, None, None, None)."""
    for page in cfg.get('pages', []):
        for lane in LANES:
            for i, w in enumerate(page.get(lane, [])):
                if w.get('id') == widget_id:
                    return page, lane, i, w
    return None, None, None, None


def stamp():
    return time.strftime('%Y-%m-%d %H:%M:%S')
