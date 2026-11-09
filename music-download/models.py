from dataclasses import dataclass
from pathlib import Path
from yandex_music import Track


@dataclass(frozen=True)
class DownloadResult:
    track: Track
    path: Path
    skipped: bool = False
