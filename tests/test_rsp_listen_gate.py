"""Addressing rules of board/rsp-listen: names, follow-ups by direction, stripping."""
from conftest import load_rsp_listen

L = load_rsp_listen()


def utt(text, az=None):
    d = {"text": text}
    if az is not None:
        d["az"] = az
    return d


def test_name_matches_anywhere_and_tolerates_spelling():
    for text in ("Jarvis, what time is it?", "what time is it, jarvis", "Could you, Jervis, check?",
                 "Is Jarvis's battery low?", "JARVIS!"):
        assert L.find_name(text, ["Jarvis"], 0.8), text
    for text in ("The jar is on the shelf.", "Travis called earlier today.", "", "service"):
        assert not L.find_name(text, ["Jarvis"], 0.8), text


def test_exact_spelling_only_when_fuzzy_is_one_and_short_names_never_fuzzy():
    assert not L.find_name("Jervis, hello", ["Jarvis"], 1.0)
    assert L.find_name("jarvis, hello", ["Jarvis"], 1.0)
    assert not L.find_name("I bought a bat", ["Bot"], 0.5)      # under 4 letters: exact only
    assert L.find_name("hey bot", ["Bot"], 0.5)


def test_multi_word_name():
    assert L.find_name("okay hey board what's up", ["hey board"], 0.8)
    assert not L.find_name("hey, the board is warm", ["hey board"], 0.8)


def test_not_addressed_is_ignored_until_named():
    g = L.Gate(names=["computer"])
    assert g.check(utt("what's the weather", 200), 100.0)[0] == ""
    assert g.check(utt("computer, what's the weather", 200), 110.0)[0] == "name"


def test_follow_up_needs_same_direction_and_time():
    g = L.Gate(names=["computer"], follow=20, follow_az=40)
    assert g.check(utt("computer, lights on", 200), 100.0)[0] == "name"
    assert g.check(utt("and the fan too", 215), 105.0)[0] == "follow"        # same talker
    assert g.check(utt("tonight on the news", 280), 106.0)[0] == ""          # TV elsewhere
    assert g.check(utt("thanks", 190), 124.0)[0] == "follow"                 # window was extended
    assert g.check(utt("unrelated chatter", 190), 150.0)[0] == ""            # window closed


def test_follow_tracks_a_moving_talker_and_wraps_around_zero():
    g = L.Gate(names=["computer"], follow=20, follow_az=40)
    g.check(utt("computer", 350), 0.0)
    assert g.check(utt("one", 20), 5.0)[0] == "follow"       # 30 degrees across 0
    assert g.check(utt("two", 55), 10.0)[0] == "follow"      # moved again, measured from 20
    assert g.check(utt("three", 350), 12.0)[0] == ""         # 65 from the last position


def test_direction_check_is_skipped_without_direction_or_when_disabled():
    g = L.Gate(names=["computer"])
    g.check(utt("computer", 100), 0.0)
    assert g.check(utt("no direction known"), 5.0)[0] == "follow"
    g = L.Gate(names=["computer"], follow_az=0)
    g.check(utt("computer", 100), 0.0)
    assert g.check(utt("from the other side", 280), 5.0)[0] == "follow"


def test_touch_reopens_window_from_end_of_reply():
    g = L.Gate(names=["computer"], follow=20)
    g.check(utt("computer, tell me a long story", 100), 0.0)
    g.touch(60.0)                                             # the reply took a minute
    assert g.check(utt("go on", 100), 75.0)[0] == "follow"
    assert g.check(utt("go on", 100), 96.0)[0] == ""          # 20 s after the last follow-up


def test_strip_removes_name_or_phrase_and_tidies_punctuation():
    g = L.Gate(names=["computer"], strip=True)
    assert g.check(utt("Could you check that, computer?"), 0.0) == ("name", "Could you check that")
    assert g.check(utt("Computer, what time is it?"), 1.0) == ("name", "what time is it")
    assert g.check(utt("So, computer, what now?"), 2.0) == ("name", "So, what now")
    assert g.check(utt("Computer."), 3.0) == ("name", "Computer.")          # nothing left: keep
    g = L.Gate(wake="hey board", strip=True)
    assert g.check(utt("Hey board turn it up"), 0.0) == ("wake", "turn it up")


def test_wake_regex_still_works_alongside_name_and_without_either_all_pass():
    g = L.Gate(wake=r"ok(ay)? board", names=["computer"])
    assert g.check(utt("okay board"), 0.0)[0] == "wake"
    assert g.check(utt("computer"), 100.0)[0] == "name"
    assert L.Gate().check(utt("anything at all"), 0.0) == ("all", "anything at all")


def test_spoken_at_uses_stamp_but_distrusts_a_skewed_clock():
    import datetime
    now = 1_800_000_000.0
    ts = datetime.datetime.fromtimestamp(now - 30, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    assert L.spoken_at({"ts": ts}, now) == now - 30
    assert L.spoken_at({"ts": "2001-01-01T00:00:00+0000"}, now) == now
    assert L.spoken_at({}, now) == now
