from parlance.cognates import known_vocab, similar_known, strip_diacritics


def test_strip_diacritics():
    assert strip_diacritics("Systém") == "system"
    assert strip_diacritics("pieniądze") == "pieniadze"


def test_known_vocab_map_and_similar_known():
    vocab = known_vocab("cs")
    assert len(vocab) > 30_000
    assert similar_known("system", vocab) == "systém"
    assert similar_known("hotel", vocab) == "hotel"
    assert "list" in (similar_known("list", vocab) or "")
    assert similar_known("kochać", vocab) is None  # cs 'kochat' strips differently
