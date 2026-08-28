from runner_web.pseudonyms import (
    ADJECTIVES,
    ANIMALS,
    AVATAR_FORMS,
    AVATAR_MATERIALS,
    AVATAR_TEMPERAMENTS,
    COMMENT_AVATAR_ABILITIES,
    COMMENT_GLYPHS,
    EMOJIS,
    comment_avatar_profile,
)


def test_public_identity_pools_have_room_for_distinct_avatars() -> None:
    assert len(ADJECTIVES) >= 100
    assert len(ANIMALS) >= 100
    assert len(EMOJIS) >= 50
    assert len(COMMENT_GLYPHS) >= 1_200
    assert all(len(glyph) == 1 for glyph in COMMENT_GLYPHS)
    assert len(AVATAR_TEMPERAMENTS) * len(AVATAR_MATERIALS) * len(AVATAR_FORMS) >= 20_000
    assert len(COMMENT_AVATAR_ABILITIES) == 6


def test_comment_avatar_visual_form_is_stable_without_exposing_its_seed() -> None:
    first = comment_avatar_profile("Quiet Amber Relay", "fixed-seed", "risk_sentinel")
    repeated = comment_avatar_profile("Quiet Amber Relay", "fixed-seed", "risk_sentinel")
    different = comment_avatar_profile("Quiet Amber Relay", "another-seed", "risk_sentinel")

    assert first == repeated
    assert first["ability"] == "Risk Sentinel"
    assert "seed" not in first
    assert any(first[key] != different[key] for key in ("tone", "frame", "eyes", "signal"))
