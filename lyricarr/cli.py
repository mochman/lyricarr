"""Lyricarr — generate word-synced (.lrc) lyric sidecars for a music library.

Point it at a folder, for every track missing a sidecar it fetches the lyric
text from LRCLIB and force-aligns it to the audio, writing `<track>.lrc` next
to the file. Any media server that reads .lrc sidecars (Jellyfin, Navidrome,
Plex) then picks them up.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import lrclib
from .language import alignable, detect_language
from .lyrics_cache import LyricsCache, STATUS_NO_LYRICS, STATUS_NO_SYNCED
from .tags import read_meta, scan_library


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lyricarr", description=__doc__)
    ap.add_argument("library", nargs="?", type=Path,
                    default=os.environ.get("LYRICARR_LIBRARY"),
                    help="path to the music library root (env LYRICARR_LIBRARY)")
    ap.add_argument("--device", default=os.environ.get("LYRICARR_DEVICE", "auto"),
                    help="auto|cuda|mps|cpu (env LYRICARR_DEVICE)")
    ap.add_argument("--lang", default=os.environ.get("LYRICARR_LANG", "auto"),
                    help="alignment language code, or auto to detect per track (env LYRICARR_LANG)")
    ap.add_argument("--fallback-lang", default=os.environ.get("LYRICARR_FALLBACK_LANG", "en"),
                    help="language used when auto-detection fails or is unsupported "
                         "(env LYRICARR_FALLBACK_LANG)")
    ap.add_argument("--overwrite", action="store_true", default=_env_bool("LYRICARR_OVERWRITE"),
                    help="regenerate existing sidecars (env LYRICARR_OVERWRITE)")
    ap.add_argument("--upgrade", action="store_true", default=_env_bool("LYRICARR_UPGRADE"),
                    help="regenerate sidecars not written by this version of Lyricarr, "
                         "skipping ones that already are (env LYRICARR_UPGRADE)")
    ap.add_argument("--no-separate", action="store_true", default=_env_bool("LYRICARR_NO_SEPARATE"),
                    help="skip Demucs vocal isolation (env LYRICARR_NO_SEPARATE)")
    ap.add_argument("--keep-stems", action="store_true", default=_env_bool("LYRICARR_KEEP_STEMS"),
                    help="keep isolated vocal stems instead of deleting them (env LYRICARR_KEEP_STEMS)")
    ap.add_argument("--limit", type=int, default=int(os.environ.get("LYRICARR_LIMIT", "0")),
                    help="process at most N tracks (env LYRICARR_LIMIT)")
    ap.add_argument("--interval", type=int, default=int(os.environ.get("LYRICARR_INTERVAL", "0")),
                    help="seconds between repeated scans; 0 = run once (env LYRICARR_INTERVAL)")
    ap.add_argument("--dry-run", action="store_true", default=_env_bool("LYRICARR_DRY_RUN"),
                    help="scan + check LRCLIB coverage only; no alignment or writes "
                         "(the lookup cache is read but not updated)")
    ap.add_argument("--work", type=Path,
                    default=Path(os.environ.get("LYRICARR_WORK", "/tmp/lyricarr")),
                    help="scratch dir for vocal stems (env LYRICARR_WORK)")
    # --- lookup cache -------------------------------------------------------
    ap.add_argument("--force-recheck", action="store_true",
                    default=_env_bool("LYRICARR_FORCE_RECHECK"),
                    help="ignore the lookup cache and query LRCLIB again for every track "
                         "(first scan only when --interval is used) "
                         "(env LYRICARR_FORCE_RECHECK)")
    ap.add_argument("--cache-db", type=Path,
                    default=Path(os.environ.get("LYRICARR_CACHE_DB", "/app/db/cache.db")),
                    help="SQLite file remembering tracks with no synced lyrics on LRCLIB; "
                         "put it on a persistent volume in Docker (env LYRICARR_CACHE_DB)")
    ap.add_argument("--retry-days", type=int,
                    default=int(os.environ.get("LYRICARR_RETRY_DAYS", "30")),
                    help="days before a cached 'no synced lyrics' result is checked again "
                         "(env LYRICARR_RETRY_DAYS)")
    return ap


def _is_current(sidecar: Path) -> bool:
    try:
        head = sidecar.read_text(encoding="utf-8", errors="ignore")[:512]
    except OSError:
        return False
    return "[tool:lyricarr]" in head and "[length:" in head


def _needs_sidecar(sidecar: Path, args) -> bool:
    if args.overwrite or not sidecar.exists():
        return True
    return args.upgrade and not _is_current(sidecar)


def _remember(cache: LyricsCache, args, path: Path, status: str) -> None:
    """Record a negative LRCLIB result (skipped in dry-run so nothing is written)."""
    if not args.dry_run:
        cache.record(path, status)


def _run_once(args, generate_elrc, device: str, cache: LyricsCache) -> None:
    files = scan_library(args.library)
    extensions = [".lrc", ".elrc", ".ttml"]
    candidates = [f for f in files
                  if all(_needs_sidecar(f.with_suffix(ext), args) for ext in extensions)]
    have = len(files) - len(candidates)

    # Drop tracks already known to have no synced lyrics. Done before --limit so
    # cached tracks don't use up the limit.
    todo = [f for f in candidates if not cache.should_skip(f)]
    cached = len(candidates) - len(todo)

    print(f"Found {len(files)} audio files; {have} already have sidecars; "
          f"{cached} skipped (no synced lyrics, cached); "
          f"{len(todo)} to process" + (f" (limit {args.limit})" if args.limit else ""),
          flush=True)
    if args.limit:
        todo = todo[:args.limit]

    done = nolyrics = failed = 0
    for i, path in enumerate(todo, 1):
        meta = read_meta(path)
        if not meta or not meta.title:
            print(f"[{i}/{len(todo)}] ? no tags: {path.name}", flush=True)
            failed += 1
            continue
        label = f"{meta.artist} — {meta.title}"
        try:
            found = lrclib.fetch_lines(meta.artist, meta.title, meta.album, meta.duration)
        except Exception as e:
            # Network/API error: do NOT cache, so it is retried next run.
            print(f"[{i}/{len(todo)}] ✗ lookup error: {label}: {e}", flush=True)
            failed += 1
            continue
        if not found:
            print(f"[{i}/{len(todo)}] – no lyrics: {label}", flush=True)
            _remember(cache, args, path, STATUS_NO_LYRICS)
            nolyrics += 1
            continue
        kind, lines = found
        if kind != "synced":
            print(f"[{i}/{len(todo)}] – unsynced lyrics only: {label}", flush=True)
            _remember(cache, args, path, STATUS_NO_SYNCED)
            nolyrics += 1
            continue
        lang = args.lang
        if lang == "auto":
            lang = detect_language(lines) or args.fallback_lang
        if args.dry_run:
            print(f"[{i}/{len(todo)}] ✓ [{lang}] {label} ({len(lines)} lines)", flush=True)
            done += 1
            continue
        if not alignable(lang):
            lang = args.fallback_lang
        try:
            elrc = generate_elrc(path, lines, device, args.work, lang,
                                 separate=not args.no_separate, keep_stems=args.keep_stems,
                                 meta=meta)
            if not elrc:
                print(f"[{i}/{len(todo)}] – align empty: {label}", flush=True)
                failed += 1
                continue
            meta.sidecar.write_text(elrc, encoding="utf-8")
            print(f"[{i}/{len(todo)}] ✓ [{lang}] {label}", flush=True)
            done += 1
        except Exception as e:
            print(f"[{i}/{len(todo)}] ✗ {label}: {e}", flush=True)
            failed += 1

    verb = "would generate" if args.dry_run else "generated"
    print(f"Done. {verb} {done}; {nolyrics} without lyrics; {failed} failed.", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.library:
        print("No library path (positional arg or LYRICARR_LIBRARY).", file=sys.stderr)
        return 2
    args.library = Path(args.library)
    if not args.library.is_dir():
        print(f"Not a directory: {args.library}", file=sys.stderr)
        return 2

    generate_elrc = None
    device = "auto"
    if not args.dry_run:
        from .align import pick_device, generate_elrc as _gen
        generate_elrc = _gen
        device = pick_device(args.device)
        print(f"Lyricarr device: {device}", flush=True)

    cache = LyricsCache(db_path=args.cache_db,
                        retry_after_days=args.retry_days,
                        force_recheck=args.force_recheck)
    try:
        while True:
            _run_once(args, generate_elrc, device, cache)
            # With --interval, only force a full re-check on the first pass;
            # otherwise every repeat scan would hit LRCLIB for every track again.
            cache.force_recheck = False
            if args.interval <= 0:
                return 0
            print(f"Sleeping {args.interval}s before next scan...", flush=True)
            time.sleep(args.interval)
    finally:
        cache.close()


if __name__ == "__main__":
    raise SystemExit(main())
