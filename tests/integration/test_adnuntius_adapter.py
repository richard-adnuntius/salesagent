"""Vendor API contracts, exercised through real HTTP and the real adapter.

These assertions concern Adnuntius payloads and advertiser checks, which have no
AdCP wire representation. They therefore belong at the integration seam. The
remote ad server is a programmable origin; no production code is replaced.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from src.adapters import get_adapter_class, get_adapter_schemas
from src.adapters.adnuntius import AdnuntiusAdapter
from src.adapters.base import AdapterCreateRequest
from src.core.exceptions import AdCPAdapterError, AdCPMediaBuyNotFoundError
from src.core.helpers.adapter_helpers import get_adapter
from src.core.schemas import MediaPackage, ReportingPeriod
from tests.factories import (
    AdapterConfigFactory,
    CreativeAssignmentFactory,
    CreativeFactory,
    MediaBuyFactory,
    MediaPackageFactory,
    PrincipalFactory,
    TenantFactory,
)
from tests.harness._base import IntegrationEnv
from tests.helpers.egress_backoff import set_flags
from tests.helpers.envelope_assertions import assert_envelope_shape, envelope_for
from tests.helpers.media_buy_approval import uploadable_creative

START = datetime(2026, 11, 1, tzinfo=UTC)
END = datetime(2026, 12, 1, tzinfo=UTC)
PERIOD = ReportingPeriod(start=START, end=END)
ORDER = {"id": "order", "advertiser": {"id": "advertiser"}, "objectState": "ACTIVE"}
LINE = {
    "id": "line",
    "order": {"id": "order"},
    "labels": ["adcp:package:pkg"],
    "userState": "APPROVED",
    "executionState": "RUNNING",
    "startDate": START.isoformat(),
    "endDate": END.isoformat(),
}


@pytest.fixture
def adnuntius_env(integration_db):
    with IntegrationEnv(tenant_id="adn-tenant", principal_id="adn-buyer") as env:
        yield env


@pytest.fixture
def vendor_env(adnuntius_env, local_origin_tls, monkeypatch):
    local_origin = local_origin_tls
    set_flags(monkeypatch, private=True)
    env = adnuntius_env
    tenant = TenantFactory(tenant_id=env.tenant_id, ad_server="adnuntius")
    mappings = {"adnuntius": {"advertiser_id": "advertiser"}}
    PrincipalFactory(tenant=tenant, principal_id="adn-buyer", platform_mappings=mappings)
    AdapterConfigFactory(
        tenant=tenant,
        adapter_type="adnuntius",
        config_json={
            "api_url": local_origin.base_url,
            "api_key": "vendor-secret",
            "network_id": "network",
            "currency": "NOK",
            "time_zone": "UTC",
            "layout_id": "html-layout",
        },
    )
    identity = PrincipalFactory.make_identity(
        tenant_id=env.tenant_id,
        principal_id="adn-buyer",
        ad_server="adnuntius",
        platform_mappings=mappings,
    )
    yield get_adapter(identity), local_origin


def respond(origin, *objects):
    origin.respond_in_sequence([(200, json.dumps(value).encode()) for value in objects])


def test_runtime_factory_loads_json_configuration(vendor_env):
    adapter, origin = vendor_env
    assert isinstance(adapter, AdnuntiusAdapter)
    assert get_adapter_class("adnuntius") is AdnuntiusAdapter
    assert get_adapter_schemas("adnuntius").product_config is AdnuntiusAdapter.product_config_class
    assert not origin.requests


def test_booking_sends_network_currency_tier_and_package_mapping(vendor_env):
    adapter, origin = vendor_env
    respond(origin, {"id": "created-order"}, {"id": "created-line"})
    package = MediaPackage(
        package_id="pkg",
        name="Display",
        delivery_type="non_guaranteed",
        cpm=5,
        impressions=10000,
        budget=50,
        format_ids=[],
        implementation_config={"tier_id": "tier", "ad_unit_ids": ["unit"]},
    )
    result = adapter.create_media_buy(
        AdapterCreateRequest(total_budget=Decimal(50)),
        [package],
        START,
        END,
    )
    assert result.media_buy_id == "created-order"
    assert result.platform_line_item_ids == {"pkg": "created-line"}
    order_request, line_request = origin.requests
    assert order_request.json()["advertiser"] == {"id": "advertiser"}
    assert line_request.json()["order"] == {"id": "created-order"}
    assert line_request.json()["bidSpecification"]["cpm"] == {"amount": 5, "currency": "NOK"}
    for request in origin.requests:
        assert request.headers["authorization"] == "Bearer vendor-secret"
        assert parse_qs(urlsplit(request.path).query)["context"] == ["network"]


def test_package_pause_changes_only_the_selected_line_item(vendor_env):
    adapter, origin = vendor_env
    respond(origin, ORDER, {"results": [LINE], "totalCount": 1}, {"id": "line"})
    result = adapter.update_media_buy("order", "pause_package", "pkg", None, START)
    assert result.affected_packages[0].package_id == "pkg"
    assert origin.requests[-1].json() == {"userState": "PAUSED"}
    assert urlsplit(origin.requests[-1].path).path == "/lineitems/line"


def test_foreign_order_is_refused_before_any_line_item_or_stats_query(vendor_env):
    adapter, origin = vendor_env
    respond(origin, {**ORDER, "advertiser": {"id": "someone-else"}})
    with pytest.raises(AdCPMediaBuyNotFoundError) as failure:
        adapter.get_media_buy_delivery("order", PERIOD, END)
    assert_envelope_shape(envelope_for(failure.value), "MEDIA_BUY_NOT_FOUND", recovery="correctable")
    assert len(origin.requests) == 1


def test_delivery_uses_measured_spend_and_package_ids(vendor_env):
    adapter, origin = vendor_env
    respond(
        origin,
        ORDER,
        {"results": [LINE], "totalCount": 1},
        {
            "chunks": [
                {
                    "lineItem": {"id": "line"},
                    "impressions": 1234,
                    "clicks": 12,
                    "cost": {"amount": 6.17, "currency": "NOK"},
                }
            ],
        },
    )
    result = adapter.get_media_buy_delivery("order", PERIOD, END)
    assert result.totals.impressions == 1234
    assert result.totals.spend == 6.17
    assert result.by_package[0].package_id == "pkg"
    assert result.currency == "NOK"
    query = parse_qs(urlsplit(origin.requests[-1].path).query)
    assert query["orderId"] == ["order"]
    assert query["startDate"] == [START.isoformat()]


def test_creative_upload_uses_assigned_package_and_keeps_remote_id(vendor_env):
    adapter, origin = vendor_env
    respond(origin, ORDER, {"results": [LINE], "totalCount": 1}, {"id": "remote-creative"})
    statuses = adapter.add_creative_assets(
        "order",
        [
            {
                "creative_id": "buyer-creative",
                "url": "https://cdn.example/banner.png",
                "width": 300,
                "height": 250,
                "click_url": "https://brand.example/",
                "package_assignments": [{"package_id": "pkg"}],
            }
        ],
        START,
    )
    assert statuses[0].creative_id == "remote-creative"
    assert origin.requests[-1].json()["lineItem"] == {"id": "line"}


def test_approved_creative_push_uses_the_adapter_interface(vendor_env, adnuntius_env):
    from src.core.tools.media_buy_create import push_creative_to_existing_buy

    adapter, origin = vendor_env
    tenant, principal = adnuntius_env.setup_default_data()
    creative = uploadable_creative(CreativeFactory, tenant=tenant, principal=principal, status="approved")
    buy = MediaBuyFactory(tenant=tenant, principal=principal, status="active")
    MediaPackageFactory(media_buy=buy, package_id="pkg", package_config={"platform_order_id": "order"})
    CreativeAssignmentFactory(creative=creative, media_buy=buy, package_id="pkg")
    respond(origin, ORDER, {"results": [LINE], "totalCount": 1}, {"id": "uploaded"})
    success, error = push_creative_to_existing_buy(
        tenant_id=tenant.tenant_id,
        creative_id=creative.creative_id,
        media_buy_id=buy.media_buy_id,
    )
    assert success, error
    assert origin.requests[-1].json()["lineItem"] == {"id": "line"}


def test_partial_booking_deactivates_order_and_does_not_retry(vendor_env):
    adapter, origin = vendor_env
    origin.respond_in_sequence(
        [(200, b'{"id":"order"}'), (500, b'{"error":"private vendor detail"}'), (200, b'{"id":"order"}')]
    )
    package = MediaPackage(
        package_id="pkg",
        name="Display",
        delivery_type="non_guaranteed",
        cpm=5,
        impressions=1000,
        format_ids=[],
        implementation_config={"tier_id": "tier", "site_ids": ["site"]},
    )
    with pytest.raises(AdCPAdapterError) as failure:
        adapter.create_media_buy(AdapterCreateRequest(), [package], START, END)
    assert "private vendor detail" not in json.dumps(envelope_for(failure.value))
    assert len(origin.requests) == 3
    assert origin.requests[-1].json() == {"objectState": "INACTIVE"}


def test_inventory_pagination_is_not_truncated(vendor_env):
    adapter, origin = vendor_env
    respond(origin, {"results": [{"id": "a"}], "totalCount": 2}, {"results": [{"id": "b"}], "totalCount": 2})
    assert adapter.client.list("lineitems", order="order") == [{"id": "a"}, {"id": "b"}]
    assert parse_qs(urlsplit(origin.requests[-1].path).query)["page"] == ["1"]


@pytest.mark.parametrize(
    "action,expected",
    [
        (
            "update_package_budget",
            {
                "budget": [
                    {"startDay": "2026-11-01", "endDay": "2026-11-30", "amount": {"amount": 80, "currency": "NOK"}}
                ]
            },
        ),
        ("update_package_impressions", {"objectives": {"IMPRESSION": 80, "CLICK": 9}}),
    ],
)
def test_package_updates_preserve_unrelated_fields(vendor_env, action, expected):
    adapter, origin = vendor_env
    respond(origin, ORDER, {"results": [{**LINE, "objectives": {"IMPRESSION": 50, "CLICK": 9}}]}, {"id": "line"})
    adapter.update_media_buy("order", action, "pkg", 80, START)
    assert origin.requests[-1].json() == expected


@pytest.mark.parametrize(
    "status,code,recovery",
    [
        (401, "CONFIGURATION_ERROR", "terminal"),
        (404, "REFERENCE_NOT_FOUND", "correctable"),
        (429, "RATE_LIMITED", "transient"),
        (503, "SERVICE_UNAVAILABLE", "transient"),
    ],
)
def test_vendor_failures_are_typed_and_hide_response_bodies(vendor_env, status, code, recovery):
    from src.core.exceptions import AdCPSalesAgentError

    adapter, origin = vendor_env
    origin.respond_with(status, body=b'{"error":"private vendor detail"}', headers={"Retry-After": "15"})
    with pytest.raises(AdCPSalesAgentError) as failure:
        adapter.client.get("orders", "order")
    envelope = envelope_for(failure.value)
    assert_envelope_shape(envelope, code, recovery=recovery)
    assert "private vendor detail" not in json.dumps(envelope)
    assert origin.hits == 1


def test_creative_reuse_creates_a_copy_on_the_target_line(vendor_env):
    adapter, origin = vendor_env
    source = {
        "id": "source",
        "lineItem": {"id": "source-line"},
        "layout": {"id": "html-layout"},
        "thirdPartyContent": {"originalContent": "<b>Ad</b>", "handlerType": "DEFAULT"},
        "targeting": {"keywordTargets": [{"keywords": ["sport"]}]},
        "userState": "APPROVED",
        "labels": ["adcp:creative:buyer-creative"],
    }
    respond(origin, LINE, ORDER, source, {**LINE, "id": "source-line"}, ORDER, {"id": "copy"})
    result = adapter.associate_creatives(["line"], ["source"])
    assert result == [{"line_item_id": "line", "creative_id": "source", "status": "success"}]
    payload = origin.requests[-1].json()
    assert payload["lineItem"] == {"id": "line"}
    assert payload["targeting"] == source["targeting"]
    assert payload["labels"] == source["labels"]
    copy_path = origin.requests[-1].path
    respond(origin, ORDER, {"results": [LINE]}, {"id": "updated-copy"})
    adapter.add_creative_assets(
        "order",
        [
            {
                "creative_id": "buyer-creative",
                "width": 300,
                "height": 250,
                "html": "<b>Updated ad</b>",
                "package_assignments": [{"package_id": "pkg"}],
            }
        ],
        START,
    )
    assert origin.requests[-1].path == copy_path
