import json

import frappe
from frappe import _
from datetime import datetime
from time import perf_counter
from frappe.utils import nowdate, now_datetime
import requests
from requests.exceptions import RequestException
from urllib.parse import urlparse

FBR_MODE_SANDBOX = "sandbox testing"
FBR_MODE_PRODUCTION = "production"


def _append_mode_validation_error(errors):
    configured_mode = (
        frappe.db.get_single_value("FBR E-Inv Setup", "mode")
        if frappe.db.exists("DocType", "FBR E-Inv Setup")
        else ""
    )
    normalized_mode = (configured_mode or "").strip().casefold()
    if normalized_mode in {FBR_MODE_SANDBOX, FBR_MODE_PRODUCTION}:
        return

    configured_display = (configured_mode or "").strip() or "blank"
    errors.append(
        _(
            "Invalid Mode in FBR E-Inv Setup (current: {0}). "
            "Please set Mode to Sandbox Testing or Production."
        ).format(configured_display)
    )


def validate_fbr_fields(doc, method):
    """Validate FBR required fields before saving Sales Invoice"""
    if not doc.custom_submit_to_fbr:
        return

    errors = _collect_sales_invoice_errors(doc, show_messages=True)

    # If there are validation errors, prevent save
    if errors:
        frappe.throw("<br>".join(errors), title=_("FBR Validation Failed"))


def _collect_sales_invoice_errors(doc, show_messages=False):
    # Intentionally retained as requested: posting date is forced on validate.
    doc.posting_date = nowdate()
    errors = []

    # Check if FBR setup is configured
    fbr_settings = frappe.get_single("FBR E-Inv Setup")
    if not fbr_settings.api_endpoint:
        errors.append(_("FBR API endpoint not configured in FBR E-Inv Setup"))
    _append_mode_validation_error(errors)

    # Check required FBR fields
    if not doc.custom_province:
        errors.append(_("Seller Province is required for FBR submission"))

    if not doc.tax_category:
        errors.append(_("Tax Category (Buyer Province) is required for FBR submission"))

    # Validate customer tax information
    if doc.customer:
        customer = frappe.get_doc("Customer", doc.customer)
        if not customer.tax_id and not customer.custom_province and show_messages:
            frappe.msgprint(
                _(
                    "Customer {0} is missing Tax ID or Province information required for FBR"
                ).format(customer.customer_name),
                alert=True,
                indicator="orange",
            )

    # Validate company tax information
    if doc.company:
        company = frappe.get_doc("Company", doc.company)
        if not company.tax_id:
            errors.append(_("Company Tax ID is required for FBR submission"))

    # Validate items
    validate_fbr_items(doc, errors)
    return errors


def validate_fbr_items(doc, errors):
    """Validate FBR specific item requirements"""
    if not doc.items:
        errors.append(_("At least one item is required for FBR submission"))
        return

    missing_hs_codes = []
    missing_tax_templates = []
    missing_sale_types = []
    for idx, item in enumerate(doc.items, 1):
        # Check HS Code
        if not item.custom_hs_code:
            missing_hs_codes.append(f"Row {idx}: {item.item_name}")

        # Check Item Tax Template (version-16 behavior: presence check only)
        if not item.item_tax_template:
            missing_tax_templates.append(f"Row {idx}: {item.item_name}")

        # Check Sale Type
        if not item.custom_sale_type:
            missing_sale_types.append(f"Row {idx}: {item.item_name}")

    if missing_hs_codes:
        errors.append(
            _("Following items are missing HS Codes required for FBR:<br>{0}").format(
                "<br>".join(missing_hs_codes)
            )
        )

    if missing_tax_templates:
        errors.append(
            _(
                "Following items are missing Item Tax Templates required for FBR:<br>{0}"
            ).format("<br>".join(missing_tax_templates))
        )

    if missing_sale_types:
        errors.append(
            _("Following items are missing Sale Type required for FBR:<br>{0}").format(
                "<br>".join(missing_sale_types)
            )
        )


@frappe.whitelist()
def validate_fbr_document(doctype: str, docname: str):
    """API method to validate a document for FBR compliance"""
    try:
        doc = frappe.get_doc(doctype, docname)
        errors = []

        if doctype == "Sales Invoice":
            errors.extend(_collect_sales_invoice_errors(doc, show_messages=False))
        elif doctype == "POS Invoice":
            validate_pos_invoice_fbr(doc, errors)

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": get_fbr_warnings(doc),
        }

    except Exception as e:
        return {"valid": False, "errors": [str(e)], "warnings": []}


def validate_pos_invoice_fbr(doc, errors):
    """Validate POS Invoice for FBR submission"""
    _append_mode_validation_error(errors)

    # POS Invoice specific validations
    if not doc.customer:
        errors.append(_("Customer is required for FBR submission"))

    if not doc.pos_profile:
        errors.append(_("POS Profile is required"))

    buyer_company = ""
    if doc.pos_profile:
        pos_profile = frappe.get_doc("POS Profile", doc.pos_profile)
        if not pos_profile.company:
            errors.append(_("POS Profile must have a company assigned"))
        else:
            buyer_company = pos_profile.company

    if not doc.tax_category:
        errors.append(_("Tax Category (Seller Province) is required for FBR submission"))

    if buyer_company:
        buyer_company_province = frappe.db.get_value(
            "Company", buyer_company, "custom_province"
        )
        if not buyer_company_province:
            errors.append(_("Buyer Company Province is required for FBR submission"))

        buyer_company_tax_id = frappe.db.get_value("Company", buyer_company, "tax_id")
        if not buyer_company_tax_id:
            errors.append(_("Buyer Company Tax ID is required for FBR submission"))

    if not doc.items:
        errors.append(_("At least one item is required for FBR submission"))
        return

    missing_hs_codes = []
    missing_tax_templates = []
    missing_sale_types = []
    for idx, item in enumerate(doc.items, 1):
        if not item.custom_hs_code:
            missing_hs_codes.append(f"Row {idx}: {item.item_name}")
        if not item.item_tax_template:
            missing_tax_templates.append(f"Row {idx}: {item.item_name}")
        if not item.custom_sale_type:
            missing_sale_types.append(f"Row {idx}: {item.item_name}")

    if missing_hs_codes:
        errors.append(
            _("Following items are missing HS Codes required for FBR:<br>{0}").format(
                "<br>".join(missing_hs_codes)
            )
        )

    if missing_tax_templates:
        errors.append(
            _(
                "Following items are missing Item Tax Templates required for FBR:<br>{0}"
            ).format("<br>".join(missing_tax_templates))
        )

    if missing_sale_types:
        errors.append(
            _("Following items are missing Sale Type required for FBR:<br>{0}").format(
                "<br>".join(missing_sale_types)
            )
        )


def validate_pos_invoice_fields(doc, method=None):
    """Validate POS Invoice FBR fields using server-side hook."""
    if not doc.custom_submit_to_fbr:
        return

    errors = []
    validate_pos_invoice_fbr(doc, errors)
    if errors:
        frappe.throw("<br>".join(errors), title=_("FBR Validation Failed"))


def get_fbr_warnings(doc):
    """Get FBR warnings (non-blocking issues)"""
    warnings = []

    # Check for optimal submission timing
    current_hour = datetime.now().hour
    if current_hour < 6 or current_hour > 22:
        warnings.append(
            _("FBR API may have reduced availability outside business hours")
        )

    # Check for duplicate submission
    if hasattr(doc, "custom_fbr_invoice_number") and doc.custom_fbr_invoice_number:
        warnings.append(_("Document already submitted to FBR"))

    # Check customer payment terms
    if hasattr(doc, "payment_terms_template") and doc.payment_terms_template:
        # Could add specific warnings about payment terms affecting FBR
        pass

    return warnings


@frappe.whitelist()
def check_fbr_api_status():
    """Check if FBR API is accessible"""
    try:
        fbr_settings = frappe.get_single("FBR E-Inv Setup")

        if not fbr_settings.api_endpoint:
            return {"status": "error", "message": _("FBR API endpoint not configured")}

        api_endpoint = (fbr_settings.api_endpoint or "").strip()
        healthcheck_url = _resolve_healthcheck_url(api_endpoint)
        token = (fbr_settings.pral_authorization_token or "").strip()
        verify_ssl = getattr(fbr_settings, "verify_ssl", True)
        connect_timeout = float(getattr(fbr_settings, "connect_timeout", 5.0))
        read_timeout = float(getattr(fbr_settings, "read_timeout", 10.0))

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        start_time = perf_counter()
        response = requests.get(
            healthcheck_url,
            headers=headers,
            timeout=(connect_timeout, read_timeout),
            verify=bool(verify_ssl),
        )
        elapsed_ms = round((perf_counter() - start_time) * 1000, 2)

        response_data = {}
        try:
            response_data = response.json() if response.text else {}
        except ValueError:
            response_data = {}

        api_version = ""
        if isinstance(response_data, dict):
            api_version = str(
                response_data.get("apiVersion")
                or response_data.get("api_version")
                or ""
            )

        if response.status_code >= 400:
            return {
                "status": "error",
                "message": _("FBR API responded with HTTP {0}").format(
                    response.status_code
                ),
                "http_status_code": response.status_code,
                "api_version": api_version,
                "response_time_ms": elapsed_ms,
                "checked_url": healthcheck_url,
            }

        return {
            "status": "success",
            "message": _("FBR API is accessible"),
            "http_status_code": response.status_code,
            "api_version": api_version,
            "response_time_ms": elapsed_ms,
            "checked_url": healthcheck_url,
        }

    except RequestException as e:
        return {"status": "error", "message": f"FBR API check failed: {str(e)}"}
    except Exception as e:
        return {"status": "error", "message": f"FBR API check failed: {str(e)}"}


@frappe.whitelist()
def get_fbr_compliance_report(
    from_date: str | None = None, to_date: str | None = None
):
    """Generate FBR compliance report"""
    try:
        if not from_date:
            from_date = frappe.utils.add_days(frappe.utils.today(), -30)
        if not to_date:
            to_date = frappe.utils.today()

        # Get submitted invoices in date range
        submitted_invoices = frappe.db.sql(
            """
            SELECT 
                'Sales Invoice' as doctype,
                name,
                posting_date,
                customer,
                grand_total,
                custom_fbr_status,
                custom_fbr_invoice_number,
                custom_submit_to_fbr
            FROM `tabSales Invoice`
            WHERE docstatus = 1 
                AND posting_date BETWEEN %s AND %s
                AND custom_submit_to_fbr = 1
            
            UNION ALL
            
            SELECT 
                'POS Invoice' as doctype,
                name,
                posting_date,
                customer,
                grand_total,
                custom_fbr_status,
                custom_fbr_invoice_number,
                custom_submit_to_fbr
            FROM `tabPOS Invoice`
            WHERE docstatus = 1 
                AND posting_date BETWEEN %s AND %s
                AND custom_submit_to_fbr = 1
        """,
            (from_date, to_date, from_date, to_date),
            as_dict=True,
        )

        # Categorize results
        total_invoices = len(submitted_invoices)
        successful = len(
            [inv for inv in submitted_invoices if inv.custom_fbr_status == "Valid"]
        )
        invalid = len(
            [inv for inv in submitted_invoices if inv.custom_fbr_status == "Invalid"]
        )
        errors = len(
            [
                inv
                for inv in submitted_invoices
                if inv.custom_fbr_status in ["Error", "Failed"]
            ]
        )
        pending = len([inv for inv in submitted_invoices if not inv.custom_fbr_status])

        compliance_rate = (
            (successful / total_invoices * 100) if total_invoices > 0 else 0
        )

        return {
            "from_date": from_date,
            "to_date": to_date,
            "total_invoices": total_invoices,
            "successful": successful,
            "invalid": invalid,
            "errors": errors,
            "pending": pending,
            "compliance_rate": round(compliance_rate, 2),
            "invoices": submitted_invoices,
        }

    except Exception as e:
        frappe.log_error(
            f"Error generating FBR compliance report: {str(e)}", "FBR Compliance Report"
        )
        return {"error": str(e)}


def force_today_posting_date(doc, method):
    """Force posting_date/time to 'today' at submit time."""
    doc.set_posting_time = 1
    doc.posting_date = nowdate()
    if hasattr(doc, "posting_time"):
        doc.posting_time = now_datetime().time()


def block_cancel_for_successfully_submitted_fbr_invoice(doc, method=None):
    """
    Prevent cancellation of documents already successfully submitted to FBR.

    Blocking condition:
    - custom_fbr_status is Valid (case-insensitive)
    - custom_fbr_invoice_number is present
    """
    fbr_status = (getattr(doc, "custom_fbr_status", "") or "").strip().lower()
    fbr_invoice_number = (getattr(doc, "custom_fbr_invoice_number", "") or "").strip()

    if fbr_status == "valid" and fbr_invoice_number:
        frappe.throw(
            _(
                "This cannot be cancelled as it has already been successfully submitted to FBR."
            ),
            title=_("Cancellation Not Allowed"),
        )


def cleanup_fbr_queue_on_cancel(doc, method=None):
    """
    On successful cancellation, remove related retryable queue rows and log cancellation.

    Only Pending/Failed queue items are eligible for cleanup; Processing is intentionally
    left untouched.
    """
    try:
        queue_rows = frappe.get_all(
            "FBR Queue",
            filters={
                "document_type": doc.doctype,
                "document_name": doc.name,
                "status": ["in", ["Pending", "Failed"]],
            },
            fields=["name", "status"],
            order_by="creation asc",
            limit_page_length=0,
        )
        if not queue_rows:
            return

        deleted_rows = []
        failed_deletions = []

        for row in queue_rows:
            try:
                frappe.delete_doc("FBR Queue", row.name, ignore_permissions=True)
                deleted_rows.append({"queue_id": row.name, "status": row.status})
            except Exception as e:
                failed_deletions.append({"queue_id": row.name, "error": str(e)})
                frappe.log_error(
                    f"Failed deleting FBR Queue row {row.name} during cancel of {doc.doctype} {doc.name}: {str(e)}",
                    "FBR Cancel Queue Cleanup",
                )

        if not deleted_rows:
            return

        response_payload = {
            "message": "Invoice cancelled before FBR submission; removed related queue items.",
            "removed_queue_items": deleted_rows,
            "removed_count": len(deleted_rows),
        }
        if failed_deletions:
            response_payload["failed_deletions"] = failed_deletions

        log_doc = frappe.new_doc("FBR Logs")
        log_doc.update(
            {
                "document_type": doc.doctype,
                "document_name": doc.name,
                "status": "Cancelled",
                "submitted_at": now_datetime(),
                "response_data": json.dumps(response_payload, indent=2),
            }
        )
        log_doc.insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(
            f"Error cleaning queue/logging cancellation for {doc.doctype} {doc.name}: {str(e)}",
            "FBR Cancel Queue Cleanup",
        )


def _resolve_healthcheck_url(api_endpoint: str) -> str:
    """Use a stable GET-able reference API endpoint for diagnostics."""
    endpoint = (api_endpoint or "").strip()
    if not endpoint:
        return "https://gw.fbr.gov.pk/pdi/v1/provinces"

    parsed = urlparse(endpoint)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/pdi/v1/provinces"

    return "https://gw.fbr.gov.pk/pdi/v1/provinces"
