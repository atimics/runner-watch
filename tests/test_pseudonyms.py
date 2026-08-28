from runner_web import pseudonyms
from runner_web.pseudonyms import ADJECTIVES, ANIMALS, COMMENT_GLYPHS, EMOJIS


def test_public_name_pools_do_not_include_a_global_identity_generator() -> None:
    assert not hasattr(pseudonyms, "pseudonym_candidate")
    assert len(ADJECTIVES) >= 100
    assert len(ANIMALS) >= 100
    assert len(EMOJIS) >= 50
    assert len(COMMENT_GLYPHS) >= 1_200
    assert all(len(glyph) == 1 for glyph in COMMENT_GLYPHS)
