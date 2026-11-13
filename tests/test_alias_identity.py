"""The confession identity: the fruit alias and the random pastel accent.

The alias is drawn from the catalog list (the copy lives in the catalogs),
the accent is a random pastel whose ranges are customizable per call (a
guild can ask for a lighter or a stronger pastel without touching code).
"""

from __future__ import annotations

from rosemary.core.alias_identity import (
    ban_hash,
    claim_hash,
    claim_token,
    fingerprint,
    generate_keypair,
    open_seal,
    random_alias,
    random_color,
    seal,
)


def test_random_alias_picks_from_the_list():
    pool = ("Maçã", "Banana", "Morango")
    for _ in range(10):
        assert random_alias(pool) in pool
    assert random_alias([]) == ""


def test_random_color_is_a_pastel_hex():
    for _ in range(10):
        value = random_color()
        assert value.startswith("#") and len(value) == 7
        red, green, blue = (int(value[i : i + 2], 16) for i in (1, 3, 5))
        assert min(red, green, blue) > 40, value


def test_random_color_takes_custom_ranges():
    # A fixed hue and a fixed lightness/saturation is a constant color.
    assert random_color(hue=0.0, saturation=(0.5, 0.5), lightness=(0.5, 0.5)) == random_color(
        hue=0.0, saturation=(0.5, 0.5), lightness=(0.5, 0.5)
    )


def test_seal_and_open_roundtrip():
    pub, priv = generate_keypair()
    blob = seal(pub, "k1", "42")
    assert blob.startswith("k1:")
    assert open_seal(priv, blob) == "42"
    assert fingerprint(pub) == fingerprint(pub)
    assert len(fingerprint(pub)) == 16


def test_claim_token_is_random_and_hashed():
    token = claim_token()
    assert token and claim_hash(token) == claim_hash(token)
    assert token not in claim_hash(token)
    assert claim_token() != token


def test_ban_hash_is_stable_per_guild_and_id(tmp_path):
    first = ban_hash(tmp_path, 1, 7)
    assert first == ban_hash(tmp_path, 1, 7)
    assert first != ban_hash(tmp_path, 1, 8)
    assert first != ban_hash(tmp_path, 2, 7)
