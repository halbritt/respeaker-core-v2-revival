import os, sys, termios, time, select
fd = os.open('/dev/ttyACM0', os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
a = termios.tcgetattr(fd); a[0]=0; a[1]=0; a[3]=0; a[2]=termios.CS8|termios.CREAD|termios.CLOCAL
termios.tcsetattr(fd, termios.TCSANOW, a)
cmd = sys.argv[1]; wait = float(sys.argv[2]) if len(sys.argv) > 2 else 4
os.write(fd, b'\n'); time.sleep(0.3)
os.write(fd, (cmd + '; echo __END__\n').encode())
buf = b''; end = time.time() + wait
while time.time() < end:
    r,_,_ = select.select([fd],[],[],0.3)
    if r:
        try: buf += os.read(fd, 65536)
        except BlockingIOError: pass
        if buf.count(b'__END__') >= 2: break
print(buf.decode(errors='replace'))
