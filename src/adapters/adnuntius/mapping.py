"""Pure translations from adapter carriers to Adnuntius JSON."""

from datetime import datetime, timedelta
from html import escape
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from src.adapters.adnuntius.schemas import AdnuntiusProductConfig
from src.core.schemas import MediaPackage

PACKAGE_LABEL = "adcp:package:"
CREATIVE_LABEL = "adcp:creative:"


def label_value(entity: dict[str, Any], prefix: str = PACKAGE_LABEL) -> str | None:
    return next(
        (label.removeprefix(prefix) for label in entity.get("labels", []) if label.startswith(prefix)),
        None,
    )


def creative_id_for_line(tenant_id: str, line_id: str, creative_key: str) -> str:
    return uuid5(NAMESPACE_URL, f"{tenant_id}:{line_id}:{creative_key}").hex


def money_budget(
    amount: float,
    currency: str,
    *,
    start: datetime,
    end: datetime,
    time_zone: str,
) -> list[dict[str, Any]]:
    zone = ZoneInfo(time_zone)
    # Adnuntius budget endDay is inclusive; the flight end instant is exclusive.
    return [
        {
            "amount": {"amount": amount, "currency": currency},
            "startDay": start.astimezone(zone).date().isoformat(),
            "endDay": (end - timedelta(microseconds=1)).astimezone(zone).date().isoformat(),
        }
    ]


def build_line_item(
    package: MediaPackage,
    config: AdnuntiusProductConfig,
    *,
    order_id: str,
    start: datetime,
    end: datetime,
    currency: str,
    rate: float,
    time_zone: str = "UTC",
) -> dict[str, Any]:
    targeting: dict[str, Any] = {}
    for ids, field, member in (
        (config.ad_unit_ids, "adUnitTarget", "adUnits"),
        (config.site_ids, "siteTarget", "sites"),
    ):
        if ids:
            targeting[field] = {member: [{"id": value} for value in ids]}
    budget = package.budget if package.budget is not None else package.impressions * rate / 1000
    return {
        "name": package.name,
        "order": {"id": order_id},
        "tier": {"id": config.tier_id},
        "type": "AUCTION",
        "objectState": "ACTIVE",
        "userState": "APPROVED",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "bidSpecification": {"cpm": {"amount": rate, "currency": currency}},
        "budget": money_budget(budget, currency, start=start, end=end, time_zone=time_zone),
        "objectives": {"IMPRESSION": package.impressions},
        "targeting": targeting,
        "labels": [PACKAGE_LABEL + package.package_id],
    }


def creative_payload(asset: dict[str, Any], *, layout_id: str) -> dict[str, Any]:
    """Hosted images use the publisher's third-party layout; no asset URL is fetched."""
    content = asset.get("html") or asset.get("snippet")
    if not content:
        image = escape(str(asset["url"]), quote=True)
        content = f'<a href="[[adnClickUrlPlaceholder]]" target="_blank"><img src="{image}" alt=""></a>'
    return {
        "name": asset.get("name") or asset["creative_id"],
        "labels": [CREATIVE_LABEL + asset["creative_id"]],
        "layout": {"id": layout_id},
        "width": int(asset["width"]),
        "height": int(asset["height"]),
        "userState": "APPROVED",
        "thirdPartyContent": {
            "handlerType": "DEFAULT",
            "originalContent": content,
            "destinationUrl": asset.get("click_url") or asset.get("click_through_url"),
        },
        "impressionTrackingUrls": asset.get("delivery_settings", {}).get("tracking_urls", {}).get("impression", []),
    }
