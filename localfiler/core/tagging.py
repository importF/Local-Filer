"""Read and write audio tags and cover art.

``read_tags`` / ``read_cover`` / ``write_tags`` dispatch on the file extension,
covering MP3 (ID3), M4A/MP4, FLAC, Opus, and Ogg Vorbis.
"""

from __future__ import annotations

import base64
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TRCK, USLT, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

_MP4_EXTS = (".m4a", ".mp4", ".aac", ".m4b")
_FLAC_EXTS = (".flac",)
_OPUS_EXTS = (".opus",)
_OGG_EXTS = (".ogg", ".oga")


def _kind(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in _MP4_EXTS:
        return "mp4"
    if ext in _FLAC_EXTS:
        return "flac"
    if ext in _OPUS_EXTS:
        return "opus"
    if ext in _OGG_EXTS:
        return "ogg"
    return "id3"


# ---------------------------------------------------------------- public API
def read_tags(path: str | Path) -> dict:
    """Return existing {title, artist, album, year, track} (empty values if none)."""
    kind = _kind(path)
    if kind == "mp4":
        return _read_mp4(path)
    if kind == "flac":
        return _read_vorbis(_open_safe(FLAC, path))
    if kind == "opus":
        return _read_vorbis(_open_safe(OggOpus, path))
    if kind == "ogg":
        return _read_vorbis(_open_safe(OggVorbis, path))
    return _read_id3(path)


def read_cover(path: str | Path) -> bytes | None:
    """Return embedded cover image bytes, or None."""
    kind = _kind(path)
    if kind == "mp4":
        return _read_cover_mp4(path)
    if kind == "flac":
        return _read_cover_flac(path)
    if kind in ("opus", "ogg"):
        opener = OggOpus if kind == "opus" else OggVorbis
        return _read_cover_vorbis(_open_safe(opener, path))
    return _read_cover_id3(path)


def write_tags(
    path: str | Path,
    meta,
    cover_path: str | Path | None = None,
    remove_cover: bool = False,
) -> None:
    """Write metadata and embed/remove the cover image.

    ``cover_path`` embeds that image (replacing any existing cover); otherwise
    ``remove_cover`` strips any embedded cover. The track number is written when
    ``meta.track`` is set and cleared when empty.
    """
    kind = _kind(path)
    if kind == "mp4":
        _write_mp4(path, meta, cover_path, remove_cover)
    elif kind == "flac":
        _write_flac(path, meta, cover_path, remove_cover)
    elif kind == "opus":
        _write_ogg(OggOpus, path, meta, cover_path, remove_cover)
    elif kind == "ogg":
        _write_ogg(OggVorbis, path, meta, cover_path, remove_cover)
    else:
        _write_id3(path, meta, cover_path, remove_cover)


# ------------------------------------------------------------------- ID3/MP3
def _read_id3(path: str | Path) -> dict:
    try:
        tags = ID3(str(path))
    except (ID3NoHeaderError, Exception):  # noqa: BLE001
        return {}

    def text(frame_id: str) -> str:
        frame = tags.get(frame_id)
        if frame and getattr(frame, "text", None):
            return str(frame.text[0])
        return ""

    return {
        "title": text("TIT2"),
        "artist": text("TPE1"),
        "album": text("TALB"),
        "year": text("TDRC"),
        "track": text("TRCK"),
    }


def _read_cover_id3(path: str | Path) -> bytes | None:
    try:
        tags = ID3(str(path))
    except (ID3NoHeaderError, Exception):  # noqa: BLE001
        return None
    frames = tags.getall("APIC")
    if frames and frames[0].data:
        return bytes(frames[0].data)
    return None


def _write_id3(path, meta, cover_path, remove_cover) -> None:
    path = str(path)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    if meta.title:
        tags.setall("TIT2", [TIT2(encoding=3, text=meta.title)])
    if meta.artist:
        tags.setall("TPE1", [TPE1(encoding=3, text=meta.artist)])
    if meta.album:
        tags.setall("TALB", [TALB(encoding=3, text=meta.album)])
    if meta.year:
        tags.setall("TDRC", [TDRC(encoding=3, text=meta.year)])

    track = (getattr(meta, "track", "") or "").strip()
    if track:
        tags.setall("TRCK", [TRCK(encoding=3, text=track)])
    else:
        tags.delall("TRCK")  # clearing the field removes the track number

    if getattr(meta, "lyrics", ""):
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="eng", desc="", text=meta.lyrics))

    if cover_path:
        cover_path = Path(cover_path)
        data = cover_path.read_bytes()
        mime = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    elif remove_cover:
        tags.delall("APIC")

    tags.save(path, v2_version=3)


# ------------------------------------------------------------------- MP4/M4A
def _read_mp4(path: str | Path) -> dict:
    try:
        audio = MP4(str(path))
    except Exception:  # noqa: BLE001
        return {}

    def text(key: str) -> str:
        val = audio.get(key)
        return str(val[0]) if val else ""

    track = ""
    trkn = audio.get("trkn")
    if trkn and trkn[0] and trkn[0][0]:
        track = str(trkn[0][0])

    return {
        "title": text("\xa9nam"),
        "artist": text("\xa9ART"),
        "album": text("\xa9alb"),
        "year": text("\xa9day"),
        "track": track,
    }


def _read_cover_mp4(path: str | Path) -> bytes | None:
    try:
        audio = MP4(str(path))
    except Exception:  # noqa: BLE001
        return None
    covers = audio.get("covr")
    return bytes(covers[0]) if covers else None


def _write_mp4(path, meta, cover_path, remove_cover) -> None:
    audio = MP4(str(path))

    if meta.title:
        audio["\xa9nam"] = [meta.title]
    if meta.artist:
        audio["\xa9ART"] = [meta.artist]
    if meta.album:
        audio["\xa9alb"] = [meta.album]
    if meta.year:
        audio["\xa9day"] = [meta.year]

    track = (getattr(meta, "track", "") or "").strip()
    if track:
        digits = "".join(ch for ch in track.split("/")[0] if ch.isdigit())
        if digits:
            audio["trkn"] = [(int(digits), 0)]
    elif "trkn" in audio:
        del audio["trkn"]

    if cover_path:
        cover_path = Path(cover_path)
        data = cover_path.read_bytes()
        fmt = (
            MP4Cover.FORMAT_PNG
            if cover_path.suffix.lower() == ".png"
            else MP4Cover.FORMAT_JPEG
        )
        audio["covr"] = [MP4Cover(data, imageformat=fmt)]
    elif remove_cover and "covr" in audio:
        del audio["covr"]

    audio.save()


# ---------------------------------------------- FLAC / Opus / Ogg (Vorbis comments)
def _open_safe(opener, path):
    """Open ``path`` with ``opener``; return None on failure (mirrors ID3 reads)."""
    try:
        return opener(str(path))
    except Exception:  # noqa: BLE001
        return None


def _read_vorbis(audio) -> dict:
    """Read Vorbis-comment tags (shared by FLAC, Opus and Ogg Vorbis)."""
    if audio is None:
        return {}

    def first(key: str) -> str:
        val = audio.get(key)
        return str(val[0]) if val else ""

    return {
        "title": first("title"),
        "artist": first("artist"),
        "album": first("album"),
        "year": first("date"),
        "track": first("tracknumber"),
    }


def _apply_vorbis_text(audio, meta) -> None:
    if meta.title:
        audio["title"] = meta.title
    if meta.artist:
        audio["artist"] = meta.artist
    if meta.album:
        audio["album"] = meta.album
    if meta.year:
        audio["date"] = meta.year

    track = (getattr(meta, "track", "") or "").strip()
    if track:
        audio["tracknumber"] = track
    elif "tracknumber" in audio:
        del audio["tracknumber"]

    if getattr(meta, "lyrics", ""):
        audio["lyrics"] = meta.lyrics


def _make_picture(cover_path: Path) -> Picture:
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
    pic.data = cover_path.read_bytes()
    return pic


def _write_ogg(opener, path, meta, cover_path, remove_cover) -> None:
    audio = opener(str(path))
    _apply_vorbis_text(audio, meta)

    if cover_path:
        pic = _make_picture(Path(cover_path))
        # Opus/Ogg store cover art as a base64 FLAC picture block.
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    elif remove_cover and "metadata_block_picture" in audio:
        del audio["metadata_block_picture"]

    audio.save()


def _read_cover_vorbis(audio) -> bytes | None:
    if audio is None:
        return None
    blocks = audio.get("metadata_block_picture")
    if not blocks:
        return None
    try:
        pic = Picture(base64.b64decode(blocks[0]))
        return bytes(pic.data)
    except Exception:  # noqa: BLE001
        return None


def _write_flac(path, meta, cover_path, remove_cover) -> None:
    audio = FLAC(str(path))
    _apply_vorbis_text(audio, meta)

    if cover_path:
        audio.clear_pictures()
        audio.add_picture(_make_picture(Path(cover_path)))
    elif remove_cover:
        audio.clear_pictures()

    audio.save()


def _read_cover_flac(path) -> bytes | None:
    audio = _open_safe(FLAC, path)
    if audio is None or not audio.pictures:
        return None
    return bytes(audio.pictures[0].data)
