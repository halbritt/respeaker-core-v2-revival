"""Tier 3 (pairwise cross-feature) and Tier 4 (real-world E2E scenarios) tests."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import py_compile
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import BOARD_DIR, NEWOS_DIR, REPO_ROOT, unshare_available
from test_odas_patches import build_preimage_tree, parse_unified_diff
from test_privacy_hygiene import (
    EXPECTED_NOREPLY_EMAIL,
    find_lan_ips,
    find_owner_leaks,
    find_private_hostnames,
    find_wifi_or_token_secrets,
    list_repo_files,
    read_text_if_not_binary,
)
from test_syntax_imports import discover_repo_scripts

BUSYBOX_HOST = Path("/usr/bin/busybox")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- Tier 3: Cross-Feature Pairwise Combinations (7 tests) ---


def test_tier3_f1_f2_privacy_and_syntax_clean_across_all_repo_scripts(tmp_path: Path) -> None:
    leaks: list[str] = []
    for path in list_repo_files():
        text = read_text_if_not_binary(path)
        if text is None:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for ln, m in find_lan_ips(text) + find_private_hostnames(text) + find_wifi_or_token_secrets(text) + find_owner_leaks(text):
            leaks.append(f"{rel}:{ln}: {m}")
    assert not leaks, f"Privacy leaks across repo files: {leaks}"

    py_files, sh_files, bash_files = discover_repo_scripts()
    for rel in py_files:
        src = REPO_ROOT / rel
        ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
        py_compile.compile(str(src), cfile=str(tmp_path / (src.name + ".pyc")), doraise=True)
    for rel in sh_files:
        res = subprocess.run(["sh", "-n", str(REPO_ROOT / rel)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"sh -n failed for {rel}: {res.stderr}"
    for rel in bash_files:
        res = subprocess.run(["bash", "-n", str(REPO_ROOT / rel)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"bash -n failed for {rel}: {res.stderr}"


def test_tier3_f1_f6_mk_sd_image_runtime_secrets_not_persisted_in_repo() -> None:
    mk_sd = (NEWOS_DIR / "mk-sd-image.sh").read_text(encoding="utf-8")
    env_cfg = (BOARD_DIR / "rsp-stt.env").read_text(encoding="utf-8")
    for text in (mk_sd, env_cfg):
        assert find_lan_ips(text) == []
        assert find_private_hostnames(text) == []
        assert find_wifi_or_token_secrets(text) == []
        assert find_owner_leaks(text) == []

    assert "WIFI_SSID=${WIFI_SSID:?" in mk_sd
    assert "PSK=${WIFI_PSK:-" in mk_sd
    for ignored in ("board-credentials.txt", "newos/out/"):
        assert subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", ignored], check=False).returncode == 0


def test_tier3_f2_f3_board_cli_import_and_localhost_sse_auth_handshake(
    rsp_stt_loader, event_server_factory, tmp_path: Path
) -> None:
    stt = rsp_stt_loader(env={"STT_HTTP_TOKEN": "t3-secret"})
    assert stt.audioop is None and stt.np is not None
    _, port = event_server_factory(stt)

    bad_req = urllib.request.Request(f"http://127.0.0.1:{port}/events", headers={"Authorization": "Bearer wrong"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(bad_req, timeout=3)
    assert exc.value.code == 401

    hits = tmp_path / "hits.jsonl"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        f"with open({str(hits)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'text': os.environ['RSP_TEXT'], 'az': os.environ['RSP_AZ'], "
        "'payload': json.loads(sys.stdin.read())}) + '\\n')\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            str(BOARD_DIR / "rsp-listen"),
            "--url",
            f"http://127.0.0.1:{port}/events",
            "--token",
            "t3-secret",
            "--wake",
            "computer",
            "--strip-wake",
            "--",
            sys.executable,
            str(probe),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 3.0
        while not stt.SUBSCRIBERS and time.time() < deadline:
            time.sleep(0.02)
        assert len(stt.SUBSCRIBERS) == 1
        stt.publish("utterance", {"text": "Computer, check status", "az": 120, "ts": "2026-11-17T00:00:00Z"})

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if hits.exists() and hits.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.02)
        if not hits.is_file():
            rc = proc.poll()
            if rc is not None:
                out, err = proc.communicate(timeout=2)
                diag = f"rc={rc}, stdout={out!r}, stderr={err!r}"
            else:
                diag = "rsp-listen still running"
            pytest.fail(f"hits file {hits} was not created before deadline ({diag})")
        row = json.loads(hits.read_text(encoding="utf-8").strip())
        assert row["text"] == "check status"
        assert row["az"] == "120"
        assert row["payload"]["text"] == "check status"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_tier3_f3_f4_odas_cfg_ports_and_rsp_stt_rewriting_contract(rsp_stt_loader, tmp_path: Path) -> None:
    stt = rsp_stt_loader(
        env={
            "STT_RUN_DIR": str(tmp_path),
            "STT_ODAS_TRACK_PORT": "9200",
            "STT_ODAS_AUDIO_PORT": "9202",
            "STT_ODAS_SEP_PORT": "9203",
        }
    )
    out_cfg = Path(stt.odas_config(str(tmp_path / "raw.fifo"))).read_text(encoding="utf-8")
    assert 'type = "file"; path =' in out_cfg
    assert 'type = "soundcard"' not in out_cfg
    assert len(re.findall(r"angle\s*=\s*\(\s*180\.0,\s*180\.0\s*\);", out_cfg)) == 6
    assert "map: (1, 2, 3, 4, 5, 6);" in out_cfg
    for port in ("9200", "9202", "9203"):
        assert f"port = {port};" in out_cfg


def test_tier3_f4_f5_patch_hygiene_across_odas_and_kernel_stacks(tmp_path: Path) -> None:
    odas_patches = sorted((NEWOS_DIR / "cross" / "patches").glob("*.patch"))
    assert odas_patches
    kernel_patches = [NEWOS_DIR / "port" / "0001-ASoC-codecs-add-AC108-mic-array-driver-for-ReSpeaker.patch"]
    port_patches_dir = NEWOS_DIR / "port" / "patches"
    assert port_patches_dir.is_dir(), "newos/port/patches/ missing"
    kernel_patches.extend(sorted(port_patches_dir.glob("*.patch")))

    for p in odas_patches + kernel_patches:
        txt = p.read_text(encoding="utf-8")
        assert "<<<<<<<" not in txt and ">>>>>>>" not in txt
        files = parse_unified_diff(txt)
        assert files, f"No diffs in {p}"
        for fentry in files:
            for h in fentry["hunks"]:
                assert sum(1 for line in h["body"] if line.startswith((" ", "-"))) == h["old_count"]
                assert sum(1 for line in h["body"] if line.startswith((" ", "+"))) == h["new_count"]

    tree = tmp_path / "kernel_a"
    shutil.copytree(NEWOS_DIR / "port" / "tree" / "a", tree)
    chk = subprocess.run(
        ["git", "apply", "--check", str(kernel_patches[0])],
        cwd=tree,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert chk.returncode == 0, chk.stderr

    for p in odas_patches:
        od_tree = tmp_path / p.stem
        pflag = build_preimage_tree(od_tree, p.read_text(encoding="utf-8"))
        res = subprocess.run(
            ["git", "apply", pflag, "--check", str(p)],
            cwd=od_tree,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"{p.name}: {res.stderr}"


def test_tier3_f5_f6_dts_pstore_ramoops_matches_debug_initrd_and_mk_sd_image() -> None:
    dts = (NEWOS_DIR / "port" / "rk3229-respeaker-v2.dts").read_text(encoding="utf-8")
    mk_sd = (NEWOS_DIR / "mk-sd-image.sh").read_text(encoding="utf-8")
    dbg_init = (NEWOS_DIR / "debug-initrd" / "init").read_text(encoding="utf-8")

    ram_m = re.search(r"ramoops@(90000000)", dts)
    assert ram_m is not None
    ramoops_addr = int(ram_m.group(1), 16)
    fdt_addr = int(re.search(r"fdt_addr_r=(0x[0-9a-fA-F]+)", mk_sd).group(1), 16)
    ramdisk_addr = int(re.search(r"ramdisk_addr_r=(0x[0-9a-fA-F]+)", mk_sd).group(1), 16)
    assert fdt_addr < ramdisk_addr < ramoops_addr

    assert "dtb=rk3229-respeaker-v2.dtb" in mk_sd
    assert "max-frequency = <37500000>;" in dts
    assert "mount -t pstore pstore" in dbg_init
    assert "console-ramoops-0" in dbg_init and "dmesg-" in dbg_init


def test_tier3_f3_f6_mk_sd_image_installs_exact_board_runtime_and_env_contract() -> None:
    mk_sd = (NEWOS_DIR / "mk-sd-image.sh").read_text(encoding="utf-8")
    svc = (BOARD_DIR / "rsp-stt.service").read_text(encoding="utf-8")
    env_txt = (BOARD_DIR / "rsp-stt.env").read_text(encoding="utf-8")

    expected_board_files = (
        "rsp-stt.py",
        "odas-respeaker-core-v2.cfg",
        "rsp-stt.env",
        "rsp-stt.service",
        "rsp-record",
        "rsp-replay",
        "rsp-listen",
    )
    for fname in expected_board_files:
        assert (BOARD_DIR / fname).is_file()
        assert fname in mk_sd, f"{fname} missing from mk-sd-image.sh install rules"

    assert 'install -m 755 "$HERE/../board/rsp-stt.py" "$R/opt/rsp-stt/rsp-stt.py"' in mk_sd
    assert "ExecStart=/usr/bin/python3 -u /opt/rsp-stt/rsp-stt.py" in svc
    assert 'install -m 644 "$HERE/../board/rsp-stt.env" "$R/etc/default/rsp-stt"' in mk_sd
    assert "EnvironmentFile=/etc/default/rsp-stt" in svc
    assert "STT_ODAS_CFG=/opt/rsp-stt/odas-respeaker-core-v2.cfg" in env_txt


# --- Tier 4: Real-World Application Scenarios (5 tests) ---


def test_tier4_e2e_full_stt_pipeline_odas_vad_whisper_sse_to_rsp_listen(
    whisper_mock_factory, tmp_path: Path
) -> None:
    base_url, captured = whisper_mock_factory("Computer, turn on the desk light")
    track_port, audio_port, sep_port, http_port = (_free_port() for _ in range(4))

    run_dir = tmp_path / "run"
    rec_dir = tmp_path / "rec"
    site_dir = tmp_path / "site"
    bin_dir = tmp_path / "bin"
    for d in (run_dir, rec_dir, site_dir, bin_dir):
        d.mkdir()
    log_file = tmp_path / "transcripts.jsonl"

    # Inject mock onnxruntime into child rsp-stt.py process via PYTHONPATH sitecustomize
    (site_dir / "sitecustomize.py").write_text(
        "import sys, types, numpy as np\n"
        "m = types.ModuleType('onnxruntime')\n"
        "class SessionOptions: pass\n"
        "class InferenceSession:\n"
        "    def __init__(self, *a, **kw): pass\n"
        "    def run(self, _, feed): return np.array([[0.95]], dtype=np.float32), feed['state']\n"
        "m.SessionOptions = SessionOptions\n"
        "m.InferenceSession = InferenceSession\n"
        "sys.modules['onnxruntime'] = m\n",
        encoding="utf-8",
    )

    fake_arecord = bin_dir / "arecord"
    fake_arecord.write_text(
        "#!/bin/sh\nwhile dd if=/dev/zero bs=15360 count=10 2>/dev/null; do sleep 0.05; done\n",
        encoding="utf-8",
    )
    fake_arecord.chmod(0o755)

    go_file = tmp_path / "odas.go"
    fake_odas = tmp_path / "fake_odaslive.py"
    fake_odas.write_text(
        "import json, os, re, socket, sys, time, numpy as np\n"
        "cfg = open(sys.argv[sys.argv.index('-c') + 1]).read()\n"
        "ports = [int(p) for p in re.findall(r'port = (\\d+);', cfg)]\n"
        "# cfg order: tracked (0), separated (1), postfiltered (2)\n"
        "tp, sp, ap = ports[0], ports[1], ports[2]\n"
        "st = socket.create_connection(('127.0.0.1', tp), timeout=5)\n"
        "ss = socket.create_connection(('127.0.0.1', sp), timeout=5)\n"
        "sa = socket.create_connection(('127.0.0.1', ap), timeout=5)\n"
        "st.sendall((json.dumps({'src': [{'id': 1, 'x': 0.0, 'y': 1.0, 'z': 0.0, 'activity': 0.9}]}) + '\\n').encode())\n"
        f"while not os.path.exists({str(go_file)!r}):\n"
        "    time.sleep(0.02)\n"
        "hi = np.zeros((480, 4), dtype='<i2'); hi[::2, 0] = 1400; hi[1::2, 0] = -1400\n"
        "lo = np.zeros((480, 4), dtype='<i2'); lo[::2, 0] = 400; lo[1::2, 0] = -400\n"
        "qu = np.zeros((480, 4), dtype='<i2'); qu[::2, 0] = 10; qu[1::2, 0] = -10\n"
        "for idx in range(36):\n"
        "    blk = (hi if idx % 2 == 0 else lo).tobytes()\n"
        "    ss.sendall(blk); sa.sendall(blk)\n"
        "for _ in range(16):\n"
        "    blk = qu.tobytes()\n"
        "    ss.sendall(blk); sa.sendall(blk)\n"
        "time.sleep(8)\n",
        encoding="utf-8",
    )
    fake_odas_sh = bin_dir / "fake-odaslive"
    fake_odas_sh.write_text(f"#!/bin/sh\nexec {sys.executable!r} {str(fake_odas)!r} \"$@\"\n", encoding="utf-8")
    fake_odas_sh.chmod(0o755)

    stt_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(site_dir),
        "STT_SOURCE": "odas",
        "STT_ODAS_BIN": str(fake_odas_sh),
        "STT_ODAS_CFG": str(BOARD_DIR / "odas-respeaker-core-v2.cfg"),
        "STT_ODAS_TRACK_PORT": str(track_port),
        "STT_ODAS_AUDIO_PORT": str(audio_port),
        "STT_ODAS_SEP_PORT": str(sep_port),
        "STT_HTTP": f"127.0.0.1:{http_port}",
        "STT_SERVER": f"{base_url}/inference",
        "STT_RUN_DIR": str(run_dir),
        "STT_REC_DIR": str(rec_dir),
        "STT_LOG": str(log_file),
        "STT_SPEECH_MIN_MS": "96",
        "STT_SPEECH_MODEL": str(tmp_path / "silero.onnx"),
        "STT_END_SILENCE_MS": "180",
        "STT_MIN_UTTERANCE_MS": "150",
        "STT_PREROLL_MS": "90",
        "STT_MIN_DYN_DB": "5",
    }

    stt_proc = subprocess.Popen(
        [sys.executable, "-u", str(BOARD_DIR / "rsp-stt.py")],
        env=stt_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    listen_proc = None
    try:
        # Wait until rsp-stt EventServer is listening before launching rsp-listen
        deadline = time.time() + 6.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", http_port), timeout=0.2):
                    break
            except OSError:
                assert stt_proc.poll() is None, f"rsp-stt exited early: {stt_proc.stderr.read()}"
                time.sleep(0.05)

        hits = tmp_path / "listen_hits.jsonl"
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import json, os, sys\n"
            f"with open({str(hits)!r}, 'a') as f:\n"
            "    f.write(json.dumps({'text': os.environ['RSP_TEXT'], 'az': os.environ['RSP_AZ'], "
            "'stdin': json.loads(sys.stdin.read())}) + '\\n')\n",
            encoding="utf-8",
        )
        listen_proc = subprocess.Popen(
            [
                sys.executable,
                str(BOARD_DIR / "rsp-listen"),
                "--url",
                f"http://127.0.0.1:{http_port}/events",
                "--wake",
                "computer",
                "--strip-wake",
                "--",
                sys.executable,
                str(probe),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.3)
        go_file.write_text("go\n", encoding="utf-8")

        end_wait = time.time() + 8.0
        while time.time() < end_wait:
            if hits.exists() and hits.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.05)

        assert hits.exists() and hits.read_text(encoding="utf-8").strip(), (
            f"No utterance received by rsp-listen; stt_stderr={stt_proc.stderr.read() if stt_proc.poll() is not None else 'running'}"
        )
        hit = json.loads(hits.read_text(encoding="utf-8").splitlines()[0])
        assert hit["text"] == "turn on the desk light"
        assert hit["az"] == "90"
        assert hit["stdin"]["text"] == "turn on the desk light"

        transcript = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
        assert transcript["text"] == "Computer, turn on the desk light"
        assert transcript["az"] == 90
        assert captured and captured[0]["path"] == "/inference"
    finally:
        if listen_proc is not None:
            listen_proc.terminate()
            listen_proc.wait(timeout=5)
        stt_proc.terminate()
        stt_proc.wait(timeout=5)


def test_tier4_e2e_offline_recording_cap_pruning_and_replay_transcript(
    whisper_mock_factory, tmp_path: Path
) -> None:
    base_url, _ = whisper_mock_factory("Offline replay check succeeded")
    bin_dir = tmp_path / "bin"
    run_dir = tmp_path / "run"
    rec_home = tmp_path / "home"
    rec_dir = rec_home / "recordings"
    led_dir = tmp_path / "led"
    for d in (bin_dir, run_dir, rec_dir, led_dir):
        d.mkdir(parents=True)
    (led_dir / "trigger").write_text("mmc0\n", encoding="utf-8")
    (led_dir / "brightness").write_text("0\n", encoding="utf-8")

    (bin_dir / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "sudo").write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for n in ("systemctl", "sudo", "sleep"):
        (bin_dir / n).chmod(0o755)

    sandbox_rec = tmp_path / "rsp-record"
    sandbox_rec.write_text(
        (BOARD_DIR / "rsp-record")
        .read_text(encoding="utf-8")
        .replace("run=/run/rsp-stt", f"run={run_dir}")
        .replace('led="/sys/class/leds/respeaker2:blue:state2"', f'led="{led_dir}"'),
        encoding="utf-8",
    )
    sandbox_rec.chmod(0o755)

    old_raw = rec_dir / "20260101T000000Z-stale.raw"
    old_jsonl = rec_dir / "20260101T000000Z-stale.transcript.jsonl"
    old_raw.write_bytes(b"\x02" * 1_500_000)
    old_jsonl.write_text('{"text": "stale"}\n', encoding="utf-8")
    new_raw = rec_dir / "20261117T000000Z-take1.raw"

    def responder() -> None:
        req = run_dir / "record.req"
        act = run_dir / "record.active"
        deadline = time.time() + 3.0
        while not req.exists() and time.time() < deadline:
            time.sleep(0.002)
        new_raw.write_bytes(b"\x00" * (15360 * 5))
        act.write_text(str(new_raw) + "\n", encoding="utf-8")
        time.sleep(0.05)
        act.unlink()

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    rec_res = subprocess.run(
        ["sh", str(sandbox_rec), "1", "take1"],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(rec_home), "RSP_REC_CAP_MB": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    t.join(timeout=3)
    assert rec_res.returncode == 0, rec_res.stderr
    assert not old_raw.exists() and not old_jsonl.exists()
    assert new_raw.is_file()

    # Now replay new_raw through sandboxed rsp-replay -> real rsp-stt.py offline -> fake odaslive
    env_file = tmp_path / "rsp-stt.env"
    env_file.write_text(
        (BOARD_DIR / "rsp-stt.env")
        .read_text(encoding="utf-8")
        .replace("STT_ODAS_CFG=/opt/rsp-stt/odas-respeaker-core-v2.cfg", f"STT_ODAS_CFG={BOARD_DIR / 'odas-respeaker-core-v2.cfg'}")
        .replace("STT_SPEECH_MIN_MS=200", "STT_SPEECH_MIN_MS=0")
        .replace("STT_END_SILENCE_MS=1500", "STT_END_SILENCE_MS=150"),
        encoding="utf-8",
    )
    sandbox_replay = tmp_path / "rsp-replay"
    sandbox_replay.write_text(
        (BOARD_DIR / "rsp-replay")
        .read_text(encoding="utf-8")
        .replace("/etc/default/rsp-stt.local", str(tmp_path / "none_local_env"))
        .replace("/etc/default/rsp-stt", str(env_file)),
        encoding="utf-8",
    )
    sandbox_replay.chmod(0o755)

    fake_odas = tmp_path / "offline_odas.py"
    fake_odas.write_text(
        "import json, re, socket, sys, time, numpy as np\n"
        "cfg = open(sys.argv[sys.argv.index('-c') + 1]).read()\n"
        "ports = [int(p) for p in re.findall(r'port = (\\d+);', cfg)]\n"
        "tp, sp, ap = ports[0], ports[1], ports[2]\n"
        "st = socket.create_connection(('127.0.0.1', tp), timeout=5)\n"
        "ss = socket.create_connection(('127.0.0.1', sp), timeout=5)\n"
        "sa = socket.create_connection(('127.0.0.1', ap), timeout=5)\n"
        "st.sendall((json.dumps({'src': [{'id': 1, 'x': 1.0, 'y': 0.0, 'z': 0.0, 'activity': 0.9}]}) + '\\n').encode())\n"
        "time.sleep(0.25)\n"
        "hi = np.zeros((480, 4), dtype='<i2'); hi[::2, 0] = 1500; hi[1::2, 0] = -1500\n"
        "lo = np.zeros((480, 4), dtype='<i2'); lo[::2, 0] = 400; lo[1::2, 0] = -400\n"
        "qu = np.zeros((480, 4), dtype='<i2'); qu[::2, 0] = 10; qu[1::2, 0] = -10\n"
        "for idx in range(30):\n"
        "    blk = (hi if idx % 2 == 0 else lo).tobytes()\n"
        "    ss.sendall(blk); sa.sendall(blk)\n"
        "    time.sleep(0.005)\n"
        "for _ in range(10):\n"
        "    blk = qu.tobytes()\n"
        "    ss.sendall(blk); sa.sendall(blk)\n"
        "    time.sleep(0.005)\n"
        "sa.close(); ss.close(); st.close()\n",
        encoding="utf-8",
    )
    fake_odas_bin = tmp_path / "offline-odaslive"
    fake_odas_bin.write_text(f"#!/bin/sh\nexec {sys.executable!r} {str(fake_odas)!r} \"$@\"\n", encoding="utf-8")
    fake_odas_bin.chmod(0o755)

    replay_res = subprocess.run(
        ["sh", str(sandbox_replay), str(new_raw), str(fake_odas_bin)],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RSP_STT": str(BOARD_DIR / "rsp-stt.py"),
            "STT_SERVER": f"{base_url}/inference",
            "STT_MIN_UTTERANCE_MS": "120",
            "STT_PREROLL_MS": "60",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert replay_res.returncode == 0, f"{replay_res.stdout}\n{replay_res.stderr}"
    out_jsonl = new_raw.with_suffix(".transcript.jsonl")
    assert out_jsonl.is_file()
    rows = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["text"] == "Offline replay check succeeded"
    assert rows[0]["az"] == 0


def test_tier4_e2e_release_hygiene_and_repo_readiness_gate(tmp_path: Path) -> None:
    email = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "config", "--get", "user.email"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert email == EXPECTED_NOREPLY_EMAIL
    assert (REPO_ROOT / "LICENSE").is_file()
    assert (REPO_ROOT / "LICENSES" / "GPL-2.0-only.txt").is_file()

    for path in list_repo_files():
        text = read_text_if_not_binary(path)
        if text is None:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        assert not find_lan_ips(text), rel
        assert not find_private_hostnames(text), rel
        assert not find_wifi_or_token_secrets(text), rel
        assert not find_owner_leaks(text), rel

    py_files, sh_files, bash_files = discover_repo_scripts()
    assert any(p.startswith("tests/") for p in py_files)
    for rel in py_files:
        ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        py_compile.compile(str(REPO_ROOT / rel), cfile=str(tmp_path / (Path(rel).name + ".pyc")), doraise=True)
    for rel in sh_files:
        assert subprocess.run(["sh", "-n", str(REPO_ROOT / rel)], check=False).returncode == 0, rel
    for rel in bash_files:
        assert subprocess.run(["bash", "-n", str(REPO_ROOT / rel)], check=False).returncode == 0, rel


def test_tier4_e2e_kernel_ac108_dts_and_odas_patch_integration(tmp_path: Path) -> None:
    port_dir = NEWOS_DIR / "port"
    tree = tmp_path / "kernel"
    shutil.copytree(port_dir / "tree" / "a", tree)
    ac108_patch = port_dir / "0001-ASoC-codecs-add-AC108-mic-array-driver-for-ReSpeaker.patch"
    res = subprocess.run(
        ["patch", "-d", str(tree), "-p1", "-i", str(ac108_patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"

    applied_c = hashlib.sha256((tree / "sound/soc/codecs/ac108.c").read_bytes()).hexdigest()
    tree_b_c = hashlib.sha256((port_dir / "tree/b/sound/soc/codecs/ac108.c").read_bytes()).hexdigest()
    port_c = hashlib.sha256((port_dir / "ac108.c").read_bytes()).hexdigest()
    assert applied_c == tree_b_c
    assert applied_c == port_c, "M3 pending: newos/port/ac108.c differs from tree/b & 0001 patch"

    dts = re.sub(r"/\*.*?\*/", "", (port_dir / "rk3229-respeaker-v2.dts").read_text(encoding="utf-8"), flags=re.DOTALL)
    assert "max-frequency = <37500000>;" in dts
    assert "ramoops@90000000" in dts
    assert "phy-supply" not in dts

    odas_patches = sorted((NEWOS_DIR / "cross" / "patches").glob("*.patch"))
    assert odas_patches
    for p in odas_patches:
        ot = tmp_path / p.stem
        pflag = build_preimage_tree(ot, p.read_text(encoding="utf-8"))
        chk = subprocess.run(
            ["git", "apply", pflag, "--check", str(p)],
            cwd=ot,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert chk.returncode == 0, f"{p.name}: {chk.stderr}"


@pytest.mark.skipif(
    not (unshare_available() and BUSYBOX_HOST.is_file()),
    reason="unshare -Ur or static /usr/bin/busybox unavailable on host",
)
def test_tier4_e2e_boot_safety_net_arm_deadman_and_pstore_recovery(tmp_path: Path) -> None:
    from test_boot_safety_net import _init_chroot_rootfs, _run_chroot

    # Step 1: clean boot in debug-initrd/init -> switch_root -> rsp-preinit -> deadman fires
    root = tmp_path / "lifecycle"
    _init_chroot_rootfs(root)
    (root / "proc" / "cmdline").write_text("rootwait rsp_deadman=1 rspboot\n", encoding="utf-8")
    (root / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (root / "sd" / "sbin").mkdir(parents=True)
    (root / "sd" / "boot").mkdir(parents=True)
    (root / "sd" / "proc").mkdir()
    (root / "sd" / "sys").mkdir()
    (root / "sd" / "dev").mkdir()
    (root / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (root / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (root / "pstore" / "console-ramoops-0").write_text("boot ok\nrsp-clean-reboot\n", encoding="utf-8")

    init_res = _run_chroot(root, ["/init"])
    assert init_res.returncode == 0, init_res.stderr
    assert "/sd /sbin/rsp-preinit" in (root / "switch_root.log").read_text(encoding="utf-8")
    assert (root / "sd" / "boot" / "uEnv.txt").is_file()

    # Step 2: rsp-preinit deadman fires without /run/rsp-keep
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
    pre_res = _run_chroot(root, ["/sbin/rsp-preinit"])
    assert pre_res.returncode == 0, pre_res.stderr
    assert (root / "boot" / "uEnv.txt.done").is_file()

    # Step 3: crash pstore on next boot archives pstore and disarms SD uEnv.txt
    crash_root = tmp_path / "crash"
    _init_chroot_rootfs(crash_root)
    (crash_root / "dev" / "mmcblk0p2").write_text("", encoding="utf-8")
    (crash_root / "sd" / "sbin").mkdir(parents=True)
    (crash_root / "sd" / "boot").mkdir(parents=True)
    (crash_root / "sd" / "sbin" / "rsp-preinit").write_text("#!/bin/sh\n", encoding="utf-8")
    (crash_root / "sd" / "sbin" / "rsp-preinit").chmod(0o755)
    (crash_root / "sd" / "boot" / "uEnv.txt").write_text("dtb=rk3229-respeaker-v2.dtb\n", encoding="utf-8")
    (crash_root / "pstore" / "dmesg-ramoops-0").write_text("Kernel panic - not syncing\n", encoding="utf-8")

    crash_res = _run_chroot(crash_root, ["/init"])
    assert crash_res.returncode == 0, crash_res.stderr
    assert (crash_root / "sd" / "boot-debug" / "boot-0" / "pstore" / "dmesg-ramoops-0").is_file()
    assert (crash_root / "sd" / "boot" / "uEnv.txt.done").is_file()
    assert "-f" in (crash_root / "reboot.log").read_text(encoding="utf-8")
