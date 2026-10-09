from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.service_period_purchase import ServicePeriodPurchasePreviewRequest


@pytest.mark.parametrize("count", [True, False, 0, 13, "2", 1.5])
def test_period_count_requires_an_integer_in_the_supported_range(count):
    with pytest.raises(ValidationError):
        ServicePeriodPurchasePreviewRequest(subscription_id=uuid4(), period_count=count)


def test_preview_preserves_typed_service_identity():
    subscription_id = uuid4()
    result = ServicePeriodPurchasePreviewRequest(
        subscription_id=str(subscription_id), period_count=12
    )
    assert result.subscription_id == subscription_id
