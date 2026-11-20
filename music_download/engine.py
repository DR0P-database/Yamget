import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

logger = logging.getLogger(__name__)


class Aria2Error(OSError):
    """Sanitized process, transport or RPC failure."""


class DownloadCancelled(Exception):
    pass


@dataclass(frozen=True)
class DownloadProgress:
    gid: str
    status: str
    completed_bytes: int
    total_bytes: int | None
    bytes_per_second: int


class Aria2Engine:
    def __init__(
        self,
        executable: str = "aria2c",
        *,
        startup_timeout: float = 10,
        rpc_timeout: float = 2,
        poll_interval: float = 0.2,
    ):
        """Создать движок; процесс aria2 запускается отдельно через start().

        :param executable: Имя aria2c для поиска в PATH или путь к executable.
        :param startup_timeout: Время ожидания готовности RPC после запуска
            процесса, в секундах. По умолчанию 10.
        :param rpc_timeout: Таймаут сетевых операций отдельного RPC-запроса,
            в секундах. По умолчанию 2; при запуске ограничивается
            оставшимся временем ожидания готовности.
        :param poll_interval: Интервал между проверками готовности RPC и между
            опросами состояния загрузки, в секундах. По умолчанию 0.2.

        HTTP-клиент управления обращается к loopback напрямую: ProxyHandler({})
        отключает использование прокси из окружения для RPC-запросов.
        """
        self.executable = executable
        self.startup_timeout = startup_timeout
        self.rpc_timeout = rpc_timeout
        self.poll_interval = poll_interval
        self._process: subprocess.Popen | None = None
        self._config: tempfile.TemporaryDirectory | None = None
        self._secret = ""
        self._endpoint = ""
        # Local control must never go through an environment-configured proxy.
        self._opener = build_opener(ProxyHandler({}))

    def check_available(self) -> None:
        """Проверить наличие исполняемого файла aria2.

        :raises Aria2Error: Исполняемый файл не найден в PATH или по заданному пути.
        """
        logger.debug("Проверка доступности aria2c")
        if shutil.which(self.executable) is None:
            logger.error("Исполняемый файл aria2c не найден")
            raise Aria2Error("Не найден aria2c. Установите aria2 и добавьте его в PATH")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def start(self) -> None:
        """Запустить aria2 и дождаться готовности локального RPC.

        Повторный вызов для работающего процесса ничего не меняет.

        :raises Aria2Error: aria2 недоступен, завершился или не ответил вовремя.
        :raises OSError: Не удалось выделить порт, создать конфиг или процесс.
        """
        if self._process is not None:
            if self._process.poll() is not None:
                raise Aria2Error("Процесс aria2 завершился")
            return
        logger.info("Запуск процесса aria2")
        self.check_available()
        # aria2 needs a concrete port; a bind conflict is detected at startup.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self._secret = secrets.token_hex(32)
        self._endpoint = f"http://127.0.0.1:{port}/jsonrpc"
        self._config = tempfile.TemporaryDirectory(prefix="yamget-aria2-")
        config = Path(self._config.name) / "aria2.conf"
        config.touch(mode=0o600)
        config.write_text(f"rpc-secret={self._secret}\n", encoding="utf-8")
        command = [
            self.executable,
            f"--conf-path={config}",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            "--rpc-allow-origin-all=false",
            f"--rpc-listen-port={port}",
            "--disable-ipv6=true",
            "--daemon=false",
            "--stop-with-process=" + str(os.getpid()),
            "--max-concurrent-downloads=1",
            "--max-tries=3",
            "--retry-wait=2",
            "--connect-timeout=15",
            "--timeout=60",
            "--summary-interval=0",
            "--console-log-level=error",
            "--download-result=hide",
            "--file-allocation=none",
            "--max-connection-per-server=4",
            "--split=4",
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + self.startup_timeout
            while True:
                try:
                    self._rpc(
                        "getVersion",
                        timeout=min(self.rpc_timeout, max(0.01, deadline - time.monotonic())),
                    )
                    logger.info("RPC aria2 готов")
                    return
                except Aria2Error:
                    if self._process.poll() is not None:
                        raise Aria2Error(
                            "aria2 не запустился: проверьте executable и RPC-порт"
                        ) from None
                    if time.monotonic() >= deadline:
                        raise Aria2Error("Истекло время ожидания готовности RPC aria2") from None
                    time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        except BaseException:
            self.close()
            raise

    def _rpc(self, method: str, *params, timeout: float | None = None):
        """Выполнить авторизованный запрос к локальному RPC aria2.

        :param method: Имя RPC-метода без префикса aria2.
        :param *params: Позиционные параметры RPC после токена авторизации.
        :param timeout: Таймаут запроса в секундах; None использует rpc_timeout.

        :returns: Значение поля result из ответа JSON-RPC.

        :raises Aria2Error: Процесс недоступен, запрос отклонён или ответ некорректен.
        """
        if self._process is None or self._process.poll() is not None:
            raise Aria2Error("Процесс aria2 не запущен или завершился")
        if method != "tellStatus":
            logger.debug("RPC aria2: %s", method)
        request_id = secrets.token_hex(8)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": f"aria2.{method}",
            "params": [f"token:{self._secret}", *params],
        }
        request = Request(
            self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=timeout or self.rpc_timeout) as response:
                result = json.load(response)
        except (OSError, URLError, ValueError):
            raise Aria2Error("Нет связи с RPC aria2") from None
        if not isinstance(result, dict) or result.get("id") != request_id:
            raise Aria2Error("Некорректный ответ RPC aria2")
        if "error" in result:
            # aria2 error messages can contain signed URLs. Never expose them.
            raise Aria2Error(f"RPC aria2 отклонил операцию {method}")
        if "result" not in result:
            raise Aria2Error("В ответе RPC aria2 отсутствует результат")
        return result["result"]

    def add(self, url: str, target: Path) -> str:
        """Добавить загрузку в работающий движок.

        :param url: Прямая HTTP(S)-ссылка на аудиофайл.
        :param target: Путь файла назначения; передаётся aria2 как абсолютный путь.

        :returns: GID, назначенный загрузке aria2.

        :raises ValueError: Ссылка некорректна или использует другую схему.
        :raises Aria2Error: Ошибка RPC или отсутствие GID в ответе.
        """
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(char in url for char in "\r\n")
        ):
            raise ValueError("aria2 ожидает корректную HTTP(S)-ссылку")
        target = target.resolve()
        gid = self._rpc(
            "addUri",
            [url],
            {
                "dir": str(target.parent),
                "out": target.name,
                "allow-overwrite": "true",
                "auto-file-renaming": "false",
                "continue": "false",
            },
        )
        if not isinstance(gid, str) or not gid:
            raise Aria2Error("aria2 не вернул GID загрузки")
        logger.debug("Передача добавлена: gid=%s", gid)
        return gid

    def status(self, gid: str) -> DownloadProgress:
        """Получить текущее состояние загрузки.

        :param gid: Идентификатор загрузки aria2.
        :returns: Статус, загруженные/общие байты и скорость в байтах в секунду.
            Неизвестный или нулевой общий размер представлен как None.
        :raises Aria2Error: Ошибка RPC или некорректные данные прогресса.
        """
        data = self._rpc(
            "tellStatus",
            gid,
            ["status", "completedLength", "totalLength", "downloadSpeed", "errorCode"],
        )
        try:
            return DownloadProgress(
                gid,
                data["status"],
                int(data["completedLength"]),
                int(data["totalLength"]) or None,
                int(data["downloadSpeed"]),
            )
        except (KeyError, TypeError, ValueError):
            raise Aria2Error("Некорректное состояние загрузки aria2") from None

    def pause(self, gid: str) -> None:
        """Запросить принудительную паузу загрузки.

        :param gid: Идентификатор загрузки aria2.
        :raises Aria2Error: RPC недоступен или операция отклонена.
        """
        logger.debug("Пауза передачи: gid=%s", gid)
        self._rpc("forcePause", gid)

    def resume(self, gid: str) -> None:
        """Запросить продолжение загрузки.

        :param gid: Идентификатор приостановленной загрузки aria2.
        :raises Aria2Error: RPC недоступен или операция отклонена.
        """
        logger.debug("Продолжение передачи: gid=%s", gid)
        self._rpc("unpause", gid)

    def cancel(self, gid: str) -> None:
        """Запросить принудительное удаление загрузки из aria2.

        :param gid: Идентификатор загрузки aria2.
        :raises Aria2Error: RPC недоступен или операция отклонена.
        """
        logger.debug("Отмена передачи: gid=%s", gid)
        self._rpc("forceRemove", gid)

    def download(
        self, url: str, target: Path, on_progress: Callable[[DownloadProgress], None] | None = None
    ) -> None:
        """Скачать файл и опрашивать прогресс до завершения.

        :param url: Прямая HTTP(S)-ссылка.
        :param target: Путь файла назначения.
        :param on_progress: Callback с DownloadProgress при каждом опросе,
            включая конечный статус. Исключения callback передаются наружу.

        :raises DownloadCancelled: aria2 сообщил об удалении загрузки.
        :raises Aria2Error: Ошибка RPC, передачи или отсутствие непустого файла.
        :raises OSError: Ошибка проверки файла назначения.

        Движок должен быть запущен заранее. При выходе незавершённая
        передача отменяется, а результат удаляется из памяти aria2.
        """
        gid = self.add(url, target)
        terminal = False
        try:
            while True:
                progress = self.status(gid)
                terminal = progress.status in {"complete", "error", "removed"}
                if on_progress:
                    on_progress(progress)
                if terminal:
                    logger.debug(
                        "Передача завершена: gid=%s, статус=%s, байт=%d",
                        gid,
                        progress.status,
                        progress.completed_bytes,
                    )
                    if progress.status == "removed":
                        raise DownloadCancelled("Загрузка отменена")
                    if progress.status == "error":
                        raise Aria2Error("aria2 сообщил об ошибке загрузки")
                    if not target.is_file() or target.stat().st_size == 0:
                        raise Aria2Error("aria2 завершил загрузку без непустого файла")
                    return
                time.sleep(self.poll_interval)
        finally:
            try:
                if not terminal:
                    current = self.status(gid)
                    if current.status not in {"complete", "error", "removed"}:
                        self.cancel(gid)
                self._rpc("removeDownloadResult", gid)  # Не удаляет из ОС
            except Aria2Error:
                # Stop the writer before the service deletes a temporary file.
                if not terminal:
                    self.close()

    def close(self) -> None:
        """Завершить процесс aria2 и удалить временный конфиг.

        Если RPC-завершение не сработало, используются terminate и kill.
        Повторный вызов после закрытия допустим.
        """
        process = self._process
        if process is not None:
            logger.info("Остановка процесса aria2")
        try:
            if process is not None:
                if process.poll() is None:
                    try:
                        self._rpc("forceShutdown")
                    except Aria2Error:
                        logger.warning("RPC-завершение aria2 не удалось, отправляется terminate")
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.warning("aria2 не завершился вовремя, отправляется kill")
                        process.kill()
                        process.wait()
                else:
                    process.wait()
        finally:
            self._process = None
            self._secret = ""
            self._endpoint = ""
            if self._config is not None:
                self._config.cleanup()
                self._config = None
