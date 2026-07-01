from pathlib import Path


def test_payment_arrangement_templates_include_currency_with_amounts():
    detail = Path("templates/admin/billing/payment_arrangement_detail.html").read_text()
    listing = Path("templates/admin/billing/payment_arrangements.html").read_text()

    assert "arrangement.invoice.currency" in detail
    assert (
        'arrangement_currency ~ " " ~ "{:,.2f}".format(arrangement.total_amount)'
        in detail
    )
    assert (
        'arrangement_currency ~ " " ~ "{:,.2f}".format(arrangement.installment_amount)'
        in detail
    )
    assert '{{ arrangement_currency }} {{ "{:,.2f}".format(inst.amount) }}' in detail
    assert "Record payment for installment" in detail
    assert "arrangement_currency" in listing
    assert (
        '{{ arrangement_currency }} {{ "{:,.2f}".format(arr.total_amount) }}' in listing
    )
