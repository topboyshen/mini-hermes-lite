"""Strict, dependency-free validation and private staging for pi input files.

Image checks validate type, container/header structure and dimensions, not a full
pixel decode. pi performs its native image processing after these bounded checks.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import os
from pathlib import Path
import struct
import tempfile
from typing import Callable, Iterator
import unicodedata
import uuid
import zlib


MAX_IMAGE_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_DIMENSION = 8192

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
TEXT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json"}


def _invalid() -> ValueError:
    return ValueError("invalid attachment")


def _dimensions(width: int, height: int) -> None:
    if not (0 < width <= MAX_IMAGE_DIMENSION and 0 < height <= MAX_IMAGE_DIMENSION
            and width * height <= MAX_IMAGE_PIXELS):
        raise _invalid()


def _png(data: bytes) -> None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _invalid()
    offset, seen_header, seen_data = 8, False, False
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(data):
            raise _invalid()
        payload = data[offset + 8:offset + 8 + size]
        crc = int.from_bytes(data[end - 4:end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise _invalid()
        if not seen_header:
            if kind != b"IHDR" or size != 13:
                raise _invalid()
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            _dimensions(width, height)
            allowed_depth = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in allowed_depth.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise _invalid()
            seen_header = True
        elif kind in (b"IHDR", b"acTL", b"fcTL", b"fdAT"):
            # pi does not treat APNG as a supported inline PNG.
            raise _invalid()
        elif kind == b"IDAT":
            seen_data = seen_data or size > 0
        elif kind == b"IEND":
            if size or not seen_data or end != len(data):
                raise _invalid()
            return
        offset = end
    raise _invalid()


def _jpeg(data: bytes) -> None:
    if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        raise _invalid()
    offset, seen_frame = 2, False
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            raise _invalid()
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset + 3 > len(data):
            raise _invalid()
        marker = data[offset]
        offset += 1
        if marker in (0x00, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            raise _invalid()
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data) - 2:
            raise _invalid()
        payload = data[offset + 2:offset + length]
        if marker in (0xC0, 0xC1, 0xC2):
            if len(payload) < 6 or payload[0] not in (8, 12):
                raise _invalid()
            height, width = struct.unpack(">HH", payload[1:5])
            _dimensions(width, height)
            if payload[5] not in (1, 3, 4) or len(payload) != 6 + 3 * payload[5]:
                raise _invalid()
            seen_frame = True
        elif marker == 0xDA:
            if not seen_frame or len(payload) < 4 or payload[0] not in (1, 3, 4) or len(payload) != 4 + 2 * payload[0]:
                raise _invalid()
            if offset + length >= len(data) - 2:
                raise _invalid()
            return
        offset += length
    raise _invalid()


def _webp(data: bytes) -> None:
    if (len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP"
            or int.from_bytes(data[4:8], "little") + 8 != len(data)):
        raise _invalid()
    offset, image, canvas = 12, None, None
    while offset + 8 <= len(data):
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        end = offset + 8 + size
        if end + (size & 1) > len(data):
            raise _invalid()
        payload = data[offset + 8:end]
        if kind == b"VP8X":
            if offset != 12 or len(payload) != 10 or payload[0] & 0x02:
                raise _invalid()
            canvas = (1 + int.from_bytes(payload[4:7], "little"), 1 + int.from_bytes(payload[7:10], "little"))
            _dimensions(*canvas)
        elif kind == b"VP8 ":
            if image or len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
                raise _invalid()
            image = (int.from_bytes(payload[6:8], "little") & 0x3FFF, int.from_bytes(payload[8:10], "little") & 0x3FFF)
            _dimensions(*image)
        elif kind == b"VP8L":
            if image or len(payload) < 5 or payload[0] != 0x2F:
                raise _invalid()
            bits = int.from_bytes(payload[1:5], "little")
            if bits >> 29:
                raise _invalid()
            image = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            _dimensions(*image)
        elif kind in (b"ANIM", b"ANMF"):
            raise _invalid()
        offset = end + (size & 1)
    if offset != len(data) or image is None or canvas and canvas != image:
        raise _invalid()


def validate_attachments(value: object) -> list[dict[str, str]]:
    """Return canonical wire entries; never accept a client-side filesystem path."""
    if not isinstance(value, list) or len(value) > 1:
        raise _invalid()
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "mime_type", "data"}:
            raise _invalid()
        name, mime, encoded = item["name"], item["mime_type"], item["data"]
        if (not isinstance(name, str) or not name or len(name) > 128
                or len(name.encode("utf-8", errors="replace")) > 255
                or "/" in name or "\\" in name or name.startswith(".")
                or any(unicodedata.category(ch).startswith("C") for ch in name)):
            raise _invalid()
        suffix = Path(name).suffix.lower()
        expected = IMAGE_TYPES.get(suffix) or TEXT_TYPES.get(suffix)
        if expected is None or not isinstance(mime, str):
            raise _invalid()
        mime = mime.lower().strip()
        if mime != expected and not (suffix in TEXT_TYPES and mime == "text/plain"):
            raise _invalid()
        limit = MAX_IMAGE_BYTES if suffix in IMAGE_TYPES else MAX_TEXT_BYTES
        if not isinstance(encoded, str) or not encoded or len(encoded) > 4 * ((limit + 2) // 3):
            raise _invalid()
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise _invalid() from None
        if not data or len(data) > limit or base64.b64encode(data).decode("ascii") != encoded:
            raise _invalid()
        if suffix in IMAGE_TYPES:
            {"image/png": _png, "image/jpeg": _jpeg, "image/webp": _webp}[expected](data)
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeError:
                raise _invalid() from None
            if not text or any(ord(ch) < 32 and ch not in "\t\n\r" or ord(ch) == 127 for ch in text):
                raise _invalid()
        result.append({"name": name, "mime_type": expected, "data": encoded})
    return result


@contextmanager
def staged_attachments(
    workspace: Path, attachments: list[dict[str, str]],
) -> Iterator[tuple[list[Path], Callable[[], None]]]:
    """Stage only validated bytes under a private, randomly named directory."""
    if not attachments:
        yield [], lambda: None
        return
    temporary = tempfile.TemporaryDirectory(prefix=".pi-input-", dir=workspace)
    try:
        directory = Path(temporary.name)
        directory.chmod(0o700)
        paths = []
        for item in attachments:
            suffix = Path(item["name"]).suffix.lower()
            path = directory / (uuid.uuid4().hex + suffix)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(base64.b64decode(item["data"], validate=True))
            paths.append(path)
        yield paths, temporary.cleanup
    finally:
        temporary.cleanup()
