# Lyricarr

Forked from [Nathan1258](https://github.com/Nathan1258/lyricarr)

**Word-synced lyrics for your self-hosted music library — automatically.**

Point Lyricarr at your music folder and it
generates **word-by-word (enhanced `.lrc`) sidecars** next to your tracks for
Apple-Music-style karaoke lyrics compatible with **Jellyfin, Navidrome, and Plex**.

It gets the lyric *text* from [LRCLIB](https://lrclib.net) (free, no account)
and produces the timing itself by aligning the words to your actual audio.
No paid lyrics API, no scraping, no account tokens.

## How it works

```
your audio  +  LRCLIB lyric text  ──►  Demucs (isolate vocals)
                                   ──►  WhisperX (force-align words)
                                   ──►  <track>.lrc  (word-timed, next to the file)
```

Because the timing is aligned to *your* file, it stays in sync even when other
sources were timed to a different master/remaster. Your media server picks the
sidecars up on its next library scan.

## Quick start (Docker)

Add the service to your compose file (see `docker-compose.yml`), point `/music`
at the same library your server reads, then:

```bash
docker compose run --rm lyricarr            # one pass over the library
```

- CPU by default (works anywhere).
- **NVIDIA GPU**: use the `ghcr.io/mochman/lyricarr:cuda` image and `--gpus all`
  — Demucs goes from minutes to seconds per track.
- Set `LYRICARR_INTERVAL=86400` and `restart: unless-stopped` to keep it topping
  up new music daily.

## Quick start (native — Mac/Linux, uses GPU)

```bash
pipx install .            # or: pip install .
brew install ffmpeg       # Linux: apt install ffmpeg
lyricarr /path/to/music   # auto-detects cuda / mps / cpu
```

## Configuration

Every flag has a `LYRICARR_*` env var (used by the Docker image):

| Flag | Env | Default | Meaning |
|------|-----|---------|---------|
| `library` | `LYRICARR_LIBRARY` | `/music` (Docker) | music library root |
| `--device` | `LYRICARR_DEVICE` | `auto` | `auto`/`cuda`/`mps`/`cpu` |
| `--lang` | `LYRICARR_LANG` | `auto` | alignment language, or `auto` to detect it per track from the lyrics |
| `--fallback-lang` | `LYRICARR_FALLBACK_LANG` | `en` | used when detection fails or the language has no alignment model |
| `--overwrite` | `LYRICARR_OVERWRITE` | off | regenerate existing sidecars |
| `--upgrade` | `LYRICARR_UPGRADE` | off | regenerate sidecars made by older Lyricarr versions or other tools; safe to stop and resume |
| `--no-separate` | `LYRICARR_NO_SEPARATE` | off | skip Demucs (faster, less accurate) |
| `--limit N` | `LYRICARR_LIMIT` | 0 | cap tracks per run |
| `--interval S` | `LYRICARR_INTERVAL` | 0 | seconds between scans; 0 = once |
| `--dry-run` | `LYRICARR_DRY_RUN` | off | report LRCLIB coverage only |

## Try it safely first

```bash
lyricarr /path/to/music --dry-run     # shows which tracks have lyrics available
lyricarr /path/to/music --limit 5     # generate a handful, then check your server
```

## Server support

| Server | Reads `.lrc` sidecar | Word-by-word |
|--------|:--:|:--:|
| Jellyfin | ✅ | ✅ parses into structured word cues |
| Plex | ✅ (after a library scan) | ✅ serves the raw `.lrc` unchanged — a client parses the word timing |
| Navidrome | ✅ | ✅ from v0.64, via OpenSubsonic `getLyricsBySongId?enhanced=true` (older versions are line-level) |

Notes:
- The sidecar file Lyricarr writes is always full word-level. Jellyfin exposes
  it as parsed word cues, Plex serves the untouched file, and Navidrome v0.64+
  returns word cues to clients that ask for enhanced lyrics.
- Plex needs a **library file scan** to detect newly-added sidecars, and its own
  apps may render line-level — but the full enhanced file is available to any
  client that reads the raw lyric stream.

## Players

The `.lrc` files Lyricarr writes work in any client that reads lyric sidecars.

## Notes & limits

- Not every track is on LRCLIB (instrumentals/obscure releases are skipped).
- Accuracy dips on dense harmonies and heavy overlap.
- Mixed-language libraries work out of the box: each track's language is
  detected from its lyrics and aligned with a matching model. Tracks in a
  language without an alignment model use `--fallback-lang`.
- Tracks that only have unsynced lyrics on LRCLIB are skipped.
- Background/duet labelling isn't detected yet (roadmap).

## License

MIT.
