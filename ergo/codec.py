"""Minimal Sigma constant encoding for integer registers (SInt 0x04, SLong 0x05)."""


def _zigzag_vlq(val: int) -> str:
    zz = (val * 2) if val >= 0 else ((-val) * 2 - 1)
    out = []
    while zz > 0x7F:
        out.append(0x80 | (zz & 0x7F))
        zz >>= 7
    out.append(zz & 0x7F)
    return "".join(f"{b:02x}" for b in out)


def encode_slong(val: int) -> str:
    """Encode an integer as a serialized SLong register value."""
    return "05" + _zigzag_vlq(val)


def decode_int_register(hex_value: str) -> int:
    """Decode a serialized SInt/SLong register (as returned by the node)."""
    data = bytes.fromhex(hex_value)
    if not data or data[0] not in (0x04, 0x05):
        raise ValueError(f"not an SInt/SLong constant: {hex_value[:8]}")
    zz = shift = 0
    for b in data[1:]:
        zz |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return (zz >> 1) ^ -(zz & 1)
    raise ValueError(f"truncated SInt/SLong constant: {hex_value[:16]}")
