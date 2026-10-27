"""Pure transformations at the Adnuntius API boundary."""

from datetime import UTC, datetime

from src.adapters.adnuntius.mapping import build_line_item, creative_payload, label_value, money_budget
from src.adapters.adnuntius.schemas import AdnuntiusProductConfig
from src.core.schemas import MediaPackage


def test_line_item_preserves_package_budget_flight_and_inventory():
    package = MediaPackage(
        package_id="pkg_1",
        name="Display",
        delivery_type="non_guaranteed",
        cpm=5,
        impressions=10000,
        format_ids=[],
        budget=50,
    )
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    payload = build_line_item(
        package,
        AdnuntiusProductConfig(tier_id="tier", ad_unit_ids=["unit"]),
        order_id="order",
        start=start,
        end=end,
        currency="NOK",
        rate=7,
    )
    assert payload["order"] == {"id": "order"}
    assert payload["tier"] == {"id": "tier"}
    assert payload["startDate"] == start.isoformat()
    assert payload["endDate"] == end.isoformat()
    assert payload["bidSpecification"] == {"cpm": {"amount": 7, "currency": "NOK"}}
    assert payload["budget"] == [
        {"amount": {"amount": 50, "currency": "NOK"}, "startDay": "2026-11-01", "endDay": "2026-11-30"}
    ]
    assert payload["objectives"] == {"IMPRESSION": 10000}
    assert payload["targeting"] == {"adUnitTarget": {"adUnits": [{"id": "unit"}]}}
    assert label_value(payload) == "pkg_1"


def test_hosted_image_html_escapes_attributes_and_uses_click_macro():
    payload = creative_payload(
        {
            "creative_id": "creative",
            "url": 'https://cdn.example/a.png?x="',
            "click_url": "https://brand.example/",
            "width": 300,
            "height": 250,
            "delivery_settings": {"tracking_urls": {"impression": ["https://tracker.example/impression"]}},
        },
        layout_id="layout",
    )
    assert payload["layout"] == {"id": "layout"}
    assert payload["width"] == 300
    content = payload["thirdPartyContent"]
    assert content["handlerType"] == "DEFAULT"
    assert "&quot;" in content["originalContent"]
    assert "[[adnClickUrlPlaceholder]]" in content["originalContent"]
    assert content["destinationUrl"] == "https://brand.example/"
    assert payload["impressionTrackingUrls"] == ["https://tracker.example/impression"]


def test_budget_days_use_the_network_calendar_across_daylight_saving():
    result = money_budget(
        25,
        "NOK",
        start=datetime(2026, 10, 24, 22, tzinfo=UTC),
        end=datetime(2026, 10, 25, 23, tzinfo=UTC),
        time_zone="Europe/Oslo",
    )
    assert result == [{"amount": {"amount": 25, "currency": "NOK"}, "startDay": "2026-10-25", "endDay": "2026-10-25"}]
