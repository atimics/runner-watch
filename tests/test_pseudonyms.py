from runner_web import pseudonyms
from runner_web.pseudonyms import ADJECTIVES, ANIMALS, EMOJIS


def test_public_name_pools_do_not_include_a_global_identity_generator() -> None:
    assert not hasattr(pseudonyms, "pseudonym_candidate")
    assert len(ADJECTIVES) >= 100
    assert len(ANIMALS) >= 100
    assert len(EMOJIS) >= 50
