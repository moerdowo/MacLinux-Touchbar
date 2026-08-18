"""Touch input and synthetic keys for the Apple T1 Touch Bar.

Two halves:

* Touch -- in USB config 2 the T1 exposes interface 2 as a 10-finger digitizer.
  Its report has no report ID and is a flat array of ten 4-byte finger records:

      byte 0 : bits 0-3 transducer index, bit 4 touch, bit 5 in-range
      byte 1-2 : X, little endian, 0..32767
      byte 3 : Y, 0..127

  followed by a 12-byte vendor block (timestamp) we ignore. Reading hidraw
  directly beats evdev here because hid-generic's mapping of this descriptor is
  not something we want to depend on.

* Keys -- config 2 has NO keyboard interface at all, so a machine with no
  physical function row also has no Escape key. Every key the strip "sends" has
  to be synthesised, exactly as macOS does it. That is what the uinput half is
  for, and why the daemon must never exit leaving the panel blank.
"""

import ctypes
import fcntl
import glob
import os
import struct

FINGERS = 10
FINGER_BYTES = 4
TOUCH_REPORT_LEN = FINGERS * FINGER_BYTES

_X_MAX = 32767
_Y_MAX = 127

# --- keys ---------------------------------------------------------------

KEY_ESC = 1
KEY_F = [59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88]   # F1..F12
KEY_MUTE, KEY_VOLUMEDOWN, KEY_VOLUMEUP = 113, 114, 115
KEY_NEXTSONG, KEY_PLAYPAUSE, KEY_PREVIOUSSONG = 163, 164, 165
KEY_BRIGHTNESSDOWN, KEY_BRIGHTNESSUP = 224, 225

_EV_SYN, _EV_KEY = 0x00, 0x01
_SYN_REPORT = 0

_UI_DEV_CREATE  = (ord('U') << 8) | 1
_UI_DEV_DESTROY = (ord('U') << 8) | 2
_UI_DEV_SETUP   = (1 << 30) | (92 << 16) | (ord('U') << 8) | 3
_UI_SET_EVBIT   = (1 << 30) | (4 << 16) | (ord('U') << 8) | 100
_UI_SET_KEYBIT  = (1 << 30) | (4 << 16) | (ord('U') << 8) | 101


class VirtualKeyboard:
    """A uinput keyboard that stands in for the keys the Touch Bar draws."""

    def __init__(self, keys=None, name='Touch Bar'):
        # Every key the table knows is declared at creation: uinput fixes the
        # capability set when the device is created, and the user may bind a
        # new key in the editor long after this daemon started.
        keys = all_keycodes() if keys is None else keys
        self.fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, _UI_SET_EVBIT, _EV_KEY)
        for k in sorted(set(keys)):
            fcntl.ioctl(self.fd, _UI_SET_KEYBIT, k)
        # struct uinput_setup: input_id{bus,vendor,product,version} + name[80] + ff
        setup = struct.pack('<HHHH80sI', 0x03, 0x05AC, 0x8600, 1,
                            name.encode()[:79], 0)
        fcntl.ioctl(self.fd, _UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, _UI_DEV_CREATE)

    def _emit(self, type_, code, value):
        os.write(self.fd, struct.pack('<qqHHi', 0, 0, type_, code, value))

    def tap(self, keycode):
        self._emit(_EV_KEY, keycode, 1)
        self._emit(_EV_SYN, _SYN_REPORT, 0)
        self._emit(_EV_KEY, keycode, 0)
        self._emit(_EV_SYN, _SYN_REPORT, 0)

    def combo(self, codes):
        """Press codes in order, release in reverse -- Ctrl+C, Super+L, …"""
        codes = [c for c in codes if c]
        if not codes:
            return
        for code in codes:
            self._emit(_EV_KEY, code, 1)
            self._emit(_EV_SYN, _SYN_REPORT, 0)
        for code in reversed(codes):
            self._emit(_EV_KEY, code, 0)
            self._emit(_EV_SYN, _SYN_REPORT, 0)

    def close(self):
        try:
            fcntl.ioctl(self.fd, _UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)


# --- key names ----------------------------------------------------------

#: name -> Linux input event code (linux/input-event-codes.h). Actions name
#: keys as text so a config file stays readable and editable by hand.
KEYS = {
    'ESC': 1, 'MINUS': 12, 'EQUAL': 13, 'BACKSPACE': 14, 'TAB': 15,
    'LEFTBRACE': 26, 'RIGHTBRACE': 27, 'ENTER': 28, 'SEMICOLON': 39,
    'APOSTROPHE': 40, 'GRAVE': 41, 'BACKSLASH': 43, 'COMMA': 51, 'DOT': 52,
    'SLASH': 53, 'SPACE': 57, 'CAPSLOCK': 58,
    'CTRL': 29, 'LEFTCTRL': 29, 'RIGHTCTRL': 97,
    'SHIFT': 42, 'LEFTSHIFT': 42, 'RIGHTSHIFT': 54,
    'ALT': 56, 'LEFTALT': 56, 'RIGHTALT': 100, 'ALTGR': 100,
    'SUPER': 125, 'META': 125, 'LEFTMETA': 125, 'RIGHTMETA': 126, 'CMD': 125,
    'COMPOSE': 127, 'MENU': 139,
    'HOME': 102, 'UP': 103, 'PAGEUP': 104, 'LEFT': 105, 'RIGHT': 106,
    'END': 107, 'DOWN': 108, 'PAGEDOWN': 109, 'INSERT': 110, 'DELETE': 111,
    'MUTE': 113, 'VOLUMEDOWN': 114, 'VOLUMEUP': 115, 'POWER': 116,
    'PAUSE': 119, 'STOP': 128, 'AGAIN': 129, 'UNDO': 131, 'COPY': 133,
    'OPEN': 134, 'PASTE': 135, 'FIND': 136, 'CUT': 137, 'HELP': 138,
    'CALC': 140, 'SLEEP': 142, 'WWW': 150, 'SCREENLOCK': 152,
    'BACK': 158, 'FORWARD': 159, 'EJECTCD': 161,
    'NEXTSONG': 163, 'PLAYPAUSE': 164, 'PREVIOUSSONG': 165, 'STOPCD': 166,
    'REFRESH': 173, 'SEARCH': 217,
    'BRIGHTNESSDOWN': 224, 'BRIGHTNESSUP': 225, 'SWITCHVIDEOMODE': 227,
    'KBDILLUMTOGGLE': 228, 'KBDILLUMDOWN': 229, 'KBDILLUMUP': 230,
    'PRINT': 210, 'SYSRQ': 99, 'SCROLLLOCK': 70, 'NUMLOCK': 69,
}
for _i, _name in enumerate('1234567890'):
    KEYS[_name] = 2 + _i
for _i, _name in enumerate('QWERTYUIOP'):
    KEYS[_name] = 16 + _i
for _i, _name in enumerate('ASDFGHJKL'):
    KEYS[_name] = 30 + _i
for _i, _name in enumerate('ZXCVBNM'):
    KEYS[_name] = 44 + _i
for _i in range(12):
    KEYS[f'F{_i + 1}'] = KEY_F[_i]
for _i in range(12):
    KEYS[f'F{_i + 13}'] = 183 + _i          # F13..F24

#: Grouped for the editor's key picker.
KEY_GROUPS = [
    ('Function', [f'F{i}' for i in range(1, 25)]),
    ('Modifiers', ['CTRL', 'SHIFT', 'ALT', 'SUPER', 'MENU', 'CAPSLOCK']),
    ('Editing', ['ESC', 'TAB', 'ENTER', 'SPACE', 'BACKSPACE', 'DELETE',
                 'INSERT', 'UNDO', 'COPY', 'PASTE', 'CUT', 'FIND']),
    ('Navigation', ['UP', 'DOWN', 'LEFT', 'RIGHT', 'HOME', 'END',
                    'PAGEUP', 'PAGEDOWN', 'BACK', 'FORWARD']),
    ('Media', ['PLAYPAUSE', 'NEXTSONG', 'PREVIOUSSONG', 'STOPCD',
               'MUTE', 'VOLUMEUP', 'VOLUMEDOWN', 'EJECTCD']),
    ('Display', ['BRIGHTNESSUP', 'BRIGHTNESSDOWN', 'SWITCHVIDEOMODE',
                 'KBDILLUMUP', 'KBDILLUMDOWN', 'KBDILLUMTOGGLE']),
    ('System', ['POWER', 'SLEEP', 'SCREENLOCK', 'PRINT', 'SYSRQ', 'CALC',
                'WWW', 'SEARCH', 'REFRESH']),
    ('Letters', list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')),
    ('Digits', list('1234567890')),
]


def keycode(name):
    """'ctrl', 'F5', 'KEY_ESC', 'a' -> event code, or None if unknown."""
    if isinstance(name, int):
        return name
    if not name:
        return None
    key = str(name).strip().upper().replace('KEY_', '').replace(' ', '')
    return KEYS.get(key)


def all_keycodes():
    return sorted(set(KEYS.values()))


# --- touch --------------------------------------------------------------

_HIDIOCGRDESCSIZE = (2 << 30) | (4 << 16) | (ord('H') << 8) | 0x01


def find_digitizer():
    """Return the hidraw path of the Touch Bar digitizer, or None.

    Identified structurally rather than by name: the digitizer is the iBridge
    HID interface whose report descriptor opens with a Digitizer/Touch Pad
    application collection (05 0d 09 05 a1 01).
    """
    for hidraw in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        dev = os.path.join(hidraw, 'device')
        try:
            uevent = open(os.path.join(dev, 'uevent')).read()
        except OSError:
            continue
        if '05AC:8600' not in uevent.upper():
            continue
        try:
            rdesc = open(os.path.join(dev, 'report_descriptor'), 'rb').read(6)
        except OSError:
            continue
        if rdesc[:6] == bytes((0x05, 0x0D, 0x09, 0x05, 0xA1, 0x01)):
            return '/dev/' + os.path.basename(hidraw)
    return None


class Digitizer:
    """Reads finger positions in strip coordinates (0..2170, 0..60)."""

    def __init__(self, path, strip_w, strip_h, flip_x=False, flip_y=False):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.strip_w, self.strip_h = strip_w, strip_h
        self.flip_x, self.flip_y = flip_x, flip_y

    def fileno(self):
        return self.fd

    def read(self):
        """Return a list of (x, y) for fingers currently touching."""
        try:
            data = os.read(self.fd, 256)
        except BlockingIOError:
            return None
        except OSError:
            return None
        if len(data) < TOUCH_REPORT_LEN:
            return None
        out = []
        for i in range(FINGERS):
            flags, raw_x, raw_y = struct.unpack_from('<BHB', data, i * FINGER_BYTES)
            if not (flags >> 4) & 1:          # touch bit
                continue
            fx = raw_x / _X_MAX
            fy = raw_y / _Y_MAX
            if self.flip_x:
                fx = 1.0 - fx
            if self.flip_y:
                fy = 1.0 - fy
            out.append((fx * self.strip_w, fy * self.strip_h))
        return out

    def close(self):
        os.close(self.fd)
