import pytest

from app.chat import ChatLog, hype_weight, parse_privmsg

pytestmark = pytest.mark.unit


def test_parse_privmsg_plain_and_tagged():
    line = ":bob!bob@bob.tmi.twitch.tv PRIVMSG #speed :LUL that was insane"
    assert parse_privmsg(line) == ("bob", "LUL that was insane")
    tagged = "@badge-info=;color=#FF0000 " + line
    assert parse_privmsg(tagged) == ("bob", "LUL that was insane")
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome") is None
    assert parse_privmsg("PING :tmi.twitch.tv") is None


def test_hype_weight_orders_messages_sensibly():
    plain = hype_weight("what game is this")
    hyped = hype_weight("LUL KEKW OMEGALUL 😂")
    shouty = hype_weight("NO WAY!!!")
    assert plain == 1.0
    assert hyped > plain and shouty > plain
    assert hype_weight("lul " * 20) <= 3.0 + 0.5  # capped


def test_chatlog_series_bins_and_expiry():
    log = ChatLog(max_age_s=100)
    for t in (10.1, 10.9, 12.5):
        log.add(t, 1.0)
    log.add(11.0, 2.5)
    arr = log.series(start_ts=10, n_seconds=4)
    assert list(arr) == [2.0, 2.5, 1.0, 0.0]
    log.add(500.0)  # pushes the old events past max_age
    assert log.series(10, 4).sum() == 0
