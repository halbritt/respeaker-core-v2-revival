"""Shared host-isolated fixtures and module loaders for RESPEAKER E2E tests."""
from __future__ import annotations

import os
import sys

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import functools  # noqa: E402
import http.server  # noqa: E402
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import queue  # noqa: E402
import socketserver  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable, Iterable  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "board"
NEWOS_DIR = REPO_ROOT / "newos"


@functools.lru_cache(maxsize=1)
def unshare_available() -> bool:
    """Return True if unprivileged user namespaces (`unshare -Ur`) work on this host."""
    try:
        res = subprocess.run(
            ["unshare", "-Ur", "true"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return res.returncode == 0
    except OSError:
        return False


def make_fake_ort(probs: float | Iterable[float] = 0.95) -> types.ModuleType:
    """Create a synthetic `onnxruntime` module returning controlled speech probabilities."""
    mod = types.ModuleType("onnxruntime")
    mod.init_calls = []  # type: ignore[attr-defined]
    mod.run_calls = []   # type: ignore[attr-defined]

    class SessionOptions:
        def __init__(self) -> None:
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0

    class InferenceSession:
        def __init__(self, model_path: str, sess_options: Any = None, providers: Any = None) -> None:
            mod.init_calls.append(  # type: ignore[attr-defined]
                {"model_path": model_path, "providers": providers, "opts": sess_options}
            )
            if isinstance(probs, (int, float)):
                self._seq = None
                self._const = float(probs)
            else:
                self._seq = [float(p) for p in probs]
                self._idx = 0
                self._const = 0.0

        def run(self, output_names: Any, feed: dict[str, Any]) -> tuple[np.ndarray, Any]:
            if self._seq is not None:
                if self._idx < len(self._seq):
                    p = self._seq[self._idx]
                    self._idx += 1
                else:
                    p = self._seq[-1] if self._seq else 0.0
            else:
                p = self._const
            mod.run_calls.append({"shape": feed["input"].shape})  # type: ignore[attr-defined]
            return np.array([[p]], dtype=np.float32), feed["state"]

    mod.SessionOptions = SessionOptions  # type: ignore[attr-defined]
    mod.InferenceSession = InferenceSession  # type: ignore[attr-defined]
    return mod


def load_rsp_stt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: dict[str, str] | None = None,
    fake_ort: types.ModuleType | None = None,
    block_ort: bool = False,
) -> types.ModuleType:
    """Import `board/rsp-stt.py` in isolation with tmp_path defaults and env overrides."""
    defaults = {
        "STT_LOG": str(tmp_path / "transcripts.jsonl"),
        "STT_RUN_DIR": str(tmp_path / "run"),
        "STT_REC_DIR": str(tmp_path / "rec"),
        "STT_SERVER": "http://127.0.0.1:1/inference",
        "STT_HTTP": "",
        "STT_HTTP_TOKEN": "",
        "STT_WEBHOOK": "",
        "STT_SAVE_DIR": "",
        "STT_ODAS_CFG": str(BOARD_DIR / "odas-respeaker-core-v2.cfg"),
    }
    if env:
        defaults.update(env)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)

    if fake_ort is not None:
        monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    elif block_ort:
        monkeypatch.setitem(sys.modules, "onnxruntime", None)  # type: ignore[arg-type]

    mod_name = f"rsp_stt_test_{uuid.uuid4().hex}"
    path = BOARD_DIR / "rsp-stt.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return mod


def load_rsp_listen() -> types.ModuleType:
    """Import extensionless `board/rsp-listen` via explicit SourceFileLoader."""
    mod_name = f"rsp_listen_test_{uuid.uuid4().hex}"
    path = BOARD_DIR / "rsp-listen"
    loader = importlib.machinery.SourceFileLoader(mod_name, str(path))
    spec = importlib.util.spec_from_file_location(mod_name, str(path), loader=loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def rsp_stt_loader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., types.ModuleType]:
    """Fixture returning a factory to load `board/rsp-stt.py` with automatic module cleanup."""
    loaded_names: list[str] = []

    def _load(
        env: dict[str, str] | None = None,
        fake_ort: types.ModuleType | None = None,
        block_ort: bool = False,
    ) -> types.ModuleType:
        mod = load_rsp_stt(monkeypatch, tmp_path, env=env, fake_ort=fake_ort, block_ort=block_ort)
        loaded_names.append(mod.__name__)
        return mod

    yield _load
    for name in loaded_names:
        sys.modules.pop(name, None)


@pytest.fixture
def event_server_factory():
    """Start `rsp_stt.EventServer` on an ephemeral `127.0.0.1:0` port and shut down on teardown."""
    servers: list[tuple[Any, types.ModuleType]] = []

    def _start(rsp_stt_mod: types.ModuleType) -> tuple[Any, int]:
        srv = rsp_stt_mod.EventServer(("127.0.0.1", 0), rsp_stt_mod.EventHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        servers.append((srv, rsp_stt_mod))
        return srv, srv.server_address[1]

    yield _start
    for srv, rsp_stt_mod in servers:
        # Unblock any open /events subscriber queues before shutdown
        subs = list(rsp_stt_mod.SUBSCRIBERS)
        rsp_stt_mod.SUBSCRIBERS.clear()
        for q in subs:
            try:
                q.put_nowait({"event": "shutdown"})
            except queue.Full:
                pass
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def whisper_mock_factory():
    """Start an ephemeral localhost Whisper + Webhook HTTP server on `127.0.0.1:0`."""
    servers: list[http.server.HTTPServer] = []

    def _start(response_text: str = "hello world") -> tuple[str, list[dict[str, Any]]]:
        captured: list[dict[str, Any]] = []
        state = {"text": response_text}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                captured.append(
                    {
                        "path": self.path,
                        "content_type": self.headers.get("Content-Type", ""),
                        "body": body,
                    }
                )
                if self.path == "/inference":
                    payload = json.dumps({"text": state["text"]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                elif self.path == "/webhook":
                    self.send_response(204)
                    self.end_headers()
                else:
                    self.send_error(404)

        class ThreadedHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        srv = ThreadedHTTP(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        port = srv.server_address[1]
        return f"http://127.0.0.1:{port}", captured

    yield _start
    for srv in servers:
        srv.shutdown()
        srv.server_close()
