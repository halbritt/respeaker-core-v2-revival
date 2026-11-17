"""F2: Syntax & Import Baseline verification tests."""
from __future__ import annotations

import ast
import importlib.util
import py_compile
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT, load_rsp_listen

PYTHON_TARGETS = [
    "board/rsp-stt.py",
    "board/rsp-listen",
    "tools/bin/rsp-serial",
    "newos/debug-initrd/usblog.py",
    "newos/debug-initrd/usb-shell.py",
]

POSIX_SHELL_TARGETS = [
    "board/rsp-record",
    "board/rsp-replay",
    "newos/cross/build-odas.sh",
    "newos/cross/cross.sh",
    "newos/debug-initrd/fetch-logs.sh",
    "newos/debug-initrd/init",
    "newos/debug-initrd/run-stage.sh",
    "newos/debug-initrd/usb-push.sh",
    "newos/overlay/sbin/rsp-preinit",
]

BASH_TARGETS = [
    "newos/mk-sd-image.sh",
]


def discover_repo_scripts() -> tuple[list[str], list[str], list[str]]:
    """Walk non-ignored files in board/, newos/, tools/, tests/ for Python, sh, and bash scripts."""
    res = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "board",
            "newos",
            "tools",
            "tests",
        ],
        capture_output=True,
        check=True,
    )
    py_files: list[str] = []
    sh_files: list[str] = []
    bash_files: list[str] = []

    for raw in res.stdout.split(b"\x00"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="replace")
        p = REPO_ROOT / rel
        if not p.is_file():
            continue
        data = p.read_bytes()
        if b"\x00" in data[:4096]:
            continue
        first_line = data.splitlines()[0].decode("utf-8", errors="replace") if data else ""
        if rel.endswith(".py") or "python" in first_line:
            py_files.append(rel)
        elif (
            rel.endswith(".sh")
            or first_line.startswith("#!/bin/sh")
            or first_line.startswith("#!/bin/busybox sh")
            or re.match(r"^#!\s*/usr/bin/env\s+sh(?:\s|$)", first_line) is not None
        ):
            if "bash" in first_line:
                bash_files.append(rel)
            else:
                sh_files.append(rel)
        elif "bash" in first_line:
            bash_files.append(rel)
        elif rel in {"board/rsp-record", "board/rsp-replay"}:
            sh_files.append(rel)

    return sorted(py_files), sorted(sh_files), sorted(bash_files)


# --- Tier 1: Happy-Path Syntax & Import Baseline (5 tests) ---


@pytest.mark.parametrize("rel_path", PYTHON_TARGETS)
def test_f2_t1_python_files_parse_and_compile_cleanly(rel_path: str, tmp_path: Path) -> None:
    src = REPO_ROOT / rel_path
    ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
    cfile = tmp_path / (src.name + ".pyc")
    compiled = py_compile.compile(str(src), cfile=str(cfile), doraise=True)
    assert compiled == str(cfile) and cfile.is_file()


@pytest.mark.parametrize("rel_path", POSIX_SHELL_TARGETS)
def test_f2_t1_posix_shell_scripts_pass_sh_syntax(rel_path: str) -> None:
    res = subprocess.run(["sh", "-n", str(REPO_ROOT / rel_path)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"sh -n failed for {rel_path}: {res.stderr}"


@pytest.mark.parametrize("rel_path", BASH_TARGETS)
def test_f2_t1_bash_scripts_pass_bash_syntax(rel_path: str) -> None:
    target = REPO_ROOT / rel_path
    first_line = target.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/env bash"
    res = subprocess.run(["bash", "-n", str(target)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"bash -n failed for {rel_path}: {res.stderr}"


def test_f2_t1_board_python_modules_import_without_side_effects(rsp_stt_loader, tmp_path: Path) -> None:
    stt = rsp_stt_loader()
    listen = load_rsp_listen()
    assert callable(stt.Segmenter)
    assert callable(stt.clean)
    assert callable(listen.events)
    assert callable(listen.main)
    assert not (tmp_path / "run").exists()


def test_f2_t1_rsp_listen_cli_help_exits_cleanly() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "board" / "rsp-listen"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    for flag in ("--wake", "--follow", "--strip-wake", "--print", "RSP_TEXT", "RSP_AZ"):
        assert flag in res.stdout


# --- Tier 2: Dynamic Discovery, Edge & Guard Checks (5 tests) ---


def test_f2_t2_script_discovery_covers_every_shebang_and_extension(tmp_path: Path) -> None:
    py_files, sh_files, bash_files = discover_repo_scripts()
    for rel in py_files:
        src = REPO_ROOT / rel
        ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
        py_compile.compile(str(src), cfile=str(tmp_path / (src.name + ".pyc")), doraise=True)
    for rel in sh_files:
        res = subprocess.run(["sh", "-n", str(REPO_ROOT / rel)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"sh -n failed for discovered script {rel}: {res.stderr}"
    for rel in bash_files:
        res = subprocess.run(["bash", "-n", str(REPO_ROOT / rel)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"bash -n failed for discovered script {rel}: {res.stderr}"

    assert set(PYTHON_TARGETS).issubset(set(py_files))
    assert set(POSIX_SHELL_TARGETS).issubset(set(sh_files))
    assert set(BASH_TARGETS).issubset(set(bash_files))


def test_f2_t2_rsp_stt_imports_when_audioop_absent_and_numpy_present(
    rsp_stt_loader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "audioop", None)  # type: ignore[arg-type]
    mod = rsp_stt_loader()
    assert mod.audioop is None
    assert mod.np is not None

    monkeypatch.setitem(sys.modules, "numpy", None)  # type: ignore[arg-type]
    with pytest.raises(ImportError):
        rsp_stt_loader()


def test_f2_t2_rsp_stt_import_evaluates_env_overrides_and_defaults(rsp_stt_loader) -> None:
    mod = rsp_stt_loader(
        env={
            "STT_SERVER": "http://127.0.0.1:9999/inference",
            "STT_CHANNELS": "4",
            "STT_MICS": "2",
            "STT_ODAS_IGNORE": "350:15,90:10",
            "STT_OFFLINE": "1",
        }
    )
    assert mod.SERVER == "http://127.0.0.1:9999/inference"
    assert mod.FRAME_BYTES == 480 * 4 * 2
    assert mod.PENDING.maxsize == 0
    assert mod.ODAS_IGNORE == [(350.0, 15.0), (90.0, 10.0)]


def test_f2_t2_extensionless_rsp_listen_requires_source_file_loader() -> None:
    path = REPO_ROOT / "board" / "rsp-listen"
    assert importlib.util.spec_from_file_location("rsp_listen", path) is None
    mod = load_rsp_listen()
    assert hasattr(mod, "events")


def test_f2_t2_unsafe_top_level_python_scripts_lack_main_guard_and_fail_without_tty(tmp_path: Path) -> None:
    for rel in ("tools/bin/rsp-serial", "newos/debug-initrd/usblog.py", "newos/debug-initrd/usb-shell.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        has_main_guard = any(
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            for node in tree.body
        )
        assert not has_main_guard, f"Expected top-level script without __main__ guard: {rel}"

    missing_tty = tmp_path / "no-such-tty"
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "bin" / "rsp-serial")],
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "RSP_TTY": str(missing_tty)},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert res.returncode != 0
