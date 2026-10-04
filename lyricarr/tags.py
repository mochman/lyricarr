from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}


@dataclass
class TrackMeta:
    path: Path
    title: str
    artist: str
    album: str
    duration: float  # seconds

    @property
    def sidecar(self) -> Path:
        return self.path.with_suffix(".elrc")


def _first(tags, *keys) -> str:
    for k in keys:
        v = tags.get(k)
        if v:
            return str(v[0] if isinstance(v, list) else v).strip()
    return ""


def read_meta(path: Path) -> TrackMeta | None:
    try:
        mf = MutagenFile(path, easy=True)
    except Exception:
        mf = None
    if mf is None:
        return None
    tags = mf.tags or {}
    title = _first(tags, "title") or path.stem
    artist = _first(tags, "artist", "albumartist")
    album = _first(tags, "album")
    duration = float(getattr(mf.info, "length", 0.0) or 0.0)
    return TrackMeta(path=path, title=title, artist=artist, album=album,
                     duration=duration)


def scan_library(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.suffix.lower() in AUDIO_EXTS and p.is_file())
