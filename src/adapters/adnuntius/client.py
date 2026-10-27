"""Adnuntius Admin API access through the shared vendor HTTP gateway."""

from typing import Any
from urllib.parse import quote

from pydantic import JsonValue

from src.adapters.vendor_http import VendorHttpClient
from src.core.errors.details import AdapterFailureDetails, ConfigurationDetails
from src.core.exceptions import (
    AdCPAdapterError,
    AdCPAdapterResourceNotFoundError,
    AdCPConfigurationError,
    AdCPRateLimitError,
)
from src.core.security.outbound_http import OutboundDeliveryFailed, QueryParams


def object_path(resource: str, object_id: str) -> str:
    return f"/{resource}/{quote(object_id, safe='')}"


class AdnuntiusClient:
    def __init__(self, *, api_url: str, api_key: str, network_id: str):
        self._vendor = VendorHttpClient(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            params={"context": network_id},
        )

    def request(
        self, method: str, path: str, *, payload: JsonValue | None = None, params: QueryParams | None = None
    ) -> dict[str, Any]:
        try:
            result = self._vendor.call(method, path, json=payload, params=params)
        except OutboundDeliveryFailed as exc:
            details = AdapterFailureDetails(status_code=exc.http_status)
            if exc.http_status in {401, 403}:
                raise AdCPConfigurationError(details=ConfigurationDetails(provider="adnuntius")) from exc
            if exc.http_status == 404:
                raise AdCPAdapterResourceNotFoundError(details=details) from exc
            if exc.http_status == 429:
                raise AdCPRateLimitError(details=details, retry_after=exc.retry_after) from exc
            raise AdCPAdapterError(details=details) from exc
        try:
            data = result.json()
        except ValueError as exc:
            raise AdCPAdapterError(internal_detail=exc) from exc
        if not isinstance(data, dict):
            raise AdCPAdapterError()
        return data

    def get(self, resource: str, object_id: str) -> dict[str, Any]:
        return self.request("GET", object_path(resource, object_id))

    def save(self, resource: str, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", object_path(resource, object_id), payload=payload)

    def list(self, resource: str, **filters: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 0
        while True:
            data = self.request("GET", f"/{resource}", params={**filters, "page": page, "pageSize": 100})
            batch = data.get("results")
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise AdCPAdapterError()
            rows.extend(batch)
            if len(rows) >= data.get("totalCount", len(rows)):
                return rows
            if not batch:
                raise AdCPAdapterError()
            page += 1
