"""Storage contract for legacy columns versus schema-driven adapter config.

The subject is which persisted configuration source wins, rather than the
capabilities wire format; exercise the repository read against PostgreSQL.
"""

import pytest

from src.core.helpers.adapter_helpers import resolve_manual_approval_signal
from tests.factories import AdapterConfigFactory, TenantFactory
from tests.harness._base import IntegrationEnv


@pytest.mark.parametrize("adapter_type", ["adnuntius", "triton"])
@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, False),
        ({"manual_approval_required": None}, False),
        ({"manual_approval_required": False}, False),
        ({"manual_approval_required": True}, True),
    ],
)
def test_unmapped_adapters_read_json_approval_setting(integration_db, adapter_type, config, expected):
    with IntegrationEnv() as env:
        tenant = TenantFactory(tenant_id=env.tenant_id, ad_server=adapter_type, human_review_required=False)
        AdapterConfigFactory(tenant=tenant, adapter_type=adapter_type, config_json=config)
        context = TenantFactory.make_tenant(
            tenant_id=tenant.tenant_id, ad_server=adapter_type, human_review_required=False
        )

        assert resolve_manual_approval_signal(context) is expected


@pytest.mark.parametrize(
    ("adapter_type", "column"),
    [
        ("google_ad_manager", "gam_manual_approval_required"),
        ("kevel", "kevel_manual_approval_required"),
        ("mock", "mock_manual_approval_required"),
    ],
)
@pytest.mark.parametrize("stored", [True, False])
def test_legacy_approval_column_wins_over_json(integration_db, adapter_type, column, stored):
    with IntegrationEnv() as env:
        tenant = TenantFactory(tenant_id=env.tenant_id, ad_server=adapter_type, human_review_required=False)
        AdapterConfigFactory(
            tenant=tenant,
            adapter_type=adapter_type,
            config_json={"manual_approval_required": stored is not True},
            **{column: stored},
        )
        context = TenantFactory.make_tenant(
            tenant_id=tenant.tenant_id, ad_server=adapter_type, human_review_required=False
        )

        assert resolve_manual_approval_signal(context) is (stored is True)


@pytest.mark.parametrize("human_review_required", [False, True])
def test_missing_adapter_config_uses_tenant_review_setting(integration_db, human_review_required):
    with IntegrationEnv() as env:
        tenant = TenantFactory(
            tenant_id=env.tenant_id, ad_server="adnuntius", human_review_required=human_review_required
        )
        context = TenantFactory.make_tenant(
            tenant_id=tenant.tenant_id, ad_server=tenant.ad_server, human_review_required=human_review_required
        )

        assert resolve_manual_approval_signal(context) is human_review_required


def test_tenant_review_overrides_json_setting(integration_db):
    with IntegrationEnv() as env:
        tenant = TenantFactory(tenant_id=env.tenant_id, ad_server="adnuntius", human_review_required=True)
        AdapterConfigFactory(tenant=tenant, adapter_type="adnuntius", config_json={"manual_approval_required": False})
        context = TenantFactory.make_tenant(
            tenant_id=tenant.tenant_id, ad_server=tenant.ad_server, human_review_required=True
        )

        assert resolve_manual_approval_signal(context) is True
