"""What a tap does.

Actions are plain dicts so they round-trip through JSON and through the
editor's property panel without a schema of their own:

    {"type": "key",        "keys": ["CTRL", "C"]}
    {"type": "exec",       "command": "alacritty"}
    {"type": "hypr",       "dispatch": "hl.dsp.focus({ workspace = \\"3\\" })"}
    {"type": "page",       "page": "media" | "+1" | "-1"}
    {"type": "volume",     "set": 0.4 | "delta": 5 | "toggle_mute": true}
    {"type": "brightness", "set": 0.4 | "delta": 10}
    {"type": "url",        "url": "https://…"}
    {"type": "mode",       "mode": "keyboard"}
    {"type": "none"}

Everything that touches the user's session is routed through dfrsession, so a
button launching a terminal starts it in their session and not as root.
"""

import shlex

import dfrinput as di

ACTION_TYPES = ['none', 'key', 'exec', 'url', 'page', 'volume', 'brightness',
                'hypr', 'mode', 'reload']

#: Human labels for the editor's action picker.
ACTION_LABELS = {
    'none': 'Do nothing',
    'key': 'Press keys',
    'exec': 'Run a command',
    'url': 'Open a URL',
    'page': 'Switch page',
    'volume': 'Change volume',
    'brightness': 'Change brightness',
    'hypr': 'Hyprland dispatch',
    'mode': 'Touch Bar mode',
    'reload': 'Reload config',
}


def describe(action):
    """One-line summary, shown on the widget row in the editor."""
    if not action or action.get('type') in (None, 'none'):
        return 'Nothing'
    kind = action.get('type')
    if kind == 'key':
        return '+'.join(action.get('keys') or []) or 'no keys'
    if kind == 'exec':
        return (action.get('command') or '')[:48] or 'no command'
    if kind == 'url':
        return action.get('url') or 'no url'
    if kind == 'page':
        target = action.get('page', '+1')
        return {'+1': 'Next page', '-1': 'Previous page'}.get(target, f'Page: {target}')
    if kind in ('volume', 'brightness'):
        if action.get('toggle_mute'):
            return 'Toggle mute'
        if action.get('set') is not None:
            return f"Set {kind} {float(action['set']) * 100:.0f}%"
        return f"{kind.title()} {action.get('delta', 0):+g}%"
    if kind == 'hypr':
        return (action.get('dispatch') or '')[:48]
    if kind == 'mode':
        return f"Switch to {action.get('mode', 'keyboard')} mode"
    return kind


class ActionRunner:
    """Executes actions on behalf of the daemon."""

    def __init__(self, session, keyboard, daemon=None):
        self.session = session
        self.keyboard = keyboard
        self.daemon = daemon

    def run(self, action):
        """Perform an action. Returns (ok, message); never raises."""
        if not action:
            return True, ''
        kind = action.get('type', 'none')
        handler = getattr(self, f'_do_{kind}', None)
        if handler is None:
            return False, f'unknown action: {kind}'
        try:
            return handler(action)
        except Exception as exc:                       # noqa: BLE001
            return False, f'{kind}: {exc}'

    # -- handlers -------------------------------------------------------

    def _do_none(self, action):
        return True, ''

    def _do_key(self, action):
        names = action.get('keys') or []
        codes = [di.keycode(name) for name in names]
        if not codes or any(c is None for c in codes):
            missing = [n for n, c in zip(names, codes) if c is None]
            return False, f"unknown key: {', '.join(missing)}"
        if self.keyboard is None:
            return False, 'no virtual keyboard'
        self.keyboard.combo(codes)
        return True, '+'.join(names)

    def _do_exec(self, action):
        command = action.get('command')
        if not command:
            return False, 'no command'
        ok, err = self.session.spawn(command)
        return ok, err or command

    def _do_url(self, action):
        url = action.get('url')
        if not url:
            return False, 'no url'
        ok, err = self.session.spawn(f'xdg-open {shlex.quote(url)}')
        return ok, err or url

    def _do_page(self, action):
        if self.daemon is None:
            return False, 'no daemon'
        return self.daemon.switch_page(action.get('page', '+1'))

    def _do_volume(self, action):
        target = '@DEFAULT_AUDIO_SINK@'
        if action.get('toggle_mute'):
            rc, _, err = self.session.run(['wpctl', 'set-mute', target, 'toggle'])
        elif action.get('set') is not None:
            level = max(0.0, min(1.0, float(action['set'])))
            rc, _, err = self.session.run(
                ['wpctl', 'set-volume', target, f'{level:.3f}'])
        else:
            delta = float(action.get('delta', 5))
            sign = '+' if delta >= 0 else '-'
            rc, _, err = self.session.run(
                ['wpctl', 'set-volume', target, f'{abs(delta) / 100:.3f}{sign}'])
        self._refresh('volume')
        return rc == 0, err

    def _do_brightness(self, action):
        if action.get('set') is not None:
            level = max(0.0, min(1.0, float(action['set'])))
            arg = f'{level * 100:.0f}%'
        else:
            delta = float(action.get('delta', 10))
            arg = f'{abs(delta):.0f}%{"+" if delta >= 0 else "-"}'
        rc, _, err = self.session.run(['brightnessctl', '-q', 'set', arg])
        self._refresh('brightness')
        return rc == 0, err

    def _do_hypr(self, action):
        dispatch = action.get('dispatch')
        if not dispatch:
            return False, 'no dispatch'
        # Hyprland's Lua parser rejects the legacy `dispatch workspace 3` form,
        # so whatever the user typed is passed through verbatim.
        rc, out, err = self.session.hyprctl('dispatch', dispatch)
        return rc == 0, (err or out).strip()

    def _do_mode(self, action):
        if self.daemon is None:
            return False, 'no daemon'
        return self.daemon.switch_mode(action.get('mode', 'keyboard'))

    def _do_reload(self, action):
        if self.daemon is None:
            return False, 'no daemon'
        self.daemon.request_reload()
        return True, 'reloading'

    def _refresh(self, kind):
        if self.daemon is not None and getattr(self.daemon, 'hub', None):
            self.daemon.hub.refresh_now(kind)
