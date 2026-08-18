#!/usr/bin/env python3
"""Drain any stale messages queued on the DFR bulk IN endpoint.
appletbdrm's probe reads a fixed 65 bytes and dies on anything else, so a
leftover response from a previous session makes every other bind fail."""
import ctypes, fcntl, glob, os, struct, sys
def IOC(d,t,nr,sz): return (d<<30)|(sz<<16)|(ord(t)<<8)|nr
BULK, CLAIM, RELEASE = IOC(3,'U',2,24), IOC(2,'U',15,4), IOC(2,'U',16,4)
EP_IN, IFNUM = 0x85, 3
for d in glob.glob('/sys/bus/usb/devices/*/'):
    try:
        if open(d+'idVendor').read().strip()=='05ac' and open(d+'idProduct').read().strip()=='8600':
            bus, num = int(open(d+'busnum').read()), int(open(d+'devnum').read()); break
    except OSError: pass
else:
    sys.exit('iBridge not found')
fd = os.open(f'/dev/bus/usb/{bus:03d}/{num:03d}', os.O_RDWR)
fcntl.ioctl(fd, CLAIM, bytearray(struct.pack('I', IFNUM)))
drained = 0
while True:
    buf = bytearray(512)
    cbuf = (ctypes.c_char*512).from_buffer(buf)
    x = bytearray(struct.pack('IIIxxxxP', EP_IN, 512, 200, ctypes.addressof(cbuf)))
    try:
        n = fcntl.ioctl(fd, BULK, x)
    except OSError:
        break
    drained += 1
    print(f'  drained {n} bytes: {bytes(buf[:min(n,24)]).hex(" ")}')
    if drained > 20: break
fcntl.ioctl(fd, RELEASE, bytearray(struct.pack('I', IFNUM)))
os.close(fd)
print(f'drained {drained} stale message(s)')
