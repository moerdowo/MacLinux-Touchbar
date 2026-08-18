"""KMS output for the Apple T1 Touch Bar, driven by the in-tree appletbdrm.

appletbdrm reports the panel transposed -- a 60x2170 mode for a strip that is
physically 2170x60 -- and un-transposes when it pushes damage rectangles over
USB. Rather than make every caller think in sideways coordinates, this module
installs a cairo transform so drawing happens in strip space: x runs 0..2170
along the strip, y runs 0..60 across it.

The driver sets .fb_create = drm_gem_fb_create_with_dirty, so partial updates
work: flush() sends only the rectangle that changed instead of all 390 KB.
"""

import ctypes
import fcntl
import mmap
import os
import struct

import cairo

STRIP_W = 2170
STRIP_H = 60


def _iowr(nr, size):
    return (3 << 30) | (size << 16) | (ord('d') << 8) | nr


_GETRESOURCES = _iowr(0xA0, 64)
_GETCONNECTOR = _iowr(0xA7, 80)
_SETCRTC      = _iowr(0xA2, 104)
_ADDFB        = _iowr(0xAE, 28)
_CREATE_DUMB  = _iowr(0xB2, 32)
_DESTROY_DUMB = _iowr(0xB4, 8)
_MAP_DUMB     = _iowr(0xB3, 16)
_DIRTYFB      = _iowr(0xB1, 24)
_SET_MASTER   = (ord('d') << 8) | 0x1E
_DROP_MASTER  = (ord('d') << 8) | 0x1F


class TouchBarBusy(Exception):
    """Another process (usually the compositor) holds DRM master on the card."""


class TouchBar:
    def __init__(self, path='/dev/dri/card2'):
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(self.fd, _SET_MASTER)
        except OSError as exc:
            os.close(self.fd)
            raise TouchBarBusy(
                f'{path}: cannot become DRM master ({exc.strerror}). '
                'Hyprland grabs the Touch Bar unless AQ_DRM_DEVICES excludes it.'
            ) from exc

        self._crtc_id, self._conn_id, self._mode = self._find_output()
        self._buf_w = struct.unpack_from('<H', self._mode, 4)[0]    # hdisplay
        self._buf_h = struct.unpack_from('<H', self._mode, 14)[0]   # vdisplay

        self._handle, self._pitch, self._size, self._fb_id = self._make_fb()
        offset = self._map_offset(self._handle)
        self._map = mmap.mmap(self.fd, self._size, mmap.MAP_SHARED,
                              mmap.PROT_WRITE | mmap.PROT_READ, offset=offset)

        self.surface = cairo.ImageSurface.create_for_data(
            self._map, cairo.FORMAT_RGB24, self._buf_w, self._buf_h, self._pitch)
        self.ctx = cairo.Context(self.surface)
        # buffer_x = STRIP_H - strip_y ; buffer_y = strip_x
        self.ctx.set_matrix(cairo.Matrix(0, 1, -1, 0, self._buf_w, 0))

        self.width, self.height = STRIP_W, STRIP_H
        self._modeset()

    # -- setup helpers -----------------------------------------------------

    def _ioc(self, op, buf):
        fcntl.ioctl(self.fd, op, buf)
        return buf

    def _find_output(self):
        res = bytearray(64)
        self._ioc(_GETRESOURCES, res)
        n_crtc, n_conn = struct.unpack_from('<II', res, 36)
        if not n_crtc or not n_conn:
            raise RuntimeError('appletbdrm exposed no CRTC/connector')
        crtcs = (ctypes.c_uint32 * n_crtc)()
        conns = (ctypes.c_uint32 * n_conn)()
        struct.pack_into('<QQ', res, 8, ctypes.addressof(crtcs), ctypes.addressof(conns))
        struct.pack_into('<I', res, 32, 0)     # count_fbs: no list wanted
        struct.pack_into('<I', res, 44, 0)     # count_encoders: pointer is NULL
        self._ioc(_GETRESOURCES, res)

        c = bytearray(80)
        struct.pack_into('<I', c, 48, conns[0])
        self._ioc(_GETCONNECTOR, c)
        n_modes = struct.unpack_from('<I', c, 32)[0]
        if not n_modes:
            raise RuntimeError('Touch Bar connector reported no modes')
        modes = ctypes.create_string_buffer(68 * n_modes)
        struct.pack_into('<Q', c, 8, ctypes.addressof(modes))
        struct.pack_into('<I', c, 36, 0)       # count_props
        struct.pack_into('<I', c, 40, 0)       # count_encoders
        self._ioc(_GETCONNECTOR, c)
        return crtcs[0], conns[0], modes.raw[:68]

    def _make_fb(self):
        d = bytearray(struct.pack('<IIIIIIQ', self._buf_h, self._buf_w, 32, 0, 0, 0, 0))
        self._ioc(_CREATE_DUMB, d)
        _, _, _, _, handle, pitch, size = struct.unpack('<IIIIIIQ', d)
        f = bytearray(struct.pack('<IIIIIII', 0, self._buf_w, self._buf_h,
                                  pitch, 32, 24, handle))
        self._ioc(_ADDFB, f)
        return handle, pitch, size, struct.unpack_from('<I', f, 0)[0]

    def _map_offset(self, handle):
        m = bytearray(struct.pack('<IIQ', handle, 0, 0))
        self._ioc(_MAP_DUMB, m)
        return struct.unpack_from('<Q', m, 8)[0]

    def _modeset(self):
        conns = (ctypes.c_uint32 * 1)(self._conn_id)
        crtc = bytearray(104)
        struct.pack_into('<QIIIIIII', crtc, 0, ctypes.addressof(conns), 1,
                         self._crtc_id, self._fb_id, 0, 0, 0, 1)
        crtc[36:36 + 68] = self._mode
        self._ioc(_SETCRTC, crtc)

    # -- drawing -----------------------------------------------------------

    def flush(self, x=0, y=0, w=None, h=None):
        """Push a strip-space rectangle to the panel. Defaults to everything."""
        self.surface.flush()
        w = STRIP_W if w is None else w
        h = STRIP_H if h is None else h
        x1, y1 = max(0, int(x)), max(0, int(y))
        x2, y2 = min(STRIP_W, int(x + w)), min(STRIP_H, int(y + h))
        if x2 <= x1 or y2 <= y1:
            return
        # strip rect -> buffer rect (the transform above, applied to corners)
        clip = struct.pack('<HHHH', STRIP_H - y2, x1, STRIP_H - y1, x2)
        cr = ctypes.create_string_buffer(clip, len(clip))
        cmd = bytearray(struct.pack('<IIIIQ', self._fb_id, 0, 0, 1,
                                    ctypes.addressof(cr)))
        self._ioc(_DIRTYFB, cmd)

    def clear(self, r=0.0, g=0.0, b=0.0):
        self.ctx.save()
        self.ctx.set_source_rgb(r, g, b)
        self.ctx.rectangle(0, 0, STRIP_W, STRIP_H)
        self.ctx.fill()
        self.ctx.restore()

    def close(self):
        try:
            self.clear()
            self.flush()
        except Exception:
            pass
        for op, arg in ((_DROP_MASTER, None),):
            try:
                fcntl.ioctl(self.fd, op) if arg is None else None
            except OSError:
                pass
        try:
            self._map.close()
        except Exception:
            pass
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
