"""Lossless audio metadata stripper.

Every audio file our render lanes produce carries the full ComfyUI graph as
an embedded tag (an MP3 ID3v2 TXXX frame, a FLAC VORBIS_COMMENT block, or an
Ogg Opus comment-header packet) -- model filenames, prompts, the works.
Sharing a render today hands over our model stack. See the internal design-decisions doc
decision 3, "Sharing is a feature, not a cleanup step".

stdlib only, by design (DESIGN.md: "Owning ~2.5k lines of dependency-free
code is the hedge") -- no mutagen, no shelling to ffmpeg. This module is not
wired into server.py; that one-line delegation lands separately.

Public API
----------
strip_audio(data, filename="") -> (bytes, stripped)
    `stripped` is False whenever we did not fully clean the file, and then
    `bytes` is the ORIGINAL, untouched input -- never a partial or guessed
    strip. Callers (the share control) must treat False as "still carries
    the recipe" regardless of what the bytes look like.
strip_video(data, filename="") -> (bytes, stripped)
    Same contract as strip_audio, but the one exception to "stdlib only,
    no shelling to ffmpeg": video containers are not worth a hand-rolled
    parser, so this shells out to `ffmpeg` (argv list, never a shell) for a
    stream-copy remux -- `-c copy` never re-encodes a frame, it only drops
    the container-level metadata/chapters. `ffmpeg` absent, an unsupported
    extension, or any failure -> (data, False), same as every other format
    here.

Format coverage
----------------
flac  -- strips non-essential metadata blocks (VORBIS_COMMENT/APPLICATION/
         PICTURE/PADDING), keeps STREAMINFO/SEEKTABLE/CUESHEET, audio frames
         copied byte-identical.
mp3   -- strips a leading ID3v2 tag and any trailing ID3v1/"TAG+"/APEv2 tag,
         MPEG frames in between copied byte-identical.
opus  -- rewrites the OpusTags page's comment list to empty (vendor string
         kept) and recomputes that one Ogg page's CRC-32. Only handled when
         the comment header fits in a single page immediately following
         OpusHead; anything else bails out to (data, False) rather than
         guess at a multi-page rewrite.
mp4/webm/mov/mkv -- ffmpeg stream-copy remux (-c copy), container metadata
         and chapters dropped, decoded audio/video frames untouched.
anything else, or any parse that does not match expectations -- (data, False).
"""

import os
import shutil
import subprocess
import tempfile

# --- FLAC --------------------------------------------------------------------
# https://xiph.org/flac/format.html#metadata_block_header
_FLAC_KEEP = {0, 3, 5}       # STREAMINFO, SEEKTABLE, CUESHEET
_FLAC_DROP = {1, 2, 4, 6}    # PADDING, APPLICATION, VORBIS_COMMENT, PICTURE


def _strip_flac(data):
    if data[:4] != b"fLaC":
        return None
    pos = 4
    keep = []
    while True:
        if pos + 4 > len(data):
            return None                       # truncated header
        b0 = data[pos]
        last = bool(b0 & 0x80)
        btype = b0 & 0x7F
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        start = pos + 4
        end = start + length
        if end > len(data):
            return None                       # truncated block
        block = data[start:end]
        if btype in _FLAC_KEEP:
            keep.append((btype, block))
        elif btype not in _FLAC_DROP:
            return None                       # reserved/unknown block -- don't guess
        pos = end
        if last:
            break
    if not keep or keep[0][0] != 0:
        return None                           # STREAMINFO must lead; something's off

    out = bytearray(b"fLaC")
    n = len(keep)
    for i, (btype, block) in enumerate(keep):
        is_last = i == n - 1
        out.append((0x80 if is_last else 0) | (btype & 0x7F))
        out += len(block).to_bytes(3, "big")
        out += block
    out += data[pos:]                         # audio frames, byte-identical
    return bytes(out)


# --- MP3 -----------------------------------------------------------------
def _synchsafe(b4):
    if any(x & 0x80 for x in b4):
        return None
    return (b4[0] << 21) | (b4[1] << 14) | (b4[2] << 7) | b4[3]


def _strip_mp3(data):
    if len(data) < 4:
        return None
    start = 0
    if data[:3] == b"ID3":
        if len(data) < 10:
            return None
        flags = data[5]
        size = _synchsafe(data[6:10])
        if size is None:
            return None
        tag_len = 10 + size
        if flags & 0x10:                      # v2.4 footer present
            tag_len += 10
        if tag_len > len(data):
            return None
        start = tag_len

    end = len(data)
    if end - start >= 128 and data[end - 128:end - 125] == b"TAG":
        ext_start = end - 128 - 227
        if ext_start >= start and data[ext_start:ext_start + 4] == b"TAG+":
            end = ext_start                   # extended ID3v1 + ID3v1
        else:
            end = end - 128                   # plain ID3v1

    if end - start >= 32 and data[end - 32:end - 24] == b"APETAGEX":
        footer = data[end - 32:end]
        tag_size = int.from_bytes(footer[12:16], "little")
        gflags = int.from_bytes(footer[20:24], "little")
        has_header = bool(gflags & 0x80000000)
        total = tag_size + (32 if has_header else 0)
        new_end = end - total
        if new_end < start or new_end > end:
            return None
        if has_header and data[new_end:new_end + 8] != b"APETAGEX":
            return None
        end = new_end

    if end - start < 2 or data[start] != 0xFF or (data[start + 1] & 0xE0) != 0xE0:
        return None                           # what remains isn't an MPEG frame -- bail
    return data[start:end]


# --- Opus/Ogg --------------------------------------------------------------
# Ogg CRC-32: poly 0x04c11db7, init 0, no reflection, no final xor -- NOT
# zlib.crc32 (which is reflected). https://xiph.org/ogg/doc/framing.html
def _ogg_crc_table():
    table = []
    poly = 0x04C11DB7
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ poly) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
        table.append(crc)
    return table


_OGG_CRC_TABLE = _ogg_crc_table()


def _ogg_crc32(buf):
    crc = 0
    for b in buf:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc & 0xFFFFFFFF


def _ogg_pages(data):
    pos = 0
    n = len(data)
    pages = []
    while pos < n:
        if data[pos:pos + 4] != b"OggS":
            raise ValueError("bad capture pattern")
        if pos + 27 > n:
            raise ValueError("truncated header")
        if data[pos + 4] != 0:
            raise ValueError("unsupported ogg version")
        flags = data[pos + 5]
        page_segments = data[pos + 26]
        seg_start = pos + 27
        if seg_start + page_segments > n:
            raise ValueError("truncated segment table")
        seg_table = data[seg_start:seg_start + page_segments]
        payload_len = sum(seg_table)
        payload_start = seg_start + page_segments
        if payload_start + payload_len > n:
            raise ValueError("truncated payload")
        pages.append({
            "start": pos, "flags": flags, "seg_table": seg_table,
            "payload_start": payload_start, "payload_len": payload_len,
        })
        pos = payload_start + payload_len
    return pages


def _strip_opus(data):
    try:
        pages = _ogg_pages(data)
    except ValueError:
        return None
    if len(pages) < 2:
        return None
    p0, p1 = pages[0], pages[1]

    head = data[p0["payload_start"]:p0["payload_start"] + p0["payload_len"]]
    if not head.startswith(b"OpusHead"):
        return None
    if p1["flags"] & 0x01:
        return None                           # page1 continues page0's packet -- unexpected

    seg = p1["seg_table"]
    terminators = [i for i, s in enumerate(seg) if s < 255]
    if len(terminators) != 1 or terminators[0] != len(seg) - 1:
        return None                           # not exactly one packet filling page1

    tags = data[p1["payload_start"]:p1["payload_start"] + p1["payload_len"]]
    if not tags.startswith(b"OpusTags") or len(tags) < 12:
        return None
    vendor_len = int.from_bytes(tags[8:12], "little")
    vendor_end = 12 + vendor_len
    if vendor_end > len(tags):
        return None
    vendor = tags[8:vendor_end]               # "OpusTags" + vendor string, kept
    new_tags = b"OpusTags" + vendor + (0).to_bytes(4, "little")  # 0 comments

    new_seg = bytearray()
    remaining = len(new_tags)
    while remaining >= 255:
        new_seg.append(255)
        remaining -= 255
    new_seg.append(remaining)
    if len(new_seg) > 255:
        return None                           # would need a second page -- don't guess

    header = bytearray(27)
    header[0:4] = b"OggS"
    header[4] = 0
    header[5] = data[p1["start"] + 5]                        # original flags
    header[6:14] = data[p1["start"] + 6:p1["start"] + 14]    # granule position
    header[14:18] = data[p1["start"] + 14:p1["start"] + 18]  # serial
    header[18:22] = data[p1["start"] + 18:p1["start"] + 22]  # sequence number
    header[22:26] = b"\x00\x00\x00\x00"                      # CRC placeholder
    header[26] = len(new_seg)
    page1 = bytes(header) + bytes(new_seg) + new_tags
    crc = _ogg_crc32(page1)
    page1 = page1[:22] + crc.to_bytes(4, "little") + page1[26:]

    p1_end = p1["payload_start"] + p1["payload_len"]
    return data[:p1["start"]] + page1 + data[p1_end:]


# --- dispatch ----------------------------------------------------------------
def _ext(filename):
    name = (filename or "").lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def strip_audio(data: bytes, filename: str = "") -> "tuple[bytes, bool]":
    """-> (bytes, stripped). `stripped` is False when we did not fully clean
    it, and then `bytes` is the ORIGINAL, untouched input."""
    try:
        if not data:
            return data, False
        ext = _ext(filename)

        if ext == "flac" or (not ext and data[:4] == b"fLaC"):
            out = _strip_flac(data)
        elif ext == "mp3" or (not ext and data[:3] == b"ID3"):
            out = _strip_mp3(data)
        elif ext in ("opus", "ogg") or (not ext and data[:4] == b"OggS"):
            out = _strip_opus(data)
        else:
            out = None

        if out is not None:
            return out, True
        return data, False
    except Exception:
        return data, False                    # a failed strip must not break the download


# --- video (ffmpeg stream-copy) ---------------------------------------------
_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv")


def strip_video(data: bytes, filename: str = "") -> "tuple[bytes, bool]":
    """-> (bytes, stripped). `stripped` is False when we did not fully clean
    it, and then `bytes` is the ORIGINAL, untouched input.

    Shells out to ffmpeg for a stream-copy remux (`-c copy` -- decode/encode
    never happens, so this can never re-encode a frame). Only .mp4/.webm/
    .mov/.mkv are attempted; anything else, or ffmpeg missing, or any
    failure along the way, comes back UNCHANGED rather than mangled.
    """
    try:
        if not data:
            return data, False
        ext = "." + _ext(filename) if _ext(filename) else ""
        if ext not in _VIDEO_EXTS:
            return data, False
        if shutil.which("ffmpeg") is None:
            return data, False
        with tempfile.TemporaryDirectory() as td:
            in_path = os.path.join(td, "in" + ext)
            out_path = os.path.join(td, "out" + ext)
            with open(in_path, "wb") as f:
                f.write(data)
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", in_path,
                 "-map", "0", "-c", "copy",
                 "-map_metadata", "-1", "-map_chapters", "-1",
                 "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
                 out_path],
                capture_output=True, timeout=120,
            )
            if proc.returncode != 0 or not os.path.isfile(out_path):
                return data, False
            with open(out_path, "rb") as f:
                out = f.read()
        if not out:
            return data, False
        return out, True
    except Exception:
        return data, False                    # a failed strip must not break the download
