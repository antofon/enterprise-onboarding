import pytest

pytestmark = pytest.mark.integration

URL = "/mock/billing/v1/subscriptions"
AUTH = {"Authorization": "Bearer legacybill-readonly-demo"}


def test_token_is_required(client) -> None:
    r = client.get(URL)
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "unauthorized"
    r = client.get(URL, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_pages_add_up_to_the_export(client) -> None:
    first = client.get(URL, params={"page": 1, "page_size": 400}, headers=AUTH)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["system"] == "LegacyBill 4.2"
    assert body["has_more"] is True
    total = body["total"]

    seen = 0
    page = 1
    while True:
        r = client.get(URL, params={"page": page, "page_size": 400}, headers=AUTH).json()
        seen += len(r["data"])
        if not r["has_more"]:
            break
        page += 1
    assert seen == total == 1003  # the committed sample
    assert page == 3

    beyond = client.get(URL, params={"page": 99, "page_size": 400}, headers=AUTH).json()
    assert beyond["data"] == [] and beyond["has_more"] is False


def test_records_keep_their_legacy_shape(client) -> None:
    rec = client.get(URL, params={"page_size": 1}, headers=AUTH).json()["data"][0]
    assert set(rec) >= {"sub_id", "acct_num", "subscription_level", "billing_freq", "sub_status"}
