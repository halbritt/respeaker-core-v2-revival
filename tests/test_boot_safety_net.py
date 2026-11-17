"""F6: Boot Safety Net (`newos/mk-sd-image.sh`, `rsp-preinit`, `debug-initrd/init`) tests."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from conftest import BUSYBOX_HOST, NEWOS_DIR, chroot_prereq_missing, run_bounded

MK_SD_IMAGE = NEWOS_DIR / "mk-sd-image.sh"
RSP_PREINIT = NEWOS_DIR / "overlay" / "sbin" / "rsp-preinit"
DEBUG_INIT = NEWOS_DIR / "debug-initrd" / "init"

# The reason names what this host lacks (user namespaces, the static busybox, or both).
pytestmark_unshare = pytest.mark.skipif(bool(chroot_prereq_missing()), reason=chroot_prereq_missing())


def _init_chroot_rootfs(root: Path) -> None:
    """Create a disposable rootfs in `root` with static busybox and mock system binaries."""
    for d in ("bin", "sbin", "etc", "proc", "sys", "dev", "run", "boot", "pstore", "sd", "lib", "cfg", "ov"):
        (root / d).mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUSYBOX_HOST, root / "bin" / "busybox")
    (root / "bin" / "busybox").chmod(0o755)

    (root / "proc" / "uptime").write_text("12.34 0.00\n", encoding="utf-8")
    (root / "proc" / "cmdline").write_text("rootwait rspboot\n", encoding="utf-8")
    (root / "proc" / "sysrq-trigger").write_text("", encoding="utf-8")
    (root / "proc" / "device-tree").mkdir(parents=True, exist_ok=True)
    (root / "proc" / "device-tree" / "model").write_text("Seeed ReSpeaker Core v2.0\n", encoding="utf-8")
    (root / "dev" / "kmsg").write_text("", encoding="utf-8")
    (root / "dev" / "null").write_text("", encoding="utf-8")

    # Mock system commands inside /bin and /sbin before busybox --install -s /bin
    mocks = {
        "bin/dmesg": "#!/bin/sh\necho '[    0.000000] Booting Linux'\n",
        "bin/sync": "#!/bin/sh\nexit 0\n",
        "bin/sleep": "#!/bin/sh\nexit 0\n",
        "bin/journalctl": "#!/bin/sh\necho 'mock journal line'\n",
        "bin/mount": "#!/bin/sh\nexit 0\n",
        "bin/umount": "#!/bin/sh\nexit 0\n",
        "bin/insmod": "#!/bin/sh\nexit 0\n",
        "bin/watchdog": "#!/bin/sh\nexit 0\n",
        "bin/switch_root": "#!/bin/sh\nprintf '%s\\n' \"$*\" >> /switch_root.log\nexit 0\n",
        "bin/reboot": "#!/bin/sh\nprintf '%s\\n' \"$*\" >> /reboot.log\nexit 0\n",
        "sbin/init": "#!/bin/sh\nprintf '%s\\n' \"$*\" > /init_args.txt\nexit 0\n",
    }
    for rel, body in mocks.items():
        p = root / rel
        p.unlink(missing_ok=True)
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)

    # Install busybox applets inside the chroot so /bin/sh -> /bin/busybox resolves inside chroot
    subprocess.run(
        ["unshare", "-Ur", "chroot", str(root), "/bin/busybox", "--install", "-s", "/bin"],
        check=True,
    )

    shutil.copy2(RSP_PREINIT, root / "sbin" / "rsp-preinit")
    (root / "sbin" / "rsp-preinit").chmod(0o755)
    shutil.copy2(DEBUG_INIT, root / "init")
    (root / "init").chmod(0o755)


def _run_chroot(root: Path, argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["unshare", "-Ur", "chroot", str(root), *argv],
        env={"PATH": "/bin:/sbin"},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# --- Tier 1: Happy-Path Boot Safety Net (6 tests) ---


def test_f6_t1_mk_sd_image_armed_and_safety_profile_flags() -> None:
    syntax = subprocess.run(["bash", "-n", str(MK_SD_IMAGE)], capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr

    txt = MK_SD_IMAGE.read_text(encoding="utf-8")
    assert "set -euo pipefail" in txt
    assert "trap cleanup EXIT" in txt
    assert "UENV=$R/boot/uEnv.txt; [[ $ARMED == 1 ]] || UENV=$R/boot/uEnv.txt.disabled" in txt
    assert "panic=5 rspboot" in txt

    help_res = subprocess.run(["bash", str(MK_SD_IMAGE), "--help"], capture_output=True, text=True, check=False)
    assert help_res.returncode == 0
    assert "--armed" in help_res.stdout

    assert "RSP_PARSE_ONLY" in txt
    for args, exp_armed, exp_prof in (
        ([], "0", "bringup"),
        (["--armed"], "1", "bringup"),
        (["--armed", "--release"], "1", "release"),
        (["--safety=release"], "0", "release"),
        (["--safety", "bringup"], "0", "bringup"),
    ):
        res = subprocess.run(
            ["bash", str(MK_SD_IMAGE), *args],
            env={**os.environ, "RSP_PARSE_ONLY": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stderr
        assert f"ARMED={exp_armed}" in res.stdout
        assert f"SAFETY_PROFILE={exp_prof}" in res.stdout


@pytestmark_unshare
def test_f6_t1_rsp_preinit_deadman_fires_and_disarms_uenv(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "proc" / "cmdline").write_text("rootwait rsp_deadman=2 rspboot\n", encoding="utf-8")
    (root / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    # Make mock /sbin/init wait briefly for background deadman loop to finish sysrq-trigger (last step)
    (root / "sbin" / "init").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" > /init_args.txt\n"
        "n=0\n"
        "while [ ! -s /proc/sysrq-trigger ]; do\n"
        "  [ $n -ge 400 ] && { echo 'timed out waiting for /proc/sysrq-trigger' >&2; exit 1; }\n"
        "  /bin/busybox usleep 50000\n"
        "  n=$((n+1))\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = _run_chroot(root, ["/sbin/rsp-preinit", "--unit", "multi-user.target"])
    assert res.returncode == 0, res.stderr
    assert (root / "init_args.txt").read_text(encoding="utf-8").strip() == "--unit multi-user.target"
    assert not (root / "boot" / "uEnv.txt").exists()
    assert (root / "boot" / "uEnv.txt.done").is_file()
    assert (root / "boot-debug" / "boot-0" / "dmesg-preinit.txt").is_file()
    assert (root / "boot-debug" / "boot-0" / "journal.txt").is_file()
    assert "deadman fired at 2" in (root / "boot-debug" / "boot-0" / "alive.txt").read_text(encoding="utf-8")
    assert "rsp-clean-reboot" in (root / "dev" / "kmsg").read_text(encoding="utf-8")
    assert (root / "proc" / "sysrq-trigger").read_text(encoding="utf-8").strip() == "b"


@pytestmark_unshare
def test_f6_t1_rsp_preinit_keep_marker_prevents_reboot(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "proc" / "cmdline").write_text("rootwait rsp_deadman=2 rspboot\n", encoding="utf-8")
    (root / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "run" / "rsp-keep").write_text("", encoding="utf-8")
    (root / "sbin" / "init").write_text(
        "#!/bin/sh\n"
        "n=0\n"
        "while ! grep -q '^899 ' /boot-debug/boot-0/alive.txt 2>/dev/null; do\n"
        "  if [ $n -ge 500 ]; then\n"
        "    echo 'timed out waiting for 899 in boot-0/alive.txt' >&2\n"
        "    cat /boot-debug/boot-0/alive.txt >&2 2>/dev/null\n"
        "    exit 1\n"
        "  fi\n"
        "  /bin/busybox usleep 50000\n"
        "  n=$((n+1))\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = _run_chroot(root, ["/sbin/rsp-preinit"])
    assert res.returncode == 0, res.stderr
    assert (root / "boot" / "uEnv.txt").is_file()
    assert not (root / "boot" / "uEnv.txt.done").exists()
    assert not (root / "boot-debug" / "boot-0" / "journal.txt").exists()
    assert (root / "proc" / "sysrq-trigger").read_text(encoding="utf-8") == ""
    assert (root / "boot-debug" / "boot-0" / "alive.txt").read_text(encoding="utf-8").startswith("899 ")


@pytestmark_unshare
def test_f6_t1_debug_initrd_clean_reboot_clears_pstore_and_switches_root(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root / "sd" / "sbin").mkdir(parents=True)
    (root / "sd" / "boot").mkdir(parents=True)
    (root / "sd" / "proc").mkdir()
    (root / "sd" / "sys").mkdir()
    (root / "sd" / "dev").mkdir()
    (root / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (root / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "pstore" / "console-ramoops-0").write_text("boot log\nrsp-clean-reboot\n", encoding="utf-8")

    res = _run_chroot(root, ["/init"])
    assert res.returncode == 0, res.stderr
    assert not list((root / "pstore").iterdir())
    assert (root / "sd" / "boot" / "uEnv.txt").is_file()
    assert (root / "sd" / "boot-debug" / "boot-0" / "dmesg.txt").is_file()
    assert "/sd /sbin/rsp-preinit" in (root / "switch_root.log").read_text(encoding="utf-8")


@pytestmark_unshare
def test_f6_t1_debug_initrd_crash_archives_pstore_and_disarms(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root / "sd" / "sbin").mkdir(parents=True)
    (root / "sd" / "boot").mkdir(parents=True)
    (root / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (root / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "pstore" / "console-ramoops-0").write_text("Kernel panic - not syncing: VFS\n", encoding="utf-8")

    res = _run_chroot(root, ["/init"])
    assert res.returncode == 0, res.stderr
    archived = root / "sd" / "boot-debug" / "boot-0" / "pstore" / "console-ramoops-0"
    assert archived.is_file() and "Kernel panic" in archived.read_text(encoding="utf-8")
    assert not (root / "sd" / "boot" / "uEnv.txt").exists()
    assert (root / "sd" / "boot" / "uEnv.txt.done").is_file()
    assert "rsp-clean-reboot" in (root / "dev" / "kmsg").read_text(encoding="utf-8")
    assert "-f" in (root / "reboot.log").read_text(encoding="utf-8")
    assert not (root / "switch_root.log").exists()


@pytestmark_unshare
@pytest.mark.parametrize("marker", ["reboot: Restarting system", "reboot: Power down"])
def test_f6_t1_debug_initrd_accepts_kernel_restart_and_powerdown_markers(tmp_path: Path, marker: str) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root / "sd" / "sbin").mkdir(parents=True)
    (root / "sd" / "boot").mkdir(parents=True)
    (root / "sd" / "proc").mkdir()
    (root / "sd" / "sys").mkdir()
    (root / "sd" / "dev").mkdir()
    (root / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (root / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "pstore" / "console-ramoops-0").write_text(f"shutdown\n{marker}\n", encoding="utf-8")

    res = _run_chroot(root, ["/init"])
    assert res.returncode == 0, res.stderr
    assert (root / "sd" / "boot" / "uEnv.txt").is_file()
    assert not (root / "reboot.log").exists()
    assert "/sd /sbin/rsp-preinit" in (root / "switch_root.log").read_text(encoding="utf-8")


# --- Tier 2: Boundary, Corner & Error Paths (6 tests) ---


def _no_remote_tools_env(tmp_path: Path, **extra: str) -> tuple[dict[str, str], Path]:
    """An environment in which the real mk-sd-image.sh cannot reach a board or gain root
    even if its guards fail: `ssh`, `scp`, `sudo` and `sshpass` on PATH are stubs that log
    the attempt and exit non-zero, and HOME is an empty directory (no keys, no ssh config)."""
    stubs = tmp_path / "stub-bin"
    stubs.mkdir(exist_ok=True)
    calls = tmp_path / "remote-calls.log"
    for name in ("ssh", "scp", "sudo", "sshpass"):
        stub = stubs / name
        stub.write_text(f"#!/bin/sh\necho \"{name} $*\" >> {str(calls)!r}\nexit 97\n", encoding="utf-8")
        stub.chmod(0o755)
    home = tmp_path / "empty-home"
    home.mkdir(exist_ok=True)
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(home), "LANG": "C"}
    env.update(extra)
    return env, calls


def test_f6_t2_mk_sd_image_fails_fast_without_sudo_user_or_wifi_ssid(tmp_path: Path) -> None:
    env, calls = _no_remote_tools_env(tmp_path)          # neither SUDO_USER nor WIFI_SSID set
    res_sudo = run_bounded(["bash", str(MK_SD_IMAGE)], env=env, timeout=30)
    assert res_sudo.returncode != 0
    assert "SUDO_USER" in res_sudo.stderr

    # Verify WIFI_SSID guard in an isolated copy with dummy armbian-build artifacts
    sb = tmp_path / "newos"
    (sb / "armbian-build" / "output" / "images").mkdir(parents=True)
    (sb / "armbian-build" / "output" / "debs").mkdir(parents=True)
    (sb / "armbian-build" / "output" / "images" / "test_minimal.img").write_bytes(b"")
    (sb / "armbian-build" / "output" / "debs" / "linux-image-current-rockchip_1.deb").write_bytes(b"")
    (sb / "armbian-build" / "output" / "debs" / "linux-dtb-current-rockchip_1.deb").write_bytes(b"")
    sb_script = sb / "mk-sd-image.sh"
    shutil.copy2(MK_SD_IMAGE, sb_script)

    res_wifi = run_bounded(["bash", str(sb_script)], env={**env, "SUDO_USER": "tester"}, timeout=30)
    assert res_wifi.returncode != 0
    assert "WIFI_SSID" in res_wifi.stderr

    assert "SAFETY_PROFILE" in MK_SD_IMAGE.read_text(encoding="utf-8")
    res_bad = run_bounded(
        ["bash", str(MK_SD_IMAGE), "--safety=bogus"], env={**env, "RSP_PARSE_ONLY": "1"}, timeout=30
    )
    assert res_bad.returncode == 2

    # the guards fired before anything tried to reach a board or become root
    assert not calls.exists(), f"mk-sd-image.sh ran a remote/privileged command: {calls.read_text()}"
    assert not (sb / "out").exists()


@pytestmark_unshare
def test_f6_t2_rsp_preinit_deadman_zero_disables_watchdog_and_increments_boot_dir(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "boot-debug" / "boot-0").mkdir(parents=True)
    (root / "proc" / "cmdline").write_text("rootwait rsp_deadman=0 rspboot\n", encoding="utf-8")
    (root / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "sbin" / "init").write_text(
        "#!/bin/sh\n"
        "n=0\n"
        "while ! grep -q '^899 ' /boot-debug/boot-1/alive.txt 2>/dev/null; do\n"
        "  if [ $n -ge 500 ]; then\n"
        "    echo 'timed out waiting for 899 in boot-1/alive.txt' >&2\n"
        "    cat /boot-debug/boot-1/alive.txt >&2 2>/dev/null\n"
        "    exit 1\n"
        "  fi\n"
        "  /bin/busybox usleep 50000\n"
        "  n=$((n+1))\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = _run_chroot(root, ["/sbin/rsp-preinit"])
    assert res.returncode == 0, res.stderr
    assert (root / "boot-debug" / "boot-1" / "alive.txt").read_text(encoding="utf-8").startswith("899 ")
    assert (root / "boot" / "uEnv.txt").is_file()
    assert not (root / "boot" / "uEnv.txt.done").exists()
    assert (root / "proc" / "sysrq-trigger").read_text(encoding="utf-8") == ""


@pytestmark_unshare
def test_f6_t2_rsp_preinit_malformed_deadman_falls_back_to_300(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "proc" / "cmdline").write_text("rootwait rsp_deadman=abc rspboot\n", encoding="utf-8")
    (root / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "sbin" / "init").write_text(
        "#!/bin/sh\n"
        "n=0\n"
        "while [ ! -s /proc/sysrq-trigger ]; do\n"
        "  [ $n -ge 400 ] && { echo 'timed out waiting for /proc/sysrq-trigger' >&2; exit 1; }\n"
        "  /bin/busybox usleep 50000\n"
        "  n=$((n+1))\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )

    res = _run_chroot(root, ["/sbin/rsp-preinit"])
    assert res.returncode == 0, res.stderr
    assert (root / "boot" / "uEnv.txt.done").is_file()
    alive = (root / "boot-debug" / "boot-0" / "alive.txt").read_text(encoding="utf-8")
    assert "deadman fired at 300" in alive


@pytestmark_unshare
def test_f6_t2_debug_initrd_dmesg_ramoops_overrides_clean_console_log(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root / "sd" / "sbin").mkdir(parents=True)
    (root / "sd" / "boot").mkdir(parents=True)
    (root / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (root / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "pstore" / "console-ramoops-0").write_text("clean end\nrsp-clean-reboot\n", encoding="utf-8")
    (root / "pstore" / "dmesg-ramoops-0").write_text("Oops: 0000 [#1]\n", encoding="utf-8")

    res = _run_chroot(root, ["/init"])
    assert res.returncode == 0, res.stderr
    pstore_archive = root / "sd" / "boot-debug" / "boot-0" / "pstore"
    assert (pstore_archive / "console-ramoops-0").is_file()
    assert (pstore_archive / "dmesg-ramoops-0").is_file()
    assert (root / "sd" / "boot" / "uEnv.txt.done").is_file()


@pytestmark_unshare
@pytest.mark.parametrize(("trailing_lines", "expect_crash"), [(16, True), (14, False)])
def test_f6_t2_debug_initrd_clean_marker_outside_tail15_triggers_crash(
    tmp_path: Path, trailing_lines: int, expect_crash: bool
) -> None:
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root / "sd" / "sbin").mkdir(parents=True)
    (root / "sd" / "boot").mkdir(parents=True)
    (root / "sd" / "proc").mkdir()
    (root / "sd" / "sys").mkdir()
    (root / "sd" / "dev").mkdir()
    (root / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (root / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    lines = ["rsp-clean-reboot"] + [f"trailing line {i}" for i in range(trailing_lines)]
    (root / "pstore" / "console-ramoops-0").write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = _run_chroot(root, ["/init"])
    assert res.returncode == 0, res.stderr
    if expect_crash:
        assert (root / "sd" / "boot" / "uEnv.txt.done").is_file()
        assert (root / "reboot.log").is_file()
    else:
        assert (root / "sd" / "boot" / "uEnv.txt").is_file()
        assert "/sd /sbin/rsp-preinit" in (root / "switch_root.log").read_text(encoding="utf-8")


@pytestmark_unshare
def test_f6_t2_debug_initrd_rspkeep_and_release_profile_preserve_uenv(tmp_path: Path) -> None:
    # (a) bringup + rspkeep on cmdline preserves uEnv.txt despite pstore crash
    root_keep = tmp_path / "keep"
    _init_chroot_rootfs(root_keep)
    (root_keep / "proc" / "cmdline").write_text("rootwait rspboot rspkeep\n", encoding="utf-8")
    (root_keep / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root_keep / "sd" / "sbin").mkdir(parents=True)
    (root_keep / "sd" / "boot").mkdir(parents=True)
    (root_keep / "sd" / "proc").mkdir()
    (root_keep / "sd" / "sys").mkdir()
    (root_keep / "sd" / "dev").mkdir()
    (root_keep / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (root_keep / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (root_keep / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root_keep / "pstore" / "dmesg-ramoops-0").write_text("Oops!\n", encoding="utf-8")

    res_keep = _run_chroot(root_keep, ["/init"])
    assert res_keep.returncode == 0, res_keep.stderr
    assert (root_keep / "sd" / "boot-debug" / "boot-0" / "pstore" / "dmesg-ramoops-0").is_file()
    assert (root_keep / "sd" / "boot" / "uEnv.txt").is_file()
    assert "/sd /sbin/rsp-preinit" in (root_keep / "switch_root.log").read_text(encoding="utf-8")

    # (b) M4 release profile in /sd/etc/rsp-safety preserves uEnv.txt after crash capture
    assert "resolve_safety_profile" in DEBUG_INIT.read_text(encoding="utf-8")
    root_rel = tmp_path / "release"
    _init_chroot_rootfs(root_rel)
    (root_rel / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root_rel / "sd" / "sbin").mkdir(parents=True)
    (root_rel / "sd" / "boot").mkdir(parents=True)
    (root_rel / "sd" / "etc").mkdir(parents=True)
    (root_rel / "sd" / "proc").mkdir()
    (root_rel / "sd" / "sys").mkdir()
    (root_rel / "sd" / "dev").mkdir()
    (root_rel / "sd" / "etc" / "rsp-safety").write_text("release\n", encoding="utf-8")
    (root_rel / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (root_rel / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (root_rel / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root_rel / "pstore" / "dmesg-ramoops-0").write_text("Oops!\n", encoding="utf-8")

    res_rel = _run_chroot(root_rel, ["/init"])
    assert res_rel.returncode == 0, res_rel.stderr
    assert (root_rel / "sd" / "boot-debug" / "boot-0" / "pstore" / "dmesg-ramoops-0").is_file()
    assert (root_rel / "sd" / "boot" / "uEnv.txt").is_file()
    assert "/sd /sbin/rsp-preinit" in (root_rel / "switch_root.log").read_text(encoding="utf-8")


@pytestmark_unshare
@pytest.mark.parametrize("via", ["file", "cmdline"])
def test_f6_t2_rsp_preinit_release_profile_skips_deadman_prunes_and_hands_off_watchdog(
    tmp_path: Path, via: str
) -> None:
    root = tmp_path / f"rootfs_{via}"
    _init_chroot_rootfs(root)
    (root / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "etc" / "systemd").mkdir(parents=True, exist_ok=True)
    # Wait briefly so any erroneous bringup deadman (rsp_deadman=1) would fire and disarm uEnv.txt
    (root / "sbin" / "init").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" > /init_args.txt\n"
        "n=0\n"
        "while [ ! -s /proc/sysrq-trigger ] && [ $n -lt 10 ]; do /bin/busybox usleep 10000; n=$((n+1)); done\n"
        "exit 0\n",
        encoding="utf-8",
    )

    wdog_proc: subprocess.Popen[bytes] | None = None
    try:
        if via == "file":
            (root / "etc" / "rsp-safety").write_text("# release image\n  release  \n", encoding="utf-8")
            (root / "proc" / "cmdline").write_text("rootwait rsp_deadman=1 rspboot\n", encoding="utf-8")
            for idx in range(21):
                (root / "boot-debug" / f"boot-{idx}").mkdir(parents=True)
            (root / "boot-debug" / "boot-21" / "pstore").mkdir(parents=True)
            (root / "etc" / "systemd" / "system.conf").write_text(
                "[Manager]\nRuntimeWatchdogSec=15s\n", encoding="utf-8"
            )
            wdog_proc = subprocess.Popen(["sleep", "30"])
            (root / "run" / "rsp-watchdog.pid").write_text(f"{wdog_proc.pid}\n", encoding="utf-8")

            res = _run_chroot(root, ["/sbin/rsp-preinit", "--unit", "multi-user.target"])
            assert res.returncode == 0, res.stderr
            assert (root / "init_args.txt").read_text(encoding="utf-8").strip() == "--unit multi-user.target"
            assert not (root / "boot-debug" / "boot-0").exists()
            assert (root / "boot-debug" / "boot-1").is_dir()
            assert (root / "boot-debug" / "boot-20").is_dir()
            assert (root / "boot-debug" / "boot-21" / "pstore").is_dir()
            assert not (root / "boot-debug" / "boot-22").exists()
            assert not (root / "run" / "rsp-watchdog.pid").exists()
            rc = wdog_proc.wait(timeout=3)
            assert rc == -signal.SIGTERM
            wdog_proc = None
        else:
            (root / "etc" / "rsp-safety").write_text("bringup\n", encoding="utf-8")
            (root / "proc" / "cmdline").write_text(
                "rootwait rsp_safety=release rsp_deadman=1 rsp_debug_keep=2 rspboot\n", encoding="utf-8"
            )
            for idx in range(4):
                (root / "boot-debug" / f"boot-{idx}").mkdir(parents=True)
            (root / "boot-debug" / "boot-4" / "pstore").mkdir(parents=True)
            (root / "etc" / "systemd" / "system.conf").write_text(
                "[Manager]\nRuntimeWatchdogSec=0\n", encoding="utf-8"
            )
            (root / "run" / "rsp-watchdog.pid").write_text("999999\n", encoding="utf-8")

            res = _run_chroot(root, ["/sbin/rsp-preinit"])
            assert res.returncode == 0, res.stderr
            assert not (root / "boot-debug" / "boot-0").exists()
            assert not (root / "boot-debug" / "boot-1").exists()
            assert (root / "boot-debug" / "boot-2").is_dir()
            assert (root / "boot-debug" / "boot-3").is_dir()
            assert (root / "boot-debug" / "boot-4" / "pstore").is_dir()
            assert not (root / "boot-debug" / "boot-5").exists()
            assert (root / "run" / "rsp-watchdog.pid").is_file()

        assert (root / "boot" / "uEnv.txt").is_file()
        assert not (root / "boot" / "uEnv.txt.done").exists()
        assert (root / "proc" / "sysrq-trigger").read_text(encoding="utf-8") == ""
    finally:
        if wdog_proc is not None and wdog_proc.poll() is None:
            wdog_proc.kill()
            wdog_proc.wait(timeout=5)


@pytestmark_unshare
@pytest.mark.parametrize("profile_word", ["rsp_safety=release", ""])
def test_f6_t2_debug_initrd_without_rspboot_always_hands_back_to_emmc(tmp_path: Path, profile_word: str) -> None:
    """Without rspboot the initramfs is in staged bring-up mode: when no stage is left it must
    disarm the card before rebooting, whatever the profile, or the board reboots forever."""
    root = tmp_path / "rootfs"
    _init_chroot_rootfs(root)
    (root / "proc" / "cmdline").write_text(f"rootwait {profile_word}\n", encoding="utf-8")
    (root / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root / "sd" / "boot").mkdir(parents=True)
    (root / "sd" / "etc").mkdir(parents=True)
    (root / "sd" / "etc" / "rsp-safety").write_text("release\n", encoding="utf-8")
    (root / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")

    res = _run_chroot(root, ["/init"], timeout=60)
    assert res.returncode == 0, res.stderr
    assert (root / "reboot.log").is_file(), "init did not reboot"
    assert not (root / "sd" / "boot" / "uEnv.txt").exists(), "card still armed: the next boot comes straight back here"
    assert (root / "sd" / "boot" / "uEnv.txt.done").is_file()
