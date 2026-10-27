# Adnuntius publisher-network adapter

## Contract grounding

Implementation target: the repository pins `adcp==6.6.0`, AdCP **3.1.1**.
This integration implements the existing adapter seam; it adds no transport or
request fields. References below were read at the `v3.1.1` tag before implementation.

- [create_media_buy, Package Object and Success Response](https://github.com/adcontextprotocol/adcp/blob/v3.1.1/docs/media-buy/task-reference/create_media_buy.mdx): preserve package IDs, pricing, budget and flight. Storyboard
  `domains/media-buy/scenarios/pending_creatives_to_start.yaml`,
  `create_without_creatives.create_buy_no_creatives` and
  `supply_creatives.assign_creative_to_package`.
- [update_media_buy, Package Update Object and PATCH Semantics](https://github.com/adcontextprotocol/adcp/blob/v3.1.1/docs/media-buy/task-reference/update_media_buy.mdx): change only requested pause, budget and impression fields. Adapter-specific mapping: ungraded.
- [get_media_buy_delivery, Response and Metrics Definitions](https://github.com/adcontextprotocol/adcp/blob/v3.1.1/docs/media-buy/task-reference/get_media_buy_delivery.mdx): report measured impressions and spend per package. Storyboard
  `domains/media-buy/scenarios/delivery_reporting.yaml`,
  `simulate_and_verify.get_delivery`.
- [L1 Security, Agent and Account Isolation, Time Semantics, and Webhook URL validation](https://github.com/adcontextprotocol/adcp/blob/v3.1.1/docs/building/by-layer/L1/security.mdx): check advertiser ownership before remote access, preserve timezone-aware half-open flight intervals, and use the existing egress gateway. Vendor-specific checks: ungraded.
- [L3 Error Handling, Configuration Error Patterns](https://github.com/adcontextprotocol/adcp/blob/v3.1.1/docs/building/by-layer/L3/error-handling.mdx): use the existing typed error taxonomy and do not expose vendor response bodies or credentials. Vendor-specific mapping: ungraded.

Adnuntius API authority: [API conventions](https://docs.adnuntius.com/adnuntius-advertising/admin-api/api-requests),
[orders](https://docs.adnuntius.com/adnuntius-advertising/admin-api/endpoints/orders),
[line items](https://docs.adnuntius.com/adnuntius-advertising/admin-api/endpoints/lineitems),
[creatives](https://docs.adnuntius.com/adnuntius-advertising/admin-api/endpoints/creatives),
and [stats](https://docs.adnuntius.com/adnuntius-advertising/admin-api/endpoints/stats).

## Configuration

Select adapter type `adnuntius`. Connection settings are stored in the existing
`AdapterConfig.config_json` column; no database migration is required. The
authenticated admin endpoint `POST /api/tenant/<tenant_id>/adapter-config`
accepts this body (prepend the deployment's URL prefix when applicable):

```json
{
  "adapter_type": "adnuntius",
  "config": {
    "network_id": "your-network-id",
    "api_key": "your-scoped-api-key",
    "currency": "NOK",
    "time_zone": "Europe/Oslo",
    "layout_id": "your-third-party-html-layout-id",
    "manual_approval_required": false
  }
}
```

Use the network's actual IANA time zone: Adnuntius budget dates use its calendar,
and their end date is inclusive. The adapter keeps the flight's exact start and
exclusive end instants. `currency` defaults to USD; use the same currency on
this tenant's pricing options. `api_url` defaults to
`https://api.adnuntius.com/api/v1`; an operator may set it for another Adnuntius
environment. All destinations still pass through the shared egress gateway.

Set each buyer's `Principal.platform_mappings` to include:

```json
{"adnuntius": {"advertiser_id": "your-advertiser-id"}}
```

The advertiser mapping is required. There is no shared default advertiser.
Configure each product's existing `implementation_config` with its tier and
inventory, using Adnuntius **external IDs**:

```json
{"tier_id": "your-tier-id", "ad_unit_ids": ["your-ad-unit-id"], "site_ids": []}
```

At least one ad unit or site is required. The publisher configures delivery
priority through the tier. Product settings travel to the adapter on both
immediate creation and the manual approval replay. Connection and product
schemas are registered for the existing schema API; this adapter has no custom
configuration page or inventory picker.

## Supported operations and limits

Publisher-network bookings: one order per buy, one line item per package,
CPM display pricing, explicit tier and inventory configuration, creative
attachment, delivery reporting, pause/resume and package budget/impression updates.
Order pause/resume changes the order only and preserves individually paused
packages. Package updates touch only the named fields. A failed partial booking
attempt deactivates its order; failed cleanup is logged for an operator. API
mutations are never automatically retried.

Hosted display images and HTML snippets use the configured third-party HTML
layout. The adapter does not fetch buyer-provided image URLs. Creative uploads
respect package assignments; reuse creates a separate creative under each
target line item, as Adnuntius requires. Uploaded creative status returns the
remote ID separately from the source asset ID. Reuse of creatives with uploaded
binary assets is refused because those assets require a separate copy operation.

Delivery uses measured impressions, clicks and cost, rather than estimates from
CPM. Package IDs are stored as labels on line items, so reporting and updates
work across adapter instances without an in-memory cache or adapter DB queries.

Marketplace buying, buyer targeting overlays, non-CPM pricing, forecasting,
inventory synchronization, creative format discovery, webhooks and delivery
snapshots are outside the initial implementation. Unsupported pricing and
targeting are refused before booking. Test the configuration on a dedicated
Adnuntius network before enabling production bookings; automated checks use a
local HTTPS vendor server and PostgreSQL, and do not create live campaigns.

See [the security boundary](../../src/adapters/adnuntius/security.md).
