"""Apple SRP-6a 已知答案向量，与 Go 版实现保持一致。"""

import pytest

from platforms.icloud.errors import ICloudError
from platforms.icloud.srp import MODULUS, AppleSRPClient, derive_password


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [
        ("s2k", "27e4f98bda15ebc577083e895266e847a0ae9896bbc80cc07c3194a107ed34a6"),
        ("s2k_fo", "9601fa505b07f7552bbfa237464cb59ed582258b5672fbc1eb57bd36f284bd55"),
    ],
)
def test_derive_password_matches_vector(protocol, expected):
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    derived = derive_password("correct horse battery staple", salt, 1000, protocol)
    assert derived.hex() == expected


@pytest.mark.parametrize(
    ("iterations", "protocol"),
    [(0, "s2k"), (1_000_001, "s2k"), (1, "unknown")],
)
def test_derive_password_rejects_invalid_parameters(iterations, protocol):
    with pytest.raises(ICloudError):
        derive_password("password", b"\x00" * 16, iterations, protocol)


def test_proof_vector():
    client = AppleSRPClient(private_value=1)
    assert client.public_bytes == b"\x02"

    proofs = client.process_challenge(
        b"user@example.com",
        bytes.fromhex("27e4f98bda15ebc577083e895266e847a0ae9896bbc80cc07c3194a107ed34a6"),
        bytes.fromhex("0102030405060708090a0b0c0d0e0f10"),
        b"\x03",
    )
    assert proofs.client_proof.hex() == "10583e3c45b38bb71efba96a3ad3b408df4827e216b232fc99bf3c09fd3302a2"
    assert proofs.server_proof.hex() == "b58eb38c33e451525fe110ef5d43046260b9c635bd54a7536ba9d444addefd19"


def test_leading_zero_in_salt_is_preserved():
    derived = b"\x24" * 32
    with_zero = AppleSRPClient(private_value=1).process_challenge(
        b"user@example.com", derived, b"\x00\x01\x02\x03", b"\x03"
    )
    without_zero = AppleSRPClient(private_value=1).process_challenge(
        b"user@example.com", derived, b"\x01\x02\x03", b"\x03"
    )
    assert with_zero.client_proof != without_zero.client_proof


@pytest.mark.parametrize(
    "server_public",
    [
        b"",
        b"\x00",
        MODULUS.to_bytes(256, "big"),
        (MODULUS + 1).to_bytes(257, "big"),
    ],
)
def test_rejects_invalid_server_public_value(server_public):
    client = AppleSRPClient(private_value=1)
    with pytest.raises(ICloudError):
        client.process_challenge(b"user@example.com", b"\x42" * 32, b"\x01", server_public)
