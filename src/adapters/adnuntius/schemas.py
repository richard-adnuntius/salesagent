"""Tenant connection and product inventory settings for Adnuntius."""

from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from src.adapters.base import BaseConnectionConfig, BaseProductConfig


class AdnuntiusConnectionConfig(BaseConnectionConfig):
    network_id: str = Field(min_length=1, description="Adnuntius network context")
    api_key: str = Field(min_length=1, repr=False, json_schema_extra={"secret": True})
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    time_zone: str = Field(description="Network IANA time zone, used for budget calendar dates")
    api_url: str = Field(default="https://api.adnuntius.com/api/v1", min_length=1)
    layout_id: str | None = Field(default=None, description="Third-party HTML layout for hosted display creatives")

    @model_validator(mode="after")
    def valid_time_zone(self) -> Self:
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise PydanticCustomError("time_zone", "Use an IANA time zone") from None
        return self


class AdnuntiusProductConfig(BaseProductConfig):
    tier_id: str = Field(min_length=1, description="Delivery tier configured on this publisher network")
    ad_unit_ids: list[str] = Field(default_factory=list)
    site_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_inventory(self) -> Self:
        if not (self.ad_unit_ids or self.site_ids):
            raise PydanticCustomError("inventory", "Adnuntius products require ad_unit_ids or site_ids")
        if any(not value for value in [*self.ad_unit_ids, *self.site_ids]):
            raise PydanticCustomError("inventory", "Adnuntius inventory IDs must be nonempty")
        return self
