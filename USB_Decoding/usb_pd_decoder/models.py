from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class RawFrame:
    timestamp_us: int
    payload: bytes
    source: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DecodedMessage:
    timestamp_us: int
    message_type: str
    header: int
    payload_words: List[int]
    valid_crc_hint: bool
    source: str
    direction: str = ""
    cc_line: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
