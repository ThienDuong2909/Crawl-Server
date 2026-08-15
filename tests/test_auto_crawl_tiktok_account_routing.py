from douyin_nwm_tool.autocrawl import AutoCrawlDatabase


def test_crawl_channel_defaults_to_legacy_main_tiktok_account(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "autocrawl.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/default-account",
        "Default account",
        "default-account",
    )

    assert channel["tiktok_account_id"] == "main_tiktok"


def test_crawl_channel_persists_selected_tiktok_account_and_can_update_it(tmp_path):
    db = AutoCrawlDatabase(tmp_path / "autocrawl.db")
    channel = db.add_channel(
        "https://www.douyin.com/user/selected-account",
        "Selected account",
        "selected-account",
        tiktok_account_id="tiktok-secondary",
    )

    assert channel["tiktok_account_id"] == "tiktok-secondary"
    db.update_channel(channel["id"], tiktok_account_id="main_tiktok")
    assert db.get_channel(channel["id"])["tiktok_account_id"] == "main_tiktok"
