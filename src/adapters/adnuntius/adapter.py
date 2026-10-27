"""Publisher-network bookings on the Adnuntius Admin API."""

import logging
import math
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfoNotFoundError

from pydantic import ValidationError

from src.adapters.adnuntius.client import AdnuntiusClient
from src.adapters.adnuntius.mapping import (
    CREATIVE_LABEL,
    build_line_item,
    creative_id_for_line,
    creative_payload,
    label_value,
    money_budget,
)
from src.adapters.adnuntius.schemas import AdnuntiusConnectionConfig, AdnuntiusProductConfig
from src.adapters.base import (
    AdapterCapabilities,
    AdapterCreateRequest,
    AdapterCreateResult,
    AdapterUpdateResult,
    AdServerAdapter,
    CreativeEngineAdapter,
    TargetingCapabilities,
)
from src.adapters.utils.pricing import resolve_package_rate
from src.core.errors.details import CapabilityRefusalDetails, EntityRefDetails, ErrorProblem
from src.core.exceptions import (
    AdCPAdapterError,
    AdCPAdapterResourceNotFoundError,
    AdCPCapabilityNotSupportedError,
    AdCPConfigurationError,
    AdCPMediaBuyNotFoundError,
    AdCPPackageNotFoundError,
    AdCPSalesAgentError,
    AdCPValidationError,
)
from src.core.schemas import (
    AdapterGetMediaBuyDeliveryResponse,
    AdapterPackageDelivery,
    AffectedPackage,
    AssetStatus,
    CheckMediaBuyStatusResponse,
    DeliveryTotals,
    MediaPackage,
    Principal,
    ReportingPeriod,
)

logger = logging.getLogger(__name__)


class AdnuntiusAdapter(AdServerAdapter):
    adapter_name = "adnuntius"
    default_channels = ["display"]
    default_delivery_measurement = {"provider": "Adnuntius"}
    connection_config_class = AdnuntiusConnectionConfig
    product_config_class = AdnuntiusProductConfig
    capabilities = AdapterCapabilities(
        inventory_entity_label="Ad Units",
        supports_geo_targeting=False,
        supported_pricing_models=["cpm"],
    )

    def __init__(
        self,
        config: dict[str, Any],
        principal: Principal,
        creative_engine: CreativeEngineAdapter | None = None,
        tenant_id: str | None = None,
    ):
        super().__init__(config, principal, creative_engine, tenant_id)
        self.advertiser_id = self._require_config(principal.get_adapter_id(self.adapter_name), field="advertiser_id")
        try:
            self.settings = AdnuntiusConnectionConfig.model_validate(config)
        except (ValidationError, ZoneInfoNotFoundError, ValueError):
            # A Pydantic exception can embed the entire input, including the API
            # key. Do not attach it to the exception chain or the boundary log.
            raise AdCPConfigurationError() from None
        self.client = AdnuntiusClient(
            api_url=self.settings.api_url,
            api_key=self.settings.api_key,
            network_id=self.settings.network_id,
        )

    @staticmethod
    def get_targeting_capabilities() -> TargetingCapabilities:
        return TargetingCapabilities()

    def validate_product_config(self, config: dict[str, Any]) -> tuple[bool, str | None]:
        try:
            AdnuntiusProductConfig.model_validate(config)
        except ValidationError as exc:
            return False, str(exc)
        return True, None

    def _product_config(self, package: MediaPackage) -> AdnuntiusProductConfig:
        try:
            return AdnuntiusProductConfig.model_validate(package.implementation_config)
        except ValidationError as exc:
            raise AdCPConfigurationError(field="implementation_config", internal_detail=exc) from exc

    def validate_media_buy_request(
        self,
        request: AdapterCreateRequest,
        packages: list[MediaPackage],
        start_time: datetime,
        end_time: datetime,
        package_pricing_info: dict[str, dict] | None = None,
    ) -> list[ErrorProblem]:
        problems = super().validate_media_buy_request(request, packages, start_time, end_time, package_pricing_info)
        for package in packages:
            self._product_config(package)
            pricing = (package_pricing_info or {}).get(package.package_id, {})
            if pricing.get("currency", self.settings.currency) != self.settings.currency:
                raise AdCPCapabilityNotSupportedError(details=CapabilityRefusalDetails(capability="currency"))
            rate = resolve_package_rate(package, package_pricing_info)
            if not math.isfinite(rate) or rate <= 0 or package.impressions <= 0:
                raise AdCPValidationError(field="packages")
            if package.budget is not None and (not math.isfinite(package.budget) or package.budget <= 0):
                raise AdCPValidationError(field="packages")
            overlay = package.targeting_overlay
            if overlay and any(getattr(overlay, field) for field in type(overlay).model_fields):
                raise AdCPCapabilityNotSupportedError(details=CapabilityRefusalDetails(capability="targeting_overlay"))
        if not packages or start_time.tzinfo is None or end_time.tzinfo is None or end_time <= start_time:
            raise AdCPValidationError(field="start_time")
        return problems

    def create_media_buy(
        self,
        request: AdapterCreateRequest,
        packages: list[MediaPackage],
        start_time: datetime,
        end_time: datetime,
        package_pricing_info: dict[str, dict] | None = None,
    ) -> AdapterCreateResult:
        problems = self.validate_media_buy_request(request, packages, start_time, end_time, package_pricing_info)
        if problems:
            raise AdCPCapabilityNotSupportedError(details=CapabilityRefusalDetails(capability="pricing_model"))
        order = self.client.save(
            "orders",
            uuid4().hex,
            {
                "name": request.po_number or f"AdCP - {self.principal.name}",
                "advertiser": {"id": self.advertiser_id},
                "objectState": "ACTIVE",
                "labels": [f"adcp:tenant:{self.tenant_id}", f"adcp:principal:{self.principal_id}"],
            },
        )
        order_id = self._remote_id(order)
        line_ids: dict[str, str] = {}
        try:
            for package in packages:
                payload = build_line_item(
                    package,
                    self._product_config(package),
                    order_id=order_id,
                    start=start_time,
                    end=end_time,
                    currency=self.settings.currency,
                    rate=resolve_package_rate(package, package_pricing_info),
                    time_zone=self.settings.time_zone,
                )
                line = self.client.save("lineitems", uuid4().hex, payload)
                line_ids[package.package_id] = self._remote_id(line)
        except AdCPSalesAgentError:
            # A partially created order must not start spending. Keep the original
            # failure when compensation itself fails, and record both for operations.
            try:
                self.client.save("orders", order_id, {"objectState": "INACTIVE"})
            except AdCPSalesAgentError:
                logger.exception("Failed to deactivate partially created Adnuntius order %s", order_id)
            raise
        self._audit("create_media_buy", order_id)
        return self._build_create_success(order_id, packages, platform_line_item_ids=line_ids)

    @staticmethod
    def _remote_id(data: dict[str, Any]) -> str:
        value = data.get("id")
        if not isinstance(value, str) or not value:
            raise AdCPAdapterError()
        return value

    def _owned_order(self, order_id: str) -> dict[str, Any]:
        try:
            order = self.client.get("orders", order_id)
        except AdCPAdapterResourceNotFoundError as exc:
            raise AdCPMediaBuyNotFoundError(details=EntityRefDetails(media_buy_id=order_id)) from exc
        if str(order.get("advertiser", {}).get("id")) != self.advertiser_id:
            raise AdCPMediaBuyNotFoundError(details=EntityRefDetails(media_buy_id=order_id))
        return order

    def _order_lines(self, order_id: str) -> list[dict[str, Any]]:
        rows = self.client.list("lineitems", order=order_id)
        # Do not trust an upstream filter to establish the ownership of nested rows.
        return [row for row in rows if row.get("order", {}).get("id") == order_id and label_value(row)]

    def _owned_line(self, line_id: str) -> dict[str, Any]:
        line = self.client.get("lineitems", line_id)
        self._owned_order(str(line.get("order", {}).get("id", "")))
        return line

    def _audit(self, operation: str, order_id: str) -> None:
        self.audit_logger.log_operation(
            operation,
            self.principal.name,
            self.principal_id,
            self.advertiser_id,
            details={"media_buy_id": order_id},
        )

    def add_creative_assets(
        self, media_buy_id: str, assets: list[dict[str, Any]], today: datetime
    ) -> list[AssetStatus]:
        self._owned_order(media_buy_id)
        lines = self._order_lines(media_buy_id)
        layout = self._require_config(self.settings.layout_id, field="layout_id")
        planned: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
        for asset in assets:
            try:
                payload = creative_payload(asset, layout_id=layout)
            except (KeyError, TypeError, ValueError) as exc:
                raise AdCPValidationError(field="assets", internal_detail=exc) from exc
            assignments = {item["package_id"] for item in asset.get("package_assignments", [])}
            selected = [line for line in lines if not assignments or label_value(line) in assignments]
            if not selected or assignments - {label_value(line) for line in selected}:
                raise AdCPPackageNotFoundError(details=EntityRefDetails(media_buy_id=media_buy_id))
            planned.append((asset, payload, selected))
        statuses = []
        for asset, payload, selected in planned:
            remote_ids = []
            for line in selected:
                line_id = self._remote_id(line)
                # Adnuntius owns a creative under ONE line item. Copies therefore
                # use a stable per-line id, so a repeated attachment updates its copy.
                creative_id = creative_id_for_line(self.tenant_id, line_id, asset["creative_id"])
                result = self.client.save("creatives", creative_id, {**payload, "lineItem": {"id": line_id}})
                remote_ids.append(self._remote_id(result))
            statuses.append(AssetStatus(asset_id=asset["creative_id"], creative_id=remote_ids[0], status="active"))
        self._audit("add_creative_assets", media_buy_id)
        return statuses

    def associate_creatives(self, line_item_ids: list[str], platform_creative_ids: list[str]) -> list[dict[str, Any]]:
        lines = [self._owned_line(line_id) for line_id in line_item_ids]
        sources = []
        for creative_id in platform_creative_ids:
            source = self.client.get("creatives", creative_id)
            self._owned_line(str(source.get("lineItem", {}).get("id", "")))
            if source.get("constraintsToAssets"):
                raise AdCPCapabilityNotSupportedError(details=CapabilityRefusalDetails(capability="asset_copy"))
            sources.append(source)
        results = []
        fields = (
            "name",
            "layout",
            "width",
            "height",
            "dimensionType",
            "layoutParameters",
            "thirdPartyContent",
            "constraintsToText",
            "constraintsToUrls",
            "impressionTrackingUrls",
            "userState",
            "targeting",
            "labels",
        )
        for line in lines:
            line_id = self._remote_id(line)
            for source in sources:
                creative_id = self._remote_id(source)
                if source.get("lineItem", {}).get("id") != line_id:
                    creative_key = label_value(source, CREATIVE_LABEL) or creative_id
                    copy_id = creative_id_for_line(self.tenant_id, line_id, creative_key)
                    self.client.save(
                        "creatives",
                        copy_id,
                        {
                            **{field: source[field] for field in fields if field in source},
                            "lineItem": {"id": line_id},
                        },
                    )
                results.append({"line_item_id": line_id, "creative_id": creative_id, "status": "success"})
        for order_id in {line["order"]["id"] for line in lines}:
            self._audit("associate_creatives", order_id)
        return results

    def check_media_buy_status(self, media_buy_id: str, today: datetime) -> CheckMediaBuyStatusResponse:
        order = self._owned_order(media_buy_id)
        lines = self._order_lines(media_buy_id)
        creatives = self.client.list("creatives", order=media_buy_id)
        if order.get("objectState") != "ACTIVE":
            status = "paused"
        elif lines and all(line.get("userState") == "PAUSED" for line in lines):
            status = "paused"
        elif lines and all(line.get("executionState") == "ENDED" for line in lines):
            status = "completed"
        elif not creatives:
            status = "pending_creative"
        elif any(line.get("executionState") == "RUNNING" for line in lines):
            status = "active"
        elif any(line.get("executionState") == "NOT_READY" for line in lines):
            status = "failed"
        else:
            status = "pending_start"
        return CheckMediaBuyStatusResponse(media_buy_id=media_buy_id, status=status, creative_count=len(creatives))

    def get_media_buy_delivery(
        self,
        media_buy_id: str,
        date_range: ReportingPeriod,
        today: datetime,
    ) -> AdapterGetMediaBuyDeliveryResponse:
        self._owned_order(media_buy_id)
        lines = self._order_lines(media_buy_id)
        data = self.client.request(
            "GET",
            "/stats",
            params={
                "orderId": media_buy_id,
                "startDate": date_range.start.isoformat(),
                "endDate": date_range.end.isoformat(),
                "groupBy": "LINE_ITEM",
                "aggregateTo": "LINE_ITEM",
                "includeAll": "true",
                "currency": self.settings.currency,
            },
        )
        metrics = {chunk["lineItem"]["id"]: chunk for chunk in data.get("chunks", []) if "lineItem" in chunk}
        by_package = []
        clicks = 0
        for line in lines:
            metric = metrics.get(self._remote_id(line), {})
            cost = metric.get("cost", {"amount": 0, "currency": self.settings.currency})
            if cost["currency"] != self.settings.currency:
                raise AdCPAdapterError()
            by_package.append(
                AdapterPackageDelivery(
                    package_id=label_value(line),
                    impressions=metric.get("impressions", 0),
                    spend=cost["amount"],
                )
            )
            clicks += metric.get("clicks", 0)
        return AdapterGetMediaBuyDeliveryResponse(
            media_buy_id=media_buy_id,
            reporting_period=date_range,
            totals=DeliveryTotals(
                impressions=sum(p.impressions for p in by_package),
                spend=sum(p.spend for p in by_package),
                clicks=clicks,
            ),
            by_package=by_package,
            currency=self.settings.currency,
        )

    def update_media_buy(
        self,
        media_buy_id: str,
        action: str,
        package_id: str | None,
        budget: int | None,
        today: datetime,
    ) -> AdapterUpdateResult:
        supported = {
            "pause_media_buy",
            "resume_media_buy",
            "pause_package",
            "resume_package",
            "update_package_budget",
            "update_package_impressions",
            "activate_order",
            "archive_order",
        }
        if action not in supported:
            raise AdCPCapabilityNotSupportedError(
                details=CapabilityRefusalDetails(
                    capability="update_action",
                    rejected_value=action,
                    accepted_values=sorted(supported),
                )
            )
        self._owned_order(media_buy_id)
        lines = self._order_lines(media_buy_id)
        if action in {"pause_media_buy", "resume_media_buy", "activate_order", "archive_order"}:
            state = "ACTIVE" if action in {"resume_media_buy", "activate_order"} else "INACTIVE"
            if action == "archive_order":
                state = "HIDDEN"
            self.client.save("orders", media_buy_id, {"objectState": state})
            affected = [AffectedPackage(package_id=label_value(line)) for line in lines]
        else:
            line = next((row for row in lines if label_value(row) == package_id), None)
            if line is None:
                raise AdCPPackageNotFoundError(
                    details=EntityRefDetails(media_buy_id=media_buy_id, package_id=package_id)
                )
            payload: dict[str, Any]
            if action in {"pause_package", "resume_package"}:
                payload = {"userState": "PAUSED" if action == "pause_package" else "APPROVED"}
            else:
                if budget is None or budget <= 0:
                    raise AdCPValidationError(field="budget")
                payload = (
                    {
                        "budget": money_budget(
                            budget,
                            self.settings.currency,
                            start=datetime.fromisoformat(line["startDate"]),
                            end=datetime.fromisoformat(line["endDate"]),
                            time_zone=self.settings.time_zone,
                        )
                    }
                    if action == "update_package_budget"
                    else {"objectives": {**line.get("objectives", {}), "IMPRESSION": budget}}
                )
            self.client.save("lineitems", self._remote_id(line), payload)
            affected = [AffectedPackage(package_id=package_id)]
        self._audit(action, media_buy_id)
        return AdapterUpdateResult(media_buy_id=media_buy_id, affected_packages=affected)
