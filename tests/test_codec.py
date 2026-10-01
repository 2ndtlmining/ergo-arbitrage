"""Sigma constant encoding for SLong/SInt registers."""
import pytest

from ergo.codec import decode_int_register, encode_slong


def test_decode_known_mainnet_values():
    assert decode_int_register("05f8f9b9e301") == 238501500   # SLong (Machina order R5)
    assert decode_int_register("04c60f") == 995               # SInt (pool feeNum R4)


@pytest.mark.parametrize("v", [0, 1, -1, 100, -100, 3_099_010_592, -(2**40), 2**62])
def test_slong_roundtrip(v):
    assert decode_int_register(encode_slong(v)) == v


def test_rejects_other_types():
    with pytest.raises(ValueError):
        decode_int_register("0e0101")
