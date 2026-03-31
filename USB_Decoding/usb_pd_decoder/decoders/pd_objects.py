"""Parse USB PD Power Data Objects (PDO) and Request Data Objects (RDO)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class FixedPDO:
    index: int          # 1-based object position
    voltage_v: float    # Volts
    current_a: float    # Amps


@dataclass
class RDOInfo:
    object_position: int   # which PDO (1-based)
    op_current_a: float    # operating current (A)
    max_current_a: float   # max current (A)


def parse_fixed_pdo(word: int, index: int) -> Optional[FixedPDO]:
    """Parse a Fixed Supply PDO word.  Returns None for non-Fixed types."""
    if (word >> 30) & 0x3 != 0x0:   # bits 31:30 must be 00 for Fixed Supply
        return None
    voltage_v = ((word >> 10) & 0x3FF) * 0.05   # bits 19:10, 50 mV units
    current_a = (word & 0x3FF) * 0.01            # bits  9:0, 10 mA units
    return FixedPDO(index=index, voltage_v=voltage_v, current_a=current_a)


def parse_rdo(word: int) -> RDOInfo:
    """Parse a Fixed Supply Request Data Object."""
    object_position = (word >> 28) & 0xF          # bits 31:28
    op_current_a    = ((word >> 10) & 0x3FF) * 0.01
    max_current_a   = (word & 0x3FF) * 0.01
    return RDOInfo(
        object_position=object_position,
        op_current_a=op_current_a,
        max_current_a=max_current_a,
    )


def parse_src_caps(payload_words: List[int]) -> List[FixedPDO]:
    """Return all Fixed Supply PDOs from a Source_Capabilities payload."""
    pdos: List[FixedPDO] = []
    for idx, word in enumerate(payload_words):
        pdo = parse_fixed_pdo(word, idx + 1)
        if pdo:
            pdos.append(pdo)
    return pdos
