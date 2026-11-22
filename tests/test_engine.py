import io
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from music_download.engine import Aria2Engine, Aria2Error, DownloadCancelled


def running_engine():
    engine = Aria2Engine(poll_interval=0)
    engine._process = Mock()
    engine._process.poll.return_value = None
    engine._endpoint = "http://127.0.0.1:1234/jsonrpc"
    engine._secret = "private-secret"
    return engine


def test_rpc_serializes_url_and_auth_without_logging(tmp_path):
    engine = running_engine()
    url = 'https://example.org/audio?token=secret&name="track"'

    def respond(request, timeout):
        payload = json.loads(request.data)
        assert payload["method"] == "aria2.addUri"
        assert payload["params"][0] == "token:private-secret"
        assert payload["params"][1] == [url]
        assert payload["params"][2]["out"] == "track.part"
        return io.BytesIO(json.dumps({"id": payload["id"], "result": "gid"}).encode())

    engine._opener.open = respond
    assert engine.add(url, tmp_path / "track.part") == "gid"


@pytest.mark.parametrize("url", ["ftp://host/file", "https://", "https://host/a\nb"])
def test_invalid_url_never_reaches_rpc(tmp_path, url):
    engine = running_engine()
    engine._rpc = Mock()
    with pytest.raises(ValueError):
        engine.add(url, tmp_path / "file")
    engine._rpc.assert_not_called()


def test_rpc_errors_do_not_expose_secret_or_url():
    engine = running_engine()

    def respond(request, timeout):
        return io.BytesIO(json.dumps({"id": json.loads(request.data)["id"],
                            "error": {"message": "https://host/?token=private-secret"}}).encode())

    engine._opener.open = respond
    with pytest.raises(Aria2Error) as error:
        engine.pause("gid")
    assert "private-secret" not in str(error.value)
    assert "https://" not in str(error.value)


def test_process_death_is_explicit():
    engine = running_engine()
    engine._process.poll.return_value = 1
    with pytest.raises(Aria2Error, match="завершился"):
        engine.status("gid")


def test_lifecycle_starts_once_and_cleans_secret_file():
    engine = Aria2Engine(poll_interval=0)
    process = Mock()
    process.poll.return_value = None
    with patch("music_download.engine.shutil.which", return_value="/bin/aria2c"), \
         patch("music_download.engine.subprocess.Popen", return_value=process) as popen, \
         patch.object(engine, "_rpc", return_value={"version": "1.37"}):
        with engine:
            engine.start()
            args = popen.call_args.args[0]
            config = Path(next(arg.split("=", 1)[1] for arg in args
                               if arg.startswith("--conf-path=")))
            assert config.stat().st_mode & 0o777 == 0o600
            assert engine._secret in config.read_text()
            assert all(engine._secret not in arg for arg in args)
            assert "--rpc-listen-all=false" in args
        assert popen.call_count == 1
        process.wait.assert_called_once()
        assert not config.exists()


def test_startup_failure_reaps_process():
    engine = Aria2Engine(startup_timeout=0, poll_interval=0)
    process = Mock()
    process.poll.return_value = None
    with patch("music_download.engine.shutil.which", return_value="aria2c"), \
         patch("music_download.engine.subprocess.Popen", return_value=process), \
         patch.object(engine, "_rpc", side_effect=Aria2Error("offline")), \
         pytest.raises(Aria2Error, match="готовности"):
        engine.start()
    process.terminate.assert_called_once()
    process.wait.assert_called_once()
    assert engine._process is None
    assert engine._config is None


@pytest.mark.parametrize("status", ["complete", "error", "removed"])
def test_terminal_downloads_and_progress(tmp_path, status):
    engine = running_engine()
    target = tmp_path / "track"
    target.write_bytes(b"audio")
    progress = []

    def rpc(method, *args, **kwargs):
        if method == "addUri":
            return "gid"
        if method == "tellStatus":
            return {"status": status, "completedLength": "5", "totalLength": "5",
                    "downloadSpeed": "0"}
        return "OK"

    engine._rpc = Mock(side_effect=rpc)
    if status == "complete":
        engine.download("https://host/audio", target, progress.append)
    else:
        with pytest.raises(DownloadCancelled if status == "removed" else Aria2Error):
            engine.download("https://host/audio", target, progress.append)
    assert progress[0].gid == "gid"
    assert progress[0].completed_bytes == 5
    assert engine._rpc.call_args.args == ("removeDownloadResult", "gid")


def test_keyboard_interrupt_removes_active_download(tmp_path):
    engine = running_engine()
    engine.add = Mock(return_value="gid")
    from music_download.engine import DownloadProgress
    engine.status = Mock(side_effect=[KeyboardInterrupt,
        DownloadProgress("gid", "active", 0, None, 0)])
    engine._rpc = Mock()
    with pytest.raises(KeyboardInterrupt):
        engine.download("https://host/audio", tmp_path / "track")
    assert ("forceRemove", "gid") in [call.args for call in engine._rpc.call_args_list]


def test_hung_process_is_killed_and_reaped():
    import subprocess
    engine = running_engine()
    process = engine._process
    process.wait.side_effect = [subprocess.TimeoutExpired("aria2", 5), 0]
    engine._rpc = Mock()
    engine.close()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
