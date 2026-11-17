#!/usr/bin/env python3
"""Append everything the board's USB gadget console prints to a log, across reboots."""
import os, sys, termios, time, select
out = open(sys.argv[1], 'ab', buffering=0)
while True:
    try:
        fd = os.open('/dev/ttyACM0', os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(fd); a[0] = 0; a[1] = 0; a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        termios.tcsetattr(fd, termios.TCSANOW, a)
        out.write(b'\n##### opened %s\n' % time.strftime('%H:%M:%S').encode())
        while True:
            r, _, _ = select.select([fd], [], [], 1)
            if r:
                d = os.read(fd, 65536)
                if not d: break
                out.write(d)
    except OSError:
        pass
    try: os.close(fd)
    except Exception: pass
    time.sleep(0.2)
