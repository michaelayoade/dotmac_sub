from pathlib import Path

TEMPLATE = Path("templates/admin/vendors/operations.html")
QUOTE_TEMPLATE = Path("templates/admin/vendors/quote_review_detail.html")
INVOICE_TEMPLATE = Path("templates/admin/vendors/invoice_review_detail.html")
VENDOR_FORM_TEMPLATE = Path("templates/admin/vendors/vendor_form.html")
ADMIN_VENDOR_OPERATIONS = Path("app/web/admin/vendor_operations.py")


def test_vendor_operations_search_and_procurement_controls_are_visibly_spaced():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert 'name="view" value="{{ active_vendor_operations_view }}"' in template
    assert "vendor_operations_tabs" in template
    assert "active_vendor_operations_label" in template
    assert 'href="{{ tab.href }}"' in template
    assert 'class="mt-4 flex flex-col gap-3 sm:flex-row"' in template
    assert 'id="vendor-operations-search"' in template
    search_input = template.split('id="vendor-operations-search"', maxsplit=1)[1]
    search_input = search_input.split(">", maxsplit=1)[0]
    assert "pl-3" in search_input
    assert 'class="grid gap-4 border-t border-slate-100' in template
    assert template.count("border border-slate-400") >= 4
    assert template.count("focus:border-teal-500") >= 4
    assert 'name="mode"' in template
    assert 'name="vendor_id"' in template
    assert 'name="bidding_close_at"' in template
    bidding_close_input = template.split('name="bidding_close_at"', maxsplit=1)[1]
    bidding_close_input = bidding_close_input.split(">", maxsplit=1)[0]
    assert "pr-10" in bidding_close_input
    calendar_icon = template.split("data-bidding-close-calendar-icon", maxsplit=1)[
        1
    ].split(">", maxsplit=1)[0]
    assert "pointer-events-none" in calendar_icon
    assert "dark:text-slate-300" in calendar_icon
    assert "dark:bg-slate-900" in calendar_icon


def test_vendor_form_submit_button_has_visible_text():
    template = VENDOR_FORM_TEMPLATE.read_text(encoding="utf-8")

    assert "submit_button(submit_label" not in template
    assert '<button type="submit"' in template
    assert "{{ submit_label }}</button>" in template
    assert "bg-cyan-600" in template


def test_vendor_operations_sections_are_filtered_by_active_view():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "active_vendor_operations_view == 'procurement'" in template
    assert "active_vendor_operations_view == 'quotes'" in template
    assert "active_vendor_operations_view == 'routes'" in template
    assert "active_vendor_operations_view == 'as-built'" in template
    assert "active_vendor_operations_view == 'materials'" in template
    assert "active_vendor_operations_view == 'advances'" in template
    assert "active_vendor_operations_view == 'invoices'" in template
    assert "active_vendor_operations_view == 'verification'" in template
    assert template.count("Vendor advances awaiting review") == 1
    assert 'class="grid items-start gap-6 lg:grid-cols-2"' not in template


def test_vendor_operations_route_accepts_view_filter_and_builds_tabs():
    route = ADMIN_VENDOR_OPERATIONS.read_text(encoding="utf-8")

    assert "class VendorOperationsTab" in route
    assert "_operations_tab_href" in route
    assert "_matches_queue_search" in route
    assert "view: str | None = Query(default=None, max_length=32)" in route
    assert '"active_vendor_operations_view": requested_view' in route
    assert '"vendor_operations_tabs": vendor_operations_tabs' in route
    assert 'invoice.get("invoice_number")' in route
    assert "release.project.name" in route
    assert (
        '("procurement", "Procurement", len(draft_projects), show_field_reviews)'
        in route
    )
    assert '("quotes", "Quotes", len(quotes), show_field_reviews)' in route
    assert '("invoices", "Invoices", len(invoices), show_financial_reviews)' in route


def test_material_issue_queue_has_one_button_form():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "Approved materials awaiting issue" in template
    assert "material_releases_awaiting_issue" in template
    assert 'action="{{ release.issue_action.preview_url }}"' in template
    assert 'name="issue_source"' in template
    assert 'value="dotmac_store"' in template
    assert 'value="erp"' in template
    assert 'name="issue_reference"' in template
    assert 'name="issued_quantity_{{ item.id }}"' in template
    assert "{{ release.issue_action.label }}" in template


def test_draft_procurement_rows_expand_to_reveal_the_existing_form():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert '<details name="draft-procurement-project"' in template
    assert "group-open:rotate-180" in template
    assert (
        'action="/admin/vendors/operations/projects/{{ project.id }}/procurement"'
        in template
    )
    assert (
        'href="/admin/projects/{{ project.project.number or project.project.id }}"'
        in template
    )
    assert 'onclick="event.stopPropagation()"' in template
    assert ">View project</a>" in template


def test_quote_revision_note_control_is_visible_and_required_for_revision_only():
    template = QUOTE_TEMPLATE.read_text(encoding="utf-8")

    textarea = template.split('id="quote-review-notes"', maxsplit=1)[1]
    textarea = textarea.split(">", maxsplit=1)[0]
    assert "border border-slate-400" in textarea
    assert "bg-white" in textarea
    assert "focus:border-teal-500" in textarea
    assert "data-quote-review-note" in textarea
    assert "data-quote-review-revision" in template
    assert "data-quote-review-approve" in template
    assert 'setAttribute("required", "required")' in template
    assert 'removeAttribute("required")' in template


def test_invoice_advance_quote_refusal_returns_review_page_alert():
    route = ADMIN_VENDOR_OPERATIONS.read_text(encoding="utf-8")
    template = INVOICE_TEMPLATE.read_text(encoding="utf-8")

    assert (
        "operations.vendor_purchase_invoices.invoice_exceeds_quote_net_of_advances"
        in route
    )
    assert "error_message=exc.message" in route
    assert "raise _quote_error(exc) from exc" in route
    assert '"error_message": error_message' in route
    assert "{% if error_message %}" in template
    assert 'role="alert"' in template
    assert "border-red-200" in template
