import pytest

from app.config import Settings
from app.db import Database

pytestmark = pytest.mark.unit


def test_from_env_reads_secrets_and_tunables():
    s = Settings.from_env({"TWITCH_CLIENT_ID": "a", "TWITCH_CLIENT_SECRET": "b", "Z_THRESHOLD": "4.5",
                           "AUTO_APPROVE": "true", "PUBLIC_BASE_URL": "https://x.app/"})
    assert s.configured()["twitch"] and not s.configured()["kick"]
    assert s.z_threshold == 4.5 and s.auto_approve is True
    assert s.public_base_url == "https://x.app"


def test_update_validates_and_is_atomic():
    s = Settings()
    with pytest.raises(ValueError):
        s.update({"pre_roll_s": 50, "z_threshold": "abc"})
    assert s.pre_roll_s == 40.0  # nothing applied
    with pytest.raises(ValueError):
        s.update({"layout": "sideways"})
    with pytest.raises(ValueError):
        s.update({"nope": 1})
    with pytest.raises(ValueError):
        s.update({"pre_roll_s": -5})
    with pytest.raises(ValueError):
        s.update({"caption_template": "{unknown}"})
    assert s.update({"pre_roll_s": "35", "auto_approve": "on"}) == {"pre_roll_s": 35.0, "auto_approve": True}


def test_default_account_ids_parsing():
    s = Settings(default_accounts=" a, b ,,c")
    assert s.default_account_ids() == ["a", "b", "c"]


def test_streamers_crud_and_normalisation():
    db = Database(":memory:")
    a = db.add_streamer("Twitch", "@IShowSpeed")
    assert a["handle"] == "ishowspeed" and a["platform"] == "twitch"
    assert db.add_streamer("twitch", "ishowspeed")["id"] == a["id"]  # no duplicate
    yt = db.add_streamer("youtube", "@MrBeast")
    assert yt["handle"] == "MrBeast"  # youtube ids/handles keep case
    with pytest.raises(ValueError):
        db.add_streamer("tiktok", "x")
    with pytest.raises(ValueError):
        db.add_streamer("kick", "  ")
    db.set_streamer_enabled(a["id"], False)
    assert [r["handle"] for r in db.list_streamers(enabled_only=True)] == ["MrBeast"]
    db.delete_streamer(a["id"])
    assert len(db.list_streamers()) == 1


def test_clip_lifecycle_and_atomic_claim():
    db = Database(":memory:")
    sid = db.start_session("twitch", "x", "t", "1", "/tmp")
    cid = db.add_clip(sid, "twitch", "x", 10, 70, 50, 5.0, 4.0, 2.0)
    assert db.get_clip(cid)["status"] == "rendering"
    db.update_clip(cid, status="pending", path="/p.mp4")
    assert db.claim_clip(cid, ("pending", "failed"), "posting") is True
    assert db.claim_clip(cid, ("pending", "failed"), "posting") is False  # second claim loses
    with pytest.raises(ValueError):
        db.update_clip(cid, status="bogus")
    with pytest.raises(ValueError):
        db.update_clip(cid, id=5)
    assert db.counts()["posting"] == 1


def test_settings_persist():
    db = Database(":memory:")
    db.put_settings({"z_threshold": 2.5})
    db.put_settings({"z_threshold": 3.5, "layout": "crop"})
    assert db.get_settings() == {"z_threshold": "3.5", "layout": "crop"}
