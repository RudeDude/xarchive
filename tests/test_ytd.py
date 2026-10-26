from xarchive.ytd import parse_ytd_js, classify_tweet, reference_targets, engagement_from_obj


def test_parse_ytd_wrapper_and_semicolon():
    raw = 'window.YTD.tweets.part0 = [{"tweet": {"id_str": "1"}}];'
    data = parse_ytd_js(raw)
    assert data[0]["tweet"]["id_str"] == "1"


def test_parse_part1_name():
    raw = "window.YTD.note_tweet.part1 = []\n"
    assert parse_ytd_js(raw) == []


def test_classify_rt_prefix_without_source_is_original():
    ttype, _reply, rt = classify_tweet({}, "RT @bob: hi")
    assert ttype == "original"
    assert rt is None


def test_classify_rt_from_retweeted_status():
    ttype, _reply, rt = classify_tweet(
        {"retweeted_status": {"id_str": "99"}},
        "RT @bob: hi",
    )
    assert ttype == "retweet"
    assert rt == "99"


def test_classify_rt_from_referenced_tweets():
    ttype, _reply, rt = classify_tweet(
        {"referenced_tweets": [{"type": "retweeted", "id": "88"}]},
        "RT @bob: hi",
    )
    assert ttype == "retweet"
    assert rt == "88"


def test_reference_targets_reply_and_quote_url():
    raw = {
        "in_reply_to_status_id_str": "111",
        "in_reply_to_screen_name": "TechOperator",
        "entities": {
            "urls": [
                {
                    "expanded_url": "https://x.com/WifeDirtyTesla/status/222",
                    "url": "https://t.co/abcd",
                }
            ]
        },
    }
    refs = reference_targets(raw, text="hello https://t.co/abcd", in_reply_to_id="111")
    kinds = {r.kind: r for r in refs}
    assert kinds["reply"].status_id == "111"
    assert kinds["reply"].screen_name == "TechOperator"
    assert kinds["quote"].status_id == "222"
    assert kinds["quote"].screen_name == "WifeDirtyTesla"


def test_reference_targets_rt_prefix_has_handle_no_id():
    refs = reference_targets({}, text="RT @DougSmart: wheels rotated")
    assert len(refs) == 1
    assert refs[0].kind == "retweet"
    assert refs[0].status_id is None
    assert refs[0].screen_name == "DougSmart"


def test_reference_targets_v2_quoted_and_skips_self():
    raw = {
        "referenced_tweets": [
            {"type": "quoted", "id": "99"},
            {"type": "replied_to", "id": "88"},
        ]
    }
    refs = reference_targets(raw, tweet_id="99")
    ids = {r.status_id for r in refs}
    assert "88" in ids
    assert "99" not in ids


def test_engagement_from_archive_and_v2():
    archive = engagement_from_obj({"favorite_count": "12", "retweet_count": "3"})
    assert archive["like_count"] == 12
    assert archive["retweet_count"] == 3
    assert archive["impression_count"] is None
    v2 = engagement_from_obj(
        {
            "public_metrics": {
                "like_count": 4,
                "retweet_count": 1,
                "reply_count": 2,
                "quote_count": 0,
                "bookmark_count": 7,
                "impression_count": 90,
            }
        }
    )
    assert v2["like_count"] == 4
    assert v2["impression_count"] == 90
    assert v2["bookmark_count"] == 7


def test_reference_targets_quote_url_not_duplicating_reply():
    raw = {
        "in_reply_to_status_id_str": "111",
        "in_reply_to_screen_name": "bob",
        "entities": {"urls": [{"expanded_url": "https://twitter.com/bob/status/111"}]},
    }
    refs = reference_targets(raw, in_reply_to_id="111")
    assert [r.kind for r in refs] == ["reply"]
    assert refs[0].status_id == "111"
