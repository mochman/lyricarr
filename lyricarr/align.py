from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np

from .tags import TrackMeta

log = logging.getLogger(__name__)

_MODELS: dict[tuple[str, str], tuple] = {}
_SEPARATORS: dict[str, object] = {}
_BRACKETS = str.maketrans("[]", "()")


def pick_device(requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _fmt_tag(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    centi = int(round(seconds * 100))
    m, centi = divmod(centi, 6000)
    s, centi = divmod(centi, 100)
    return f"{m:02d}:{s:02d}.{centi:02d}"


def _separator(device: str):
    """Load the Demucs model once per device and keep it resident."""
    from demucs.api import Separator

    if device not in _SEPARATORS:
        _SEPARATORS.clear()  # only keep one model in memory at a time
        _SEPARATORS[device] = Separator(model="htdemucs", device=device,
                                        progress=False)
    return _SEPARATORS[device]


def _run_demucs(audio: Path, device: str):
    """Return (vocals tensor [channels, samples], samplerate)."""
    import torch

    sep = _separator(device)
    try:
        _, stems = sep.separate_audio_file(audio)
    except torch.cuda.OutOfMemoryError:
        # Long outlier on a small GPU: retry once with smaller chunks.
        # Note: this setting persists for later tracks (slightly slower).
        log.warning("Demucs OOM on %s; retrying with smaller segment", audio.name)
        torch.cuda.empty_cache()
        sep.update_parameter(segment=4)
        _, stems = sep.separate_audio_file(audio)
    return stems["vocals"], sep.samplerate


def _stem_path(audio: Path, work: Path) -> Path:
    # Hash the full path so same-named files in different folders don't collide.
    h = hashlib.sha1(str(audio.resolve()).encode()).hexdigest()[:10]
    return work / f"{audio.stem}.{h}.vocals.wav"


def separate_vocals(audio: Path, device: str, work: Path,
                    keep_stems: bool = False) -> np.ndarray:
    """Demucs two-stem vocal isolation, run in-process.

    Returns the vocals as 16 kHz mono float32, ready for whisperx. If
    `keep_stems` is set, the full-quality stem is also written to `work`
    (and reused on later runs so Demucs is skipped). If the requested
    device is unsupported for Demucs (e.g. MPS), falls back to CPU.
    """
    import torchaudio
    import whisperx
    from whisperx.audio import SAMPLE_RATE

    cache = _stem_path(audio, work) if keep_stems else None
    if cache is not None and cache.exists():
        return whisperx.load_audio(str(cache))

    try:
        vocals, sr = _run_demucs(audio, device)
    except Exception as e:
        # Fall back only for non-CUDA devices; a CUDA failure that isn't OOM
        # (or a corrupt input file) should raise rather than silently run on CPU.
        if device in ("cuda", "cpu"):
            raise
        log.warning("Demucs failed on %s (%s); falling back to CPU", device, e)
        vocals, sr = _run_demucs(audio, "cpu")

    if cache is not None:
        from demucs.api import save_audio

        work.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.stem + ".tmp.wav")
        save_audio(vocals.cpu(), str(tmp), samplerate=sr)
        tmp.replace(cache)  # atomic: no half-written cache files

    mono = vocals.mean(0, keepdim=True)
    mono = torchaudio.functional.resample(mono, sr, SAMPLE_RATE)
    return mono.squeeze(0).cpu().numpy().astype(np.float32)


def _align_words(text: str, start: float, end: float, model, meta,
                 audio_arr, device: str) -> list[dict]:
    import whisperx

    aligned = whisperx.align([{"start": start, "end": end, "text": text}],
                             model, meta, audio_arr, device,
                             return_char_alignments=False)
    return [w for seg in aligned.get("segments", [])
            for w in seg.get("words", []) if w.get("word", "").strip()]


def _align_model(lang: str, device: str) -> tuple:
    import whisperx

    key = (lang, device)
    if key not in _MODELS:
        if len(_MODELS) >= 2:
            _MODELS.pop(next(iter(_MODELS)))
        _MODELS[key] = whisperx.load_align_model(language_code=lang, device=device)
    return _MODELS[key]


def _header(meta: TrackMeta | None, duration: float) -> list[str]:
    fields = [("ar", meta.artist), ("al", meta.album), ("ti", meta.title)] if meta else []
    out = [f"[{k}:{' '.join(v.translate(_BRACKETS).split())}]"
           for k, v in fields if v and v.strip()]
    m, sec = divmod(int(round(duration)), 60)
    out += [f"[length:{m:02d}:{sec:02d}]", "[tool:lyricarr]"]
    return out


def _render_line(t: float, text: str, words: list[dict],
                 floor: float, spaced: bool) -> tuple[str, float]:
    starts = [w["start"] for w in words if w.get("start") is not None]
    if not starts:
        t = max(t, floor)
        return f"[{_fmt_tag(t)}]{text}", t
    last = max(starts[0], floor)
    chunk = f"[{_fmt_tag(last)}]"
    for w in words:
        ts = w.get("start")
        last = last if ts is None else max(ts, last)
        chunk += f"<{_fmt_tag(last)}>{w['word'].strip()}" + (" " if spaced else "")
    chunk = chunk.rstrip()
    end = words[-1].get("end")
    if end is not None and end > last:
        chunk += f"<{_fmt_tag(end)}>"
    return chunk, last


def generate_elrc(audio: Path, lines: list[tuple[float, str]],
                  device: str, work: Path, lang: str = "en",
                  separate: bool = True, keep_stems: bool = False,
                  meta: TrackMeta | None = None) -> str | None:
    """Align lyric `lines` to `audio` and return an enhanced-LRC string."""
    import whisperx
    from whisperx.alignment import LANGUAGES_WITHOUT_SPACES
    from whisperx.audio import SAMPLE_RATE

    # With separation, the vocals stay in memory: nothing temporary is written
    # to disk, so there is nothing to clean up and a failure costs only this track.
    audio_arr = (separate_vocals(audio, device, work, keep_stems) if separate
                 else whisperx.load_audio(str(audio)))
    duration = len(audio_arr) / SAMPLE_RATE

    align_device = "cuda" if device == "cuda" else "cpu"
    model, align_meta = _align_model(lang, align_device)
    spaced = lang not in LANGUAGES_WITHOUT_SPACES

    out = _header(meta, duration)
    floor = 0.0
    for i, (t, text) in enumerate(lines):
        if not text:
            floor = max(t, floor)
            out.append(f"[{_fmt_tag(floor)}]")
            continue
        end = next((n for n, _ in lines[i + 1:] if n > t), duration)
        words = (_align_words(text, t, end, model, align_meta, audio_arr, align_device)
                 if end > t else [])
        line, floor = _render_line(t, text, words, floor, spaced)
        out.append(line)
    return "\n".join(out) + "\n" if any(text for _, text in lines) else None
