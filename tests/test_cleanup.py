import holler


def test_leading_backchannels_stripped():
    assert holler.cleanup("Um, hello world.") == "hello world."
    assert holler.cleanup("Uh, um, so the deploy is done.") == "so the deploy is done."
    assert holler.cleanup("Mm-hmm. Right, sounds good.") == "Right, sounds good."
    assert holler.cleanup("uh-huh okay") == "okay"


def test_trailing_thanks_family_stripped():
    assert holler.cleanup("Um, hello world, thank you for watching.") == "hello world,"
    assert holler.cleanup("Done for today. Thanks so much!") == "Done for today."
    assert holler.cleanup("Wrapping up. Thank you very much for watching.") == "Wrapping up."
    assert holler.cleanup("See you then. Thanks for listening.") == "See you then."


def test_all_hallucination_becomes_empty():
    assert holler.cleanup("Thank you.") == ""
    assert holler.cleanup("Thanks for listening.") == ""
    assert holler.cleanup("uh-huh uh-huh") == ""
    assert holler.cleanup("Um.") == ""


def test_mid_utterance_thanks_preserved():
    text = "I want to thank you for the gift"
    assert holler.cleanup(text) == text


def test_normal_transcripts_unchanged():
    text = "The quick brown fox jumps over the lazy dog."
    assert holler.cleanup(text) == text


def test_word_boundaries_protect_real_words():
    # Regression (family-wide): without \b anchors, "um"/"ah" matched word
    # prefixes/suffixes.
    assert holler.cleanup("umbrella weather today") == "umbrella weather today"
    assert holler.cleanup("ahead of schedule") == "ahead of schedule"
    assert holler.cleanup("We visited Utah") == "We visited Utah"
    assert holler.cleanup("That was a blah") == "That was a blah"
    assert holler.cleanup("um umbrella") == "umbrella"


def test_casing_variants():
    assert holler.cleanup("UM HELLO") == "HELLO"
    assert holler.cleanup("AHA, got it. THANK YOU.") == "got it."


def test_whitespace_collapsed():
    assert holler.cleanup("  spaced   out   text  ") == "spaced out text"


def test_empty_input():
    assert holler.cleanup("") == ""
