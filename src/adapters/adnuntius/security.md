# Adnuntius security boundary

- Tenant configuration supplies the network context and scoped API key.
  Authentication uses a Bearer header. Neither credentials nor vendor error
  bodies enter buyer-facing errors. Configuration validation suppresses input
  exceptions that might include the API key.
- All HTTP uses `VendorHttpClient` and the shared egress gateway. An adapter
  supplies no independent URL, DNS, redirect or address policy. IDs in resource
  paths are URL-encoded; context and authentication cannot be overridden by
  per-call query parameters.
- The boundary owns tenant/account authorization. The adapter additionally
  checks the order's advertiser before status, reporting or mutation. Creative
  reuse checks both the source creative's order and each destination's order.
  Foreign and absent orders produce the same not-found error.
- Package inventory and tiers come from seller-owned product configuration.
  Empty inventory is refused rather than booking the entire network. Unsupported
  overlays are refused rather than silently widening the audience.
- Creative content is sent to Adnuntius for serving; the adapter never executes
  HTML or downloads an asset URL. Image URL attributes are HTML-escaped. Existing
  creative targeting is retained when copying to another line item. Network
  creative review and scanning policies remain applicable.
- Mutations make one HTTP attempt. Partial order creation attempts deactivate
  the order before propagating failure; a failed compensation is logged. Writes
  use the base adapter's tenant-scoped audit logger. Persistence remains with
  repositories and the surrounding unit of work.
