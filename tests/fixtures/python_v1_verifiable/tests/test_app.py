from src.app import is_flagged


def test_flags_bad_content():
    assert is_flagged("this is bad") is True


def test_passes_clean_content():
    assert is_flagged("this is fine") is False
