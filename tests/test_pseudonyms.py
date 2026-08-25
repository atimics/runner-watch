from runner_web.pseudonyms import ADJECTIVES, ANIMALS, pseudonym_candidate


def test_pseudonyms_are_stable_adjective_animal_names() -> None:
    first = pseudonym_candidate("user-one")

    assert first == pseudonym_candidate("user-one")
    assert first.count("-") == 1
    assert first.islower()
    assert len(ADJECTIVES) >= 100
    assert len(ANIMALS) >= 100
    assert "horny" in ADJECTIVES
    assert "beaver" in ANIMALS
    assert len({pseudonym_candidate(f"user-{index}") for index in range(100)}) >= 95
