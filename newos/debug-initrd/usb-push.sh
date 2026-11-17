# push.sh <local> <remote>: copy a small file to the board through the USB shell
f=$1; r=$2; G=$(dirname $0)/usb-shell.py
python3 $G ": > $r" 3 >/dev/null
python3 - "$f" <<'PY' | while read -r chunk; do python3 $G "printf '$chunk' >> $r" 3 >/dev/null; done
import sys
d=open(sys.argv[1],'rb').read()
for i in range(0,len(d),150): print(''.join('\\x%02x'%b for b in d[i:i+150]))
PY
python3 $G "md5sum $r" 4 | grep -a "  $r" | tail -1; md5sum $f
