"""Local HTTP transfers with a real aria2 process, no music account required."""
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from music_download.engine import Aria2Engine, Aria2Error, DownloadCancelled


@pytest.fixture
def local_transfers():
    executable = os.environ.get("YAMGET_TEST_ARIA2") or shutil.which("aria2c")
    if not executable:
        pytest.skip("aria2c is not installed")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/missing":
                self.send_error(404)
                return
            count = 128 if self.path == "/slow" else 4
            self.send_response(200)
            self.send_header("Content-Length", str(count * 4096))
            self.end_headers()
            try:
                for _ in range(count):
                    self.wfile.write(b"a" * 4096)
                    self.wfile.flush()
                    if self.path == "/slow":
                        time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield executable, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_two_transfers_share_process_and_exit_cleanly(local_transfers, tmp_path):
    executable, base = local_transfers
    engine = Aria2Engine(executable, poll_interval=0.05)
    with engine:
        process = engine._process
        engine.download(base + "/first", tmp_path / "first")
        engine.download(base + "/second", tmp_path / "second")
        assert engine._process is process
        assert (tmp_path / "first").read_bytes() == b"a" * 16384
        assert (tmp_path / "second").read_bytes() == b"a" * 16384
    assert process.poll() is not None


def test_pause_resume_cancel_and_error(local_transfers, tmp_path):
    executable, base = local_transfers
    with Aria2Engine(executable, poll_interval=0.05) as engine:
        gid = engine.add(base + "/slow", tmp_path / "slow")
        engine.pause(gid)
        assert engine.status(gid).status == "paused"
        engine.resume(gid)
        assert engine.status(gid).status in {"active", "waiting"}
        engine.cancel(gid)
        assert engine.status(gid).status == "removed"
        engine._rpc("removeDownloadResult", gid)
        with pytest.raises(Aria2Error, match="ошибке загрузки"):
            engine.download(base + "/missing", tmp_path / "missing")
        # A failed job must not prevent the next transfer.
        engine.download(base + "/ok", tmp_path / "ok")


def test_interrupt_removes_transfer_but_keeps_engine(local_transfers, tmp_path):
    executable, base = local_transfers
    with Aria2Engine(executable, poll_interval=0.05) as engine:
        seen = []

        def interrupt(progress):
            seen.append(progress.gid)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            engine.download(base + "/slow", tmp_path / "slow", interrupt)
        assert engine._rpc("tellActive") == []
        engine.download(base + "/ok", tmp_path / "ok")
        assert seen


def test_cancel_through_callback_preserves_next_job(local_transfers, tmp_path):
    executable, base = local_transfers
    with Aria2Engine(executable, poll_interval=0.05) as engine:
        def cancel(progress):
            engine.cancel(progress.gid)
            raise DownloadCancelled()

        with pytest.raises(DownloadCancelled):
            engine.download(base + "/slow", tmp_path / "slow", cancel)
        engine.download(base + "/ok", tmp_path / "ok")
