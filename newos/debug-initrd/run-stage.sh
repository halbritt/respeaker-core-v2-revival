#!/bin/sh
# run-stage.sh <dtb> [extra cmdline]: put a dtb on the SD card, verify the boot files read back,
# boot it, wait for the board to return to the eMMC system, then fetch logs.
R=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd); N=$R/newos; K=6.18.63-current-rockchip; cd $N/debug-initrd
dtb=$1; shift; extra="$*"
PW=$(sed -n 's/^password: //p' $R/board-credentials.txt)
for t in $(seq 1 15); do ssh -o LogLevel=ERROR -o ConnectTimeout=20 respeaker true 2>/dev/null && break; /usr/bin/sleep 5; done
scp -q -o LogLevel=ERROR $dtb initrd-debug.img respeaker:/tmp/ || exit 1
ssh -o LogLevel=ERROR respeaker "printf '%s\n' '$PW' | sudo -S -p '' sh -c '
set -e
mountpoint -q /mnt/sd || mount /dev/mmcblk0p2 /mnt/sd
cp /tmp/$dtb /mnt/sd/boot/dtb/$K/; cp /tmp/initrd-debug.img /mnt/sd/boot/initrd.img-$K; : > /mnt/sd/boot/stages
[ -e /mnt/sd/boot/uEnv.txt ] || mv /mnt/sd/boot/uEnv.txt.done /mnt/sd/boot/uEnv.txt
sed -i -e \"s|^dtb=.*|dtb=$dtb|\" -e \"s|^cmdline=.*|cmdline=rootwait rw earlycon console=ttyS2,115200n8 loglevel=7 printk.devkmsg=on log_buf_len=4M panic=5 $extra|\" /mnt/sd/boot/uEnv.txt
sync; umount /mnt/sd; echo 3 > /proc/sys/vm/drop_caches; mount -o ro /dev/mmcblk0p2 /mnt/sd
md5sum /mnt/sd/boot/initrd.img-$K /mnt/sd/boot/dtb/$K/$dtb /mnt/sd/boot/vmlinuz-$K | cut -c1-32 | tr \"\\n\" \" \"; echo; grep -E \"^(dtb|cmdline)\" /mnt/sd/boot/uEnv.txt
umount /mnt/sd'" > /tmp/rs.$$ || { cat /tmp/rs.$$; exit 1; }
got=$(head -1 /tmp/rs.$$); tail -n +2 /tmp/rs.$$; rm -f /tmp/rs.$$
exp="$(md5sum < initrd-debug.img | cut -c1-32) $(md5sum < $dtb | cut -c1-32) 24a868c6673b5d62e6fa70d5ff157880 "
[ "$got" = "$exp" ] && echo "card readback OK" || { echo "CARD READBACK MISMATCH: $got"; exit 1; }
ssh -o LogLevel=ERROR respeaker "printf '%s\n' '$PW' | sudo -S -p '' sh -c '(sleep 2; reboot) >/dev/null 2>&1 &'"
/usr/bin/sleep 25
n0=$(sudo dmesg | grep -c 'Product: ReSpekaer'); s=$(date +%s); back=0
while [ $(( $(date +%s) - s )) -lt ${WAIT:-330} ]; do [ "$(sudo dmesg | grep -c 'Product: ReSpekaer')" != "$n0" ] && { echo "old system back after $(( $(date +%s) - s + 25 ))s"; back=1; break; }; /usr/bin/sleep 3; done
[ $back = 1 ] || { echo "board did NOT come back"; exit 2; }
/usr/bin/sleep 15; ./fetch-logs.sh 3
