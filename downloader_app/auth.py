"""Локальное хранение токена и вход через OAuth Device Flow."""

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yandex_music import DeviceCode

TOKEN_KEY = "YANDEX_MUSIC_TOKEN"


def load_local_token(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    for line in reversed(content.splitlines()):
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key.strip() == TOKEN_KEY:
            token = value.split("#", 1)[0].strip().strip("'\"").strip()
            return token or None
    return None


def authorize_and_save(path: Path) -> str:
    from yandex_music import Client

    def show_code(code: "DeviceCode") -> None:
        print(
            f"Откройте {code.verification_url} и введите код: {code.user_code}",
            file=sys.stderr,
            flush=True,
        )

    token = Client().device_auth(on_code=show_code).access_token
    if not token or any(char.isspace() or char in "\"'#" for char in token):
        raise ValueError("Яндекс вернул некорректный токен")

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as env_file:
        if os.name == "posix":
            os.fchmod(env_file.fileno(), 0o600)
        prefix = "\n" if os.fstat(env_file.fileno()).st_size else ""
        env_file.write(f"{prefix}{TOKEN_KEY}={token}\n")
    return token
