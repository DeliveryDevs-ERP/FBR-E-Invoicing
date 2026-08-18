import frappe
from frappe import _

from fbr_e_invoicing.utils import is_pra_enabled, _get_pra_pos_id, PRA_SANDBOX_TOKEN


def is_pra_tax_category(doc):
    """PRA only applies to invoices whose Tax Category matches the resolved
    Company's configured `custom_pra_tax_category` (PRA tab) - e.g. PUNJAB.
    Anything else is out of scope for PRA entirely. If the company hasn't
    configured this field, PRA never applies (matches "Post to PRA" button
    behavior, which also requires it to be set)."""
    company = getattr(doc, "company", None)
    if not company:
        return False
    pra_tax_category = frappe.db.get_value("Company", company, "custom_pra_tax_category")
    if not pra_tax_category:
        return False
    return (doc.get("tax_category") or "").strip() == pra_tax_category.strip()


def auto_fill_sandbox_token(doc, method=None):
    """Write PRA's shared sandbox token into the Company's Access Token field
    when Mode is Sandbox Testing and it's blank, so the form never shows a
    blank field whose behavior is invisible - what's stored is what's sent.
    """
    if doc.get("custom_pra_mode") == "Sandbox Testing" and not doc.get("custom_pra_access_token"):
        doc.custom_pra_access_token = PRA_SANDBOX_TOKEN


def _append_company_pra_config_errors(errors, company):
    """Check that the invoice's Company has the PRA config it needs to post."""
    if not company:
        errors.append(_("Company is required for PRA submission"))
        return

    if not _get_pra_pos_id(company):
        errors.append(
            _("PRA POS ID is not set on Company {0} (PRA tab)").format(company)
        )


def validate_pra_fields(doc, method=None):
    """Validate PRA required fields before submitting a Sales Invoice.

    Only applies to Punjab (Tax Category == PUNJAB) invoices - the "Post to
    PRA" button only ever appears for those, so a misconfigured Company or
    item must fail loudly here rather than silently in the background queue
    once someone clicks it.
    """
    if not is_pra_enabled(getattr(doc, "company", None)):
        return
    if not doc.get("custom_submit_to_pra"):
        return
    if not is_pra_tax_category(doc):
        return

    errors = _collect_pra_errors(doc)
    if errors:
        frappe.throw("<br>".join(errors), title=_("PRA Validation Failed"))


def _collect_pra_errors(doc):
    errors = []
    _append_company_pra_config_errors(errors, getattr(doc, "company", None))
    _validate_pra_items(doc, errors)
    return errors


def _validate_pra_items(doc, errors):
    if not doc.items:
        errors.append(_("At least one item is required for PRA submission"))
        return

    missing_pct_codes = []
    for idx, item in enumerate(doc.items, 1):
        if not item.get("custom_pra_pct_code"):
            missing_pct_codes.append(f"Row {idx}: {item.item_name}")

    if missing_pct_codes:
        errors.append(
            _("Following items are missing PRA PCT Code required for PRA:<br>{0}").format(
                "<br>".join(missing_pct_codes)
            )
        )
