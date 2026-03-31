from __future__ import annotations

from typing import Iterable, List

from ..models import RawFrame


class RawFrameParser:
    """Parse lines in the form: <timestamp_us> <hex bytes>."""

    @staticmethod
    def parse_hex_blob(raw: str) -> bytes:
        compact = raw.replace(" ", "").replace("_", "").strip()
        if len(compact) % 2 != 0:
            raise ValueError(f"Hex payload has odd length: {raw}")
        return bytes.fromhex(compact)

    def parse_lines(self, lines: Iterable[str], source: str = "file") -> List[RawFrame]:
        frames: List[RawFrame] = []
        for lineno, line in enumerate(lines, start=1):
            text = line.lstrip("\ufeff").strip()
            if not text or text.startswith("#"):
                continue

            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError(f"Line {lineno}: expected '<timestamp_us> <hex>'")

            ts = int(parts[0])
            payload = self.parse_hex_blob(parts[1])
            frames.append(RawFrame(timestamp_us=ts, payload=payload, source=source))
        return frames

