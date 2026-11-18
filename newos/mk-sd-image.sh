#!/usr/bin/env bash
# Assemble a bootable SD image for the ReSpeaker Core v2:
#   Armbian trixie minimal rootfs + our 6.18 kernel/dtb, laid out for Seeed's eMMC u-boot,
#   which loads /boot/uEnv.txt, /boot/vmlinuz-$uname_r, /boot/dtb/$uname_r/$dtb and
#   /boot/initrd.img-$uname_r from ext4 partition 2 and passes root=PARTUUID=<part 2>.
#
# Run as root from newos/:  sudo WIFI_SSID=<network> ./mk-sd-image.sh [--armed] [--safety=bringup|release]
#   --armed                   write /boot/uEnv.txt (the board boots this card on next reset).
#                             Default writes /boot/uEnv.txt.disabled, so the eMMC system keeps booting.
#   --safety=bringup|release  boot safety net profile (or SAFETY_PROFILE env; default: bringup).
#   --bringup / --release     aliases for --safety=bringup / --safety=release.
# Secrets (wifi passphrase, password hash) are read at build time and never stored here.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ARMED=0
SAFETY_PROFILE=${SAFETY_PROFILE:-bringup}

usage() {
	cat <<'EOF'
Usage: sudo WIFI_SSID=<network> ./mk-sd-image.sh [OPTIONS]

Options:
  --armed                     Write /boot/uEnv.txt so the board boots this SD card
                              (default: write /boot/uEnv.txt.disabled)
  --safety=bringup|release    Boot safety net profile (default: bringup)
  --safety <bringup|release>  Same as --safety=...
  --bringup                   Alias for --safety=bringup
  --release                   Alias for --safety=release
  -h, --help                  Show this help and exit

Environment:
  SAFETY_PROFILE              Default profile if not set on CLI (bringup|release)
  WIFI_SSID                   Wi-Fi network name to configure (required to build)
  WIFI_PSK                    Wi-Fi passphrase (optional; reads NetworkManager profile)
  STT_SERVER                  Optional STT server URL for /etc/default/rsp-stt.local
EOF
}

parse_args() {
	while [[ $# -gt 0 ]]; do
		case "$1" in
			--armed)
				ARMED=1
				shift
				;;
			--bringup)
				SAFETY_PROFILE=bringup
				shift
				;;
			--release)
				SAFETY_PROFILE=release
				shift
				;;
			--safety=*)
				SAFETY_PROFILE=${1#*=}
				shift
				;;
			--safety)
				if [[ $# -lt 2 ]]; then
					echo "error: --safety requires an argument (bringup or release)" >&2
					exit 2
				fi
				SAFETY_PROFILE=$2
				shift 2
				;;
			-h|--help)
				usage
				exit 0
				;;
			--)
				shift
				if [[ $# -gt 0 ]]; then
					echo "error: unexpected positional argument: $1" >&2
					exit 2
				fi
				;;
			-*)
				echo "error: unknown option: $1" >&2
				usage >&2
				exit 2
				;;
			*)
				echo "error: unexpected positional argument: $1" >&2
				usage >&2
				exit 2
				;;
		esac
	done
}

parse_args "$@"

case "$SAFETY_PROFILE" in
	bringup|release) ;;
	*)
		echo "error: invalid SAFETY_PROFILE '$SAFETY_PROFILE' (expected bringup or release)" >&2
		exit 2
		;;
esac

if [[ "${RSP_PARSE_ONLY:-0}" == "1" ]]; then
	printf 'ARMED=%s\nSAFETY_PROFILE=%s\n' "$ARMED" "$SAFETY_PROFILE"
	exit 0
fi

OWNER=${SUDO_USER:?run with sudo from your own account}
DEBS=$HERE/armbian-build/output/debs
BASE_IMG=$(ls "$HERE"/armbian-build/output/images/*_minimal.img | head -1)
IMG_DEB=$(ls -t "$DEBS"/linux-image-current-rockchip_*.deb | head -1)
DTB_DEB=$(ls -t "$DEBS"/linux-dtb-current-rockchip_*.deb | head -1)
OUT=$HERE/out/respeaker-trixie-sd.img
SIZE_MB=2600
HOSTNAME_NEW=respeaker-next
# Wifi network to join: name in WIFI_SSID; the passphrase is read from this machine's
# NetworkManager profile for that network (or pass WIFI_PSK).
WIFI_SSID=${WIFI_SSID:?set WIFI_SSID to the wifi network the board should join}
WIFI_NM=/etc/NetworkManager/system-connections/$WIFI_SSID.nmconnection
SSH_PUB=$(cat "/home/$OWNER/.ssh/id_ed25519.pub")
# password hash of the current board's respeaker user, so both systems share one login
PW_HASH=$(sudo -u "$OWNER" ssh -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR respeaker \
	"sudo -n true 2>/dev/null; cat /etc/shadow 2>/dev/null" | awk -F: '$1=="respeaker"{print $2}' || true)
[[ -n "${PW_HASH:-}" ]] || PW_HASH='!'   # no hash available: key-only login

W=$(mktemp -d /tmp/rsp-img.XXXXXX)
LOOP_OUT=; LOOP_BASE=
cleanup() {
	set +e
	mountpoint -q "$W/root" && umount "$W/root"
	mountpoint -q "$W/base" && umount "$W/base"
	[[ -n "$LOOP_OUT" ]] && losetup -d "$LOOP_OUT"
	[[ -n "$LOOP_BASE" ]] && losetup -d "$LOOP_BASE"
	rm -rf "$W"
}
trap cleanup EXIT

mkdir -p "$HERE/out" "$W"/{root,base,img,dtb}
rm -f "$OUT"; truncate -s ${SIZE_MB}M "$OUT"
# same geometry as Seeed's image: FAT at 16 MiB (unused, keeps rootfs as partition 2)
sfdisk -q "$OUT" <<EOF
label: dos
start=32768, size=204800, type=c
start=237568, type=83
EOF
LOOP_OUT=$(losetup -fP --show "$OUT")
mkfs.vfat -n BOOT "${LOOP_OUT}p1" >/dev/null
# conservative ext4 features: the loader is U-Boot 2017.09
mkfs.ext4 -q -L ROOTFS -O ^64bit,^metadata_csum,^orphan_file,^metadata_csum_seed "${LOOP_OUT}p2"
mount "${LOOP_OUT}p2" "$W/root"

LOOP_BASE=$(losetup -fP --show -r "$BASE_IMG")
mount -o ro "${LOOP_BASE}p1" "$W/base"
rsync -aHAX --numeric-ids "$W/base/" "$W/root/"
umount "$W/base"; losetup -d "$LOOP_BASE"; LOOP_BASE=

R=$W/root
# --- kernel, modules, dtb -------------------------------------------------------------
(cd "$W/img" && ar x "$IMG_DEB" && tar xf data.tar*)
(cd "$W/dtb" && ar x "$DTB_DEB" && tar xf data.tar*)
KVER=$(basename "$W"/img/boot/vmlinuz-* | sed 's/^vmlinuz-//')
rm -rf "$R"/boot/* "$R"/usr/lib/modules/*
cp "$W"/img/boot/{vmlinuz,config,System.map}-"$KVER" "$R/boot/"
MODSRC=$W/img/lib/modules; [[ -d $MODSRC ]] || MODSRC=$W/img/usr/lib/modules
cp -a "$MODSRC/$KVER" "$R/usr/lib/modules/"
mkdir -p "$R/boot/dtb/$KVER"
# Device tree: compile port/rk3229-respeaker-v2.dts against the kernel tree's headers, so a
# dts fix does not need a kernel rebuild. Falls back to the dtb in the Armbian package.
KSRC=$(ls -d "$HERE"/armbian-build/cache/sources/linux-kernel-worktree/*rockchip__armhf 2>/dev/null | head -1)
if [[ -n "$KSRC" ]] && command -v dtc >/dev/null; then
	cpp -nostdinc -undef -D__DTS__ -x assembler-with-cpp -I "$KSRC/include" \
		-I "$KSRC/arch/arm/boot/dts/rockchip" -I "$KSRC/scripts/dtc/include-prefixes" \
		"$HERE/port/rk3229-respeaker-v2.dts" | dtc -q -@ -I dts -O dtb -o "$R/boot/dtb/$KVER/rk3229-respeaker-v2.dtb" -
	echo "dtb: compiled from port/rk3229-respeaker-v2.dts"
else
	cp "$W"/dtb/boot/dtb-"$KVER"/rk3229-respeaker-v2.dtb "$R/boot/dtb/$KVER/"
	echo "dtb: taken from the Armbian package (may predate port/rk3229-respeaker-v2.dts)" >&2
fi
# Bring-up initramfs (debug-initrd/init): static busybox, USB serial gadget and watchdog
# modules. It saves the previous boot's crash log from pstore, arms the watchdog, then
# switches to this root (rspboot on the command line). Boot-critical drivers are built in.
IR=$(mktemp -d -p "$W")
mkdir -p "$IR"/{bin,dev,proc,sys,sd,lib,pstore,cfg,ov}
install -m 755 "$HERE/debug-initrd/busybox-armhf" "$IR/bin/busybox"
install -m 755 "$HERE/debug-initrd/init" "$IR/init"
cp "$MODSRC/$KVER/kernel/drivers/usb/gadget/legacy/g_serial.ko" \
   "$MODSRC/$KVER/kernel/drivers/watchdog/dw_wdt.ko" "$IR/lib/"
(cd "$IR" && find . | cpio -o -H newc -R 0:0 --quiet | gzip -9 > "$R/boot/initrd.img-$KVER")
install -m 755 "$HERE/overlay/sbin/rsp-preinit" "$R/sbin/rsp-preinit"
mkdir -p "$R/boot-debug"
printf '%s\n' "$SAFETY_PROFILE" > "$R/etc/rsp-safety"

UENV=$R/boot/uEnv.txt; [[ $ARMED == 1 ]] || UENV=$R/boot/uEnv.txt.disabled
CMDLINE="rootwait rw earlycon console=ttyS2,115200n8 loglevel=7 panic=5 rspboot"
[[ "$SAFETY_PROFILE" == "release" ]] && CMDLINE="$CMDLINE rsp_safety=release"
cat > "$UENV" <<EOF
# Read by Seeed's u-boot (env import). Rename to uEnv.txt to boot this card.
uname_r=$KVER
dtb=rk3229-respeaker-v2.dtb
# keep the dtb and initrd clear of the 30 MiB decompressed kernel and the TEE at 0x68400000
fdt_addr_r=0x68000000
ramdisk_addr_r=0x6a000000
# panic=5: reboot after a panic so the initramfs can save the log and fall back to eMMC
# rspboot: initramfs continues into this root; rsp-preinit handles bring-up dead-man / watchdog handoff
cmdline=$CMDLINE
EOF

# --- system configuration (no chroot: plain file edits) --------------------------------
PARTUUID=$(blkid -s PARTUUID -o value "${LOOP_OUT}p2")
cat > "$R/etc/fstab" <<EOF
PARTUUID=$PARTUUID / ext4 defaults,noatime,errors=remount-ro 0 1
tmpfs /tmp tmpfs defaults,nosuid 0 0
EOF
echo "$HOSTNAME_NEW" > "$R/etc/hostname"
sed -i "s/rk322x-box/$HOSTNAME_NEW/g" "$R/etc/hosts"
rm -f "$R/root/.not_logged_in_yet"          # skip Armbian's interactive first-login wizard
sed -i 's|^root:[^:]*:|root:!:|' "$R/etc/shadow"

# user respeaker (uid 1000), key login, passwordless sudo on this development image
if ! grep -q '^respeaker:' "$R/etc/passwd"; then
	echo 'respeaker:x:1000:1000:ReSpeaker:/home/respeaker:/bin/bash' >> "$R/etc/passwd"
	echo 'respeaker:x:1000:' >> "$R/etc/group"
	echo "respeaker:$PW_HASH:20000:0:99999:7:::" >> "$R/etc/shadow"
	echo 'respeaker:!::' >> "$R/etc/gshadow"
	for g in sudo audio video dialout plugdev netdev systemd-journal adm; do
		grep -q "^$g:" "$R/etc/group" && sed -i -E "/^$g:/ s/:([^:]*)$/:\1,respeaker/; /^$g:/ s/:,/:/" "$R/etc/group"
	done
fi
mkdir -p "$R/home/respeaker/.ssh"
cp -a "$R/etc/skel/." "$R/home/respeaker/" 2>/dev/null || true
echo "$SSH_PUB" > "$R/home/respeaker/.ssh/authorized_keys"
chmod 700 "$R/home/respeaker/.ssh"; chmod 600 "$R/home/respeaker/.ssh/authorized_keys"
chown -R 1000:1000 "$R/home/respeaker"
echo 'respeaker ALL=(ALL) NOPASSWD:ALL' > "$R/etc/sudoers.d/respeaker"; chmod 440 "$R/etc/sudoers.d/respeaker"
mkdir -p "$R/etc/ssh/sshd_config.d"
printf 'PasswordAuthentication no\nPermitRootLogin no\n' > "$R/etc/ssh/sshd_config.d/10-respeaker.conf"

# wifi: wpa_supplicant@wlan0 + networkd. Not netplan: its generated config asks for 802.11w,
# which the AP6212 (BCM43430A0) firmware cannot do, and association is dropped on the IGTK.
PSK=${WIFI_PSK:-$(sed -n 's/^psk=//p' "$WIFI_NM")}
install -d "$R/etc/wpa_supplicant"
( umask 077; cat > "$R/etc/wpa_supplicant/wpa_supplicant-wlan0.conf" <<EOF
ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
country=US
pmf=0
network={
	ssid="$WIFI_SSID"
	psk="$PSK"
	key_mgmt=WPA-PSK
	ieee80211w=0
}
EOF
)
cat > "$R/etc/systemd/network/20-wlan0.network" <<EOF
[Match]
Name=wlan0

[Network]
DHCP=yes

[DHCPv4]
ClientIdentifier=mac
EOF
mkdir -p "$R/etc/systemd/system/multi-user.target.wants"
ln -sf /usr/lib/systemd/system/wpa_supplicant@.service \
	"$R/etc/systemd/system/multi-user.target.wants/wpa_supplicant@wlan0.service"

# cancels rsp-preinit's dead-man switch once wifi has an address and sshd runs (bringup only)
cat > "$R/etc/systemd/system/rsp-keep.service" <<'EOF'
[Unit]
Description=Tell the bring-up dead-man switch that this boot is reachable
After=ssh.service
Wants=ssh.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'case "$(grep -oE "(^|[[:space:]])rsp_safety=[^[:space:]]*" /proc/cmdline 2>/dev/null | sed "s/^[[:space:]]*rsp_safety=//" | tail -n 1)" in release) exit 0 ;; bringup) ;; "") case "$(sed -n "s/[[:space:]]*#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; /^[[:space:]]*$/d; p; q" /etc/rsp-safety 2>/dev/null)" in release) exit 0 ;; esac ;; esac; until ip -4 addr show wlan0 | grep -q inet && systemctl -q is-active ssh; do sleep 2; done; touch /run/rsp-keep'
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/rsp-keep.service "$R/etc/systemd/system/multi-user.target.wants/rsp-keep.service"

if [[ "$SAFETY_PROFILE" == "release" ]]; then
	mkdir -p "$R/etc/systemd/system.conf.d"
	cat > "$R/etc/systemd/system.conf.d/10-rsp-watchdog.conf" <<'EOF'
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
EOF
fi

# speech-to-text client, ODAS and tools
install -d "$R/opt/rsp-stt" "$R/opt/odas/bin" "$R/opt/odas/lib" "$R/usr/local/bin"
install -m 755 "$HERE/../board/rsp-stt.py" "$R/opt/rsp-stt/rsp-stt.py"
install -m 644 "$HERE/../board/odas-respeaker-core-v2.cfg" "$R/opt/rsp-stt/"
install -m 644 "$HERE/../board/rsp-stt.env" "$R/etc/default/rsp-stt"
install -m 644 "$HERE/../board/rsp-stt.service" "$R/etc/systemd/system/rsp-stt.service"
install -m 755 "$HERE/../board/rsp-record" "$HERE/../board/rsp-replay" "$HERE/../board/rsp-listen" \
	"$HERE/../board/rsp-leds" "$R/usr/local/bin/"
# LED ring: supply switch, write access for the service user, direction display
install -m 644 "$HERE/../board/rsp-leds.service" "$HERE/../board/rsp-led-power.service" "$R/etc/systemd/system/"
install -D -m 644 "$HERE/../board/60-rsp-leds.rules" "$R/etc/udev/rules.d/60-rsp-leds.rules"
install -m 644 "$HERE/../board/asound.conf" "$R/etc/asound.conf"   # default output: the speaker, with a software volume
for u in rsp-leds rsp-led-power; do
	ln -sf /etc/systemd/system/$u.service "$R/etc/systemd/system/multi-user.target.wants/$u.service"
done
# site settings (not in the repository): at least STT_SERVER=http://<whisper host>:8910/inference
[[ -n "${STT_SERVER:-}" ]] && echo "STT_SERVER=$STT_SERVER" > "$R/etc/default/rsp-stt.local"
ODAS=$HERE/cross/out/odas-stock      # built by cross/cross.sh ./build-odas.sh
if [[ -x $ODAS/bin/odaslive ]]; then
	install -m 755 "$ODAS/bin/odaslive" "$R/opt/odas/bin/"
	install -m 755 "$ODAS/lib/libodas.so" "$R/opt/odas/lib/"
else
	echo "warning: $ODAS not built; the image will have no odaslive (STT_SOURCE=odas will fail)" >&2
fi
# Silero VAD model for the speech gate (MIT, github.com/snakers4/silero-vad), pinned by hash
VAD_URL=https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx
VAD_SHA=2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f
VAD=$HERE/out/silero_vad.onnx
[[ -s $VAD ]] || curl -fsSL -o "$VAD" "$VAD_URL" || true
if echo "$VAD_SHA  $VAD" | sha256sum -c --status 2>/dev/null; then
	install -m 644 "$VAD" "$R/opt/rsp-stt/silero_vad.onnx"
else
	rm -f "$VAD"
	echo "warning: could not fetch the Silero VAD model; the speech gate will be off" >&2
fi
# keep a core file if odaslive crashes
mkdir -p "$R/etc/sysctl.d" "$R/etc/systemd/system/rsp-stt.service.d" "$R/etc/tmpfiles.d"
echo 'kernel.core_pattern=/var/tmp/odas-cores/core.%e.%p.%t' > "$R/etc/sysctl.d/60-odas-core.conf"
printf '[Service]\nLimitCORE=infinity\n' > "$R/etc/systemd/system/rsp-stt.service.d/coredump.conf"
echo 'd /var/tmp/odas-cores 0750 respeaker respeaker 14d' > "$R/etc/tmpfiles.d/odas-cores.conf"

# First boot with network: the packages the pipeline needs (no chroot at build time), the
# mic gain, then enable the service. Runs until it has succeeded once.
cat > "$R/usr/local/sbin/rsp-firstboot" <<'EOF'
#!/bin/sh
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends python3-numpy python3-onnxruntime gpiod alsa-utils libfftw3-single3 libconfig11 libpulse0 libasound2t64
for i in 1 2 3 4 5 6; do amixer -q -c 0 sset "ADC$i PGA gain" 24; done
# the software Speaker control appears at its maximum (+12 dB) on first use: create it
# with half a second of silence, then set a moderate level before anything audible plays
aplay -q -D speaker -f S16_LE -r 16000 -c 2 -d 1 /dev/zero 2>/dev/null || true
amixer -q -c 0 sset Speaker -- 6dB || true
alsactl store
systemctl enable --now rsp-stt.service
touch /var/lib/rsp-firstboot.done
EOF
chmod 755 "$R/usr/local/sbin/rsp-firstboot"
cat > "$R/etc/systemd/system/rsp-firstboot.service" <<'EOF'
[Unit]
Description=ReSpeaker first-boot setup (packages, mic gain, enable rsp-stt)
After=network-online.target sound.target
Wants=network-online.target
ConditionPathExists=!/var/lib/rsp-firstboot.done

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/rsp-firstboot
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/rsp-firstboot.service "$R/etc/systemd/system/multi-user.target.wants/rsp-firstboot.service"

# login console on the micro-USB "OTG" port (shows up on the PC as /dev/ttyACM0)
echo g_serial > "$R/etc/modules-load.d/g_serial.conf"
mkdir -p "$R/etc/systemd/system/getty.target.wants"
ln -sf /usr/lib/systemd/system/serial-getty@.service \
	"$R/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service"
# The gadget link set up by the initramfs does not survive into the full system (the host
# still shows the device, but nothing passes): re-attach it once before the login starts.
cat > "$R/etc/systemd/system/rsp-usb-console.service" <<'EOF'
[Unit]
Description=Re-attach the USB serial gadget after the initramfs
ConditionPathIsDirectory=/sys/class/udc
Before=serial-getty@ttyGS0.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c "for u in /sys/class/udc/*; do echo disconnect > $$u/soft_connect; sleep 1; echo connect > $$u/soft_connect; done"

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/rsp-usb-console.service "$R/etc/systemd/system/multi-user.target.wants/rsp-usb-console.service"
# Armbian logs root in without a password on every serial console; ask for a login instead.
rm -f "$R/etc/systemd/system/serial-getty@.service.d/override.conf"

sync
umount "$R"; losetup -d "$LOOP_OUT"; LOOP_OUT=
chown "$OWNER": "$OUT"
echo "built $OUT (kernel $KVER, armed=$ARMED, safety=$SAFETY_PROFILE, root PARTUUID=$PARTUUID)"
