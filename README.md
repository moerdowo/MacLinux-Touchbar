# Dfrd - MacLinux Touchbar

*The Apple T1 Touch Bar, and an app to design it.*

Turns the Touch Bar on a **MacBookPro13,3** (2016, T1/iBridge `05ac:8600`)
into a 2170×60 framebuffer you can put anything on — custom buttons, images,
and live widgets — plus a GTK4 designer that previews every change against the
same renderer the hardware uses.

```
dfr-editor        design it
dfrd              run it
dfrctl            drive it from scripts
```

The programs keep the short `dfr*` names throughout — `dfrd` is the daemon,
and `~/.config/dfrd/` is where its config lives.

![Four pages on the Touch Bar](docs/strip-pages.png)

*The four stock pages, captured from the running daemon with `dfrctl
screenshot`: launchers and a BTC ticker, the F-keys, media with a volume
slider, and system meters with Hyprland workspaces.*

## What you can put on the strip

| Category | Widgets |
|---|---|
| **Basics** | Button (icon, text, image, any key or command), Text, Image (PNG/JPEG/SVG), Spacer |
| **Live data** | KITT visualiser (audio spectrum), Crypto price (CoinGecko), Stock ticker (Yahoo Finance), Weather (Open-Meteo), Script output |
| **System** | CPU, Memory, Temperature, Disk, Network throughput, Battery |
| **Controls** | Volume slider, Brightness slider, Now playing (MPRIS), Workspaces (Hyprland) |
| **Navigation** | Page switcher, and a reserved Escape key |

Market and weather widgets need no API key. Anything not covered is a
**Script** widget: run a command on a timer and show its first line.

### KITT

The `kitt` page is the visualiser on its own, filling the strip.

It shows the spectrum of **what this machine is actually playing** — the
monitor of the default sink, so every application, post-volume, and nothing
from the microphone. It follows the default sink rather than a fixed device,
so moving output to headphones does not freeze it.

```console
$ dfrctl page kitt
```

Mirrored from the centre with bass in the middle, drawn as discrete red cells
with the unlit ones still visible — that grid is the point, and it is where
the name comes from. Escape keeps its usual block at the far left, lit in the
matrix's own colour, because in display mode Escape is drawn by dfrd or it
does not exist at all.

When nothing is playing, **When silent** decides what happens:

| | |
|---|---|
| `sweep` | the Knight Rider scan, easing at each turn (default) |
| `grid` | the unlit matrix, still visible, not moving |
| `dark` | nothing at all — the strip goes dark |

`dark` really does stop: the transition is pushed once to clear the strip and
then nothing is pushed, the widget stops asking to be repainted, and the feed
holds its snapshot identical so nothing wakes it. It costs no USB and no
drawing until a sound starts. The capture itself keeps running, because that
is what notices the sound.

| Property | Default | |
|---|---|---|
| Bands | 48 | mirrored, so 96 columns |
| Rows | 9 | growing from the middle row outward, or up from the floor |
| Colour | `#ff1e0a` | one hue; heat lifts it slightly, never to orange |
| When silent | sweep | `sweep`, `grid` or `dark` |
| Auto gain | on | see below |
| Frames per second | 30 | every frame is a USB transfer |

**Auto gain is on by default and matters more than it sounds.** A sink monitor
is *post-volume*. At 24% system volume — where this machine actually sits — a
track that sounds perfectly normal measures around −50 dBFS, and with a fixed
scale the whole display sits one cell above the floor. The gain rides the
signal to put the loudest band near the top, coming down fast and going up
slowly, and it is frozen while silent so a noise floor is never amplified into
a display of nothing.

The noise floor itself is *measured*, not assumed: an analog loopback never
reads a true zero, this machine idles near −60 dBFS, and how far above zero it
sits is a property of the hardware. It is tracked as a sliding minimum, and a
candidate louder than −44 dBFS is rejected rather than believed — otherwise
switching to this page while music is already playing teaches it that the
music is silence.

Two things this deliberately does not do. It does not use numpy: the FFT is
1024-point radix-2 in pure Python, measured at 1.7 ms, and a daemon that owns
the Escape key should not gain a binary dependency to save 5% of one core. And
it does not poll — PipeWire is asked once for a continuous stream and a reader
thread owns it, so leaving the page stops the capture the same way leaving a
crypto page stops the polling.

A tap can press keys (`CTRL+C`, `F5`, media keys), run a command, open a URL,
switch page, step volume or brightness, dispatch to Hyprland, or hand the
strip back to the firmware.

## The editor

![The layout editor](docs/editor-layout.png)

`dfr-editor` shows the strip at the top of the window. That preview is not a
mock-up: it calls the same `dfrwidgets` code the daemon draws the panel with,
against the same live feeds, so BTC, the clock and the battery are real while
you are still arranging things.

* **Click** a widget to select it, **drag** it to rearrange. The strip has
  three groups — left, centre, right — shown as zones while you drag.
* **Width** is fixed pixels; **Stretch** shares out whatever is left over.
* **Live** pushes each edit to the running daemon over the control socket, so
  the hardware changes under your fingers before anything is saved.
* Undo is Ctrl+Z, save is Ctrl+S, and Save is explicit — Live never writes.

Pages are switched by the page-switcher widget, by swiping across the strip,
or with `dfrctl page <id>`.

Every property panel is generated from the widget's own schema, so the fields
you see are exactly the ones that widget understands — nothing to look up.

| Theme | Settings |
|---|---|
| ![Theme editor](docs/editor-theme.png) | ![Settings](docs/editor-settings.png) |

Nine palettes and ten colour roles, all live-previewed. The Settings view is
mostly about the one thing this hardware makes dangerous: the Escape key.

## Install

```sh
git clone https://github.com/moerdowo/MacLinux-Touchbar
cd MacLinux-Touchbar
sudo ./install.sh          # /usr/local, udev rule, service unit (not enabled)
sudo ./install.sh --polkit # also the polkit action for mode switching
```

Then:

```sh
dfrctl status              # where everything stands, as JSON
sudo dfrctl mode display   # hand the strip to dfrd
sudo systemctl start dfrd  # or run ./dfrd directly to watch it
dfr-editor
```

Enabling `dfrd` at boot is left to you deliberately — see the warning below.

## ⚠ There is no Escape key in display mode

USB config 2 has **no keyboard HID interface at all**. Interface 2 is the
10-finger digitizer, interface 6 is the ambient light sensor plus the `0xff12`
display-control page. This machine has no physical function row, so in display
mode *nothing* sends Escape until `dfrd` draws a key and synthesises it over
uinput — the same thing macOS does.

Three things follow, all of them built in:

* An Escape key is **reserved on every page** by the daemon, not placed in the
  layout, so no amount of editing can remove it by accident. It can be turned
  off in Settings, which is the only way to lose it.
* The daemon **returns the strip to keyboard mode when it stops**, including
  on crash, via `ExecStopPost` in the service unit.
* A **power cycle always** returns the device to config 1. Nothing here can
  leave the machine permanently without Escape.

## How it works

The iBridge has three USB configurations. Linux boots into **config 1**, where
`apple-ibridge` gives you the four firmware layouts and nothing else. **Config
2** — which `apple-ibridge.c` calls *"Default iBridge Interfaces(OS X)"*, the
one macOS uses — exposes **interface 3 as USB class 0x10 (audio/video) with a
bulk IN/OUT pair**.

That is exactly what the in-tree `appletbdrm` driver binds. Its logic was never
T2-specific; only its ID table was. The T1 answers `GINF` with the **identical
65-byte struct** the T2 sends:

| field | value |
|---|---|
| width × height | 2170 × 60 |
| bits per pixel | 24 (BGR888) |
| bytes per row | 6510 |
| pixel format | `0x52474241` |
| physical size | 9.954″ × 0.275″ |

So no driver patch is needed — just teach the running driver the ID:

```sh
echo "05ac 8600 10" > /sys/bus/usb/drivers/appletbdrm/new_id
```

Three gotchas cost real time, all handled here:

* **Stale bulk messages.** The IN endpoint can hold leftovers from a previous
  session (a 16-byte echo of the request header, `02 00 12 15 …`).
  `appletbdrm`'s probe reads a fixed 65 bytes and fails on anything else, which
  makes every *other* bind fail with `Actual size (52)`. `dfrdrain.py` flushes
  it first.
* **logind gives the card away.** The DRM master on the strip is
  `systemd-logind`, not the compositor: aquamarine asks libseat, logind opens
  the node and takes master, and Hyprland merely holds the passed fds. Since
  logind only manages devices tagged onto a seat,
  `99-touchbar-drm-noseat.rules` untags it and the problem disappears — the
  card stops appearing as a disabled `USB-1` monitor and `dfrd` can modeset it.
* **`AQ_DRM_DEVICES` is not enough on its own.** aquamarine consults it in
  `scanGPUs()` at startup and never on hotplug, so a strip switched on
  mid-session is grabbed regardless. It is worth setting as a backstop, but the
  udev rule is what actually works.

## Contents

| file | purpose |
|---|---|
| `dfr-editor` | GTK4/libadwaita designer: preview, palette, properties, theme |
| `dfrd` | the daemon: pages, widgets, touch, uinput, control socket |
| `dfrctl` | CLI: status, mode, page, reload, screenshot, catalogues (JSON) |
| `touchbar-mode` | root helper: switch `keyboard` ⇄ `display`, or print JSON status |
| `dfrwidgets.py` | widget catalogue, layout solver, renderer — used by both |
| `dfrtheme.py` | palette, typography, drawing primitives, icon catalogue |
| `dfrfeeds.py` | threaded data sources: system, market, weather, MPRIS, audio, script |
| `dfractions.py` | what a tap does |
| `dfrconfig.py` | config schema, defaults, atomic save, repair-on-load |
| `dfripc.py` | the control socket protocol |
| `dfrsession.py` | reaching the user's session from a root daemon |
| `dfrdrm.py` | DRM dumb buffer, modeset, damage flush, wrapped in cairo |
| `dfrinput.py` | hidraw digitizer, uinput keyboard, keycode table |
| `dfrdrain.py` | flush stale messages off the DFR bulk endpoint |

`dfrdrm.py` hides the transposition. The driver reports the panel as a 60×2170
mode and rotates internally, so the module installs a cairo matrix and you draw
in strip coordinates: **x is 0…2170 along the strip, y is 0…60 across it.**

`appletbdrm` sets `.fb_create = drm_gem_fb_create_with_dirty`, so `flush(x, y,
w, h)` sends only the rectangle that changed rather than all 390 KB. The
daemon repaints only widgets whose inputs actually changed.

## Config

`~/.config/dfrd/config.json` — pages, each with `left`, `center` and `right`
lists of widgets, plus `theme` and `settings`. The editor writes it atomically
and keeps one `.bak`. It is meant to be readable and hand-editable:

```json
{
  "type": "crypto",
  "width": 190,
  "props": { "coin": "bitcoin", "symbol": "BTC", "sparkline": true }
}
```

Icons are stored by **name** (`"icon": "terminal"`), resolved from the
catalogue in `dfrtheme.py`, because a Nerd Font glyph in the Private Use Area
does not survive every editor and copy-paste on its way into a config file. You
can still paste an emoji straight in. `dfrctl icons` lists the catalogue.

Bad values are repaired on load rather than raising — a typo in a colour must
not take the Escape key down with it. `dfrctl validate` shows what was fixed.

## Adding a widget type

Subclass `Widget` in `dfrwidgets.py`, give it a `TYPE`, a `SCHEMA` and a
`draw_content`. The editor builds its entire property panel from `SCHEMA`, so
a new widget appears in the palette with **no GUI code at all**. Declare any
data it needs with `feeds()` and read it with `env.feed(...)`; the hub handles
polling, caching, staleness and retirement.

## Diagnostics

```sh
dfrd --headless          run everything but the panel, to design without
                         taking the strip out of keyboard mode
dfrd --test              orientation test pattern
dfrd --probe-touch       raw touch coordinates, to calibrate flips
dfrd --render out.png    render the current config without any hardware
dfrctl screenshot s.png  what the strip is showing right now
dfrctl widgets           the whole catalogue with property schemas, as JSON
```
