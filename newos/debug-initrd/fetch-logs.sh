#!/bin/sh
# Copy /boot-debug from the SD card (via the eMMC system) into logs/ and summarise the newest boots.
R=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd); N=$R/newos; D=$N/debug-initrd/logs
PW=$(sed -n 's/^password: //p' $R/board-credentials.txt)
for t in $(seq 1 15); do ssh -o LogLevel=ERROR -o ConnectTimeout=20 respeaker true 2>/dev/null && break; /usr/bin/sleep 5; done
ssh -o LogLevel=ERROR respeaker "printf '%s\n' '$PW' | sudo -S -p '' sh -c '
mountpoint -q /mnt/sd || mount /dev/mmcblk0p2 /mnt/sd
tar -C /mnt/sd/boot-debug -cf /tmp/bd.tar . ; chmod 644 /tmp/bd.tar; cat /mnt/sd/boot/stages'"
scp -q -o LogLevel=ERROR respeaker:/tmp/bd.tar $D/ && tar -C $D -xf $D/bd.tar && rm $D/bd.tar
cd $D; for b in $(ls -d boot-* | sort -V | tail -${1:-4}); do echo "== $b: $(cat $b/dtb.txt 2>/dev/null) | $(cat $b/alive.txt) | $(ls $b/pstore 2>/dev/null | tr '\n' ' ')"; done
