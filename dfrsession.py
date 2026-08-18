"""Reaching the user's graphical session from a root daemon.

dfrd runs as root because it owns a DRM node and /dev/uinput, but almost
everything it *does* on a tap belongs to the user: launch a terminal, set the
volume, dispatch to Hyprland, read the now-playing track. This module is the
one place that crosses that boundary, so the environment is assembled
correctly in exactly one spot.

The signature trap, learned the hard way on this machine: Hyprland `setenv`s
HYPRLAND_INSTANCE_SIGNATURE *after* exec, so it is absent from
/proc/<pid>/environ. Reading it from there silently yields an empty string and
every hyprctl call fails with no error worth the name. It has to come from the
runtime directory instead -- the newest instance that still holds a lock.
"""

import glob
import os
import pwd
import shutil
import subprocess

_TIMEOUT = 6


class Session:
    """The target user's session: uid, runtime dir, bus, Hyprland instance."""

    def __init__(self, user):
        self.user = user if not isinstance(user, str) else pwd.getpwnam(user)
        self.uid = self.user.pw_uid
        self.gid = self.user.pw_gid
        self.name = self.user.pw_name
        self.home = self.user.pw_dir
        self.runtime_dir = f'/run/user/{self.uid}'

    # -- environment ----------------------------------------------------

    @property
    def bus_address(self):
        path = os.path.join(self.runtime_dir, 'bus')
        return f'unix:path={path}' if os.path.exists(path) else None

    def hypr_signature(self):
        """Newest Hyprland instance dir that still has a lock file, or None."""
        best = None
        for d in glob.glob(os.path.join(self.runtime_dir, 'hypr', '*/')):
            if not os.path.exists(os.path.join(d, 'hyprland.lock')):
                continue
            mtime = os.path.getmtime(d)
            if best is None or mtime > best[0]:
                best = (mtime, os.path.basename(d.rstrip('/')))
        return best[1] if best else None

    def wayland_display(self):
        for candidate in sorted(glob.glob(os.path.join(self.runtime_dir, 'wayland-*'))):
            if not candidate.endswith('.lock'):
                return os.path.basename(candidate)
        return 'wayland-1'

    def env(self, extra=None):
        env = {
            'HOME': self.home,
            'USER': self.name,
            'LOGNAME': self.name,
            'SHELL': self.user.pw_shell or '/bin/sh',
            'PATH': '/usr/local/bin:/usr/bin:/bin',
            'XDG_RUNTIME_DIR': self.runtime_dir,
            'XDG_SESSION_TYPE': 'wayland',
            'WAYLAND_DISPLAY': self.wayland_display(),
            'LANG': os.environ.get('LANG', 'C.UTF-8'),
        }
        bus = self.bus_address
        if bus:
            env['DBUS_SESSION_BUS_ADDRESS'] = bus
        sig = self.hypr_signature()
        if sig:
            env['HYPRLAND_INSTANCE_SIGNATURE'] = sig
        env.update(extra or {})
        return env

    # -- running things -------------------------------------------------

    def _demote(self):
        uid, gid, name = self.uid, self.gid, self.name

        def preexec():
            os.setgid(gid)
            os.initgroups(name, gid)
            os.setuid(uid)
            os.setsid()
        return preexec

    def run(self, argv, *, capture=True, timeout=_TIMEOUT, shell=False):
        """Run as the user. Returns (rc, stdout, stderr); never raises."""
        kwargs = {
            'env': self.env(),
            'cwd': self.home,
            'timeout': timeout,
            'shell': shell,
        }
        if os.getuid() == 0:
            kwargs['preexec_fn'] = self._demote()
        if capture:
            kwargs['capture_output'] = True
            kwargs['text'] = True
        else:
            kwargs['stdout'] = subprocess.DEVNULL
            kwargs['stderr'] = subprocess.DEVNULL
        try:
            proc = subprocess.run(argv, **kwargs)
            return proc.returncode, (proc.stdout or '') if capture else '', \
                   (proc.stderr or '') if capture else ''
        except subprocess.TimeoutExpired:
            return 124, '', 'timeout'
        except (OSError, ValueError) as exc:
            return 127, '', str(exc)

    def spawn(self, command):
        """Fire-and-forget a shell command as the user (app launching)."""
        kwargs = {
            'env': self.env(),
            'cwd': self.home,
            'shell': True,
            'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL,
            'stdin': subprocess.DEVNULL,
            'start_new_session': True,
        }
        if os.getuid() == 0:
            kwargs['preexec_fn'] = self._demote()
        try:
            subprocess.Popen(command, **kwargs)
            return True, ''
        except (OSError, ValueError) as exc:
            return False, str(exc)

    def hyprctl(self, *args, json_out=False):
        if not shutil.which('hyprctl'):
            return 127, '', 'hyprctl not installed'
        argv = ['hyprctl'] + (['-j'] if json_out else []) + list(args)
        return self.run(argv)

    def notify(self, summary, body=''):
        if shutil.which('notify-send'):
            self.run(['notify-send', '-a', 'dfrd', summary, body], capture=False)
