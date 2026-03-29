import frappe
from frappe.utils import flt, formatdate
import re


STANDARD_RATE_SALE_TYPE = "Goods at standard rate (default)"
STANDARD_RATE_SCENARIO_IDS = {"SN001", "SN002", "SN026"}
REDUCED_RATE_SALE_TYPE = "Goods at Reduced Rate"
REDUCED_RATE_SCENARIO_IDS = {"SN005", "SN028"}
THIRD_SCHEDULE_SALE_TYPE = "3rd Schedule Goods"
THIRD_SCHEDULE_SCENARIO_IDS = {"SN008", "SN027"}
SANDBOX_MODE_LABEL = "Sandbox Testing"
PRODUCTION_MODE_LABEL = "Production"
MODE_SANDBOX = "sandbox"
MODE_PRODUCTION = "production"

FBR_MODE_MAP = {
    SANDBOX_MODE_LABEL.casefold(): MODE_SANDBOX,
    PRODUCTION_MODE_LABEL.casefold(): MODE_PRODUCTION,
}


def _mode_error_message(configured_mode: str | None = None) -> str:
    configured = (configured_mode or "").strip()
    configured_display = configured or "blank"
    return (
        "Invalid Mode in 'FBR E-Inv Setup'. "
        f"Current value: '{configured_display}'. "
        f"Please set Mode to '{SANDBOX_MODE_LABEL}' or '{PRODUCTION_MODE_LABEL}'."
    )


def _resolve_fbr_mode_or_throw() -> str:
    configured_mode = (
        frappe.db.get_single_value("FBR E-Inv Setup", "mode")
        if frappe.db.exists("DocType", "FBR E-Inv Setup")
        else ""
    )
    normalized_mode = (configured_mode or "").strip().casefold()
    mode = FBR_MODE_MAP.get(normalized_mode)
    if not mode:
        raise frappe.ValidationError(_mode_error_message(configured_mode))
    return mode


def normalise_cnic(value: str | None) -> str:
    """
    Normalize CNIC/NTN/Tax IDs by removing non-digits (hyphens, spaces, etc.)
    Examples:
      "31303-9589654-7" -> "3130395896547"
      " 31303 9589654 7 " -> "3130395896547"
    """
    if not value:
        return ""
    return re.sub(r"\D", "", str(value)).strip()


def _get_customer_tax_data(customer_name: str | None) -> dict:
    if not customer_name:
        return {}

    return (
        frappe.db.get_value(
            "Customer",
            customer_name,
            ["tax_id", "nic", "ntn", "customer_name"],
            as_dict=True,
        )
        or {}
    )


def _resolve_customer_tax_id(customer_tax_data: dict) -> str:
    buyer_tax_id = customer_tax_data.get("tax_id")
    if not buyer_tax_id and customer_tax_data.get("nic"):
        buyer_tax_id = normalise_cnic(customer_tax_data.get("nic"))
    if not buyer_tax_id and customer_tax_data.get("ntn"):
        buyer_tax_id = customer_tax_data.get("ntn")
    return buyer_tax_id or ""


@frappe.whitelist()
def build_fbr_payload(sales_invoice_name: str):
    """
    Build FBR payload for a Sales Invoice per provided mapping.
    Returns a dict (JSON-serializable).
    """
    doc = frappe.get_doc("Sales Invoice", sales_invoice_name)
    invoice_type = "Debit Note" if getattr(doc, "is_debit_note", 0) else "Sale Invoice"
    return _build_invoice_payload(doc, invoice_type, _get_sales_party_fields(doc))


@frappe.whitelist()
def build_pos_fbr_payload(pos_invoice_name: str):
    """
    Build FBR payload for a POS Invoice using POS-specific party mapping
    and the shared item payload mapping.
    """
    doc = frappe.get_doc("POS Invoice", pos_invoice_name)
    return _build_invoice_payload(doc, "Sale Invoice", _get_pos_party_fields(doc))


def _get_sales_party_fields(doc):
    """Resolve invoice-level party fields for Sales Invoice."""
    customer_tax_data = _get_customer_tax_data(doc.customer)
    buyer_tax_id = _resolve_customer_tax_id(customer_tax_data)

    seller_tax_id = (
        frappe.db.get_value("Company", doc.company, "tax_id") if doc.company else None
    )
    return {
        "sellerNTNCNIC": (seller_tax_id or ""),
        "sellerBusinessName": (doc.company or ""),
        "sellerProvince": (doc.custom_province or ""),
        "sellerAddress": (_get_party_address_text("Company", doc.company) or ""),
        "buyerNTNCNIC": (buyer_tax_id or ""),
        "buyerBusinessName": (customer_tax_data.get("customer_name") or doc.customer_name or ""),
        "buyerProvince": (doc.tax_category or ""),
        "buyerAddress": (_get_party_address_text("Customer", doc.customer) or ""),
        "buyerRegistrationType": "Registered" if buyer_tax_id else "Unregistered",
    }


def _get_pos_party_fields(doc):
    """Resolve invoice-level party fields for POS Invoice."""
    seller_name = (
        frappe.db.get_value("POS Profile", doc.pos_profile, "company")
        if doc.pos_profile
        else None
    )
    seller_tax_id = (
        frappe.db.get_value("Company", seller_name, "tax_id") if seller_name else None
    )
    seller_province = (
        frappe.db.get_value("Company", seller_name, "custom_province")
        if seller_name
        else None
    )
    customer_tax_data = _get_customer_tax_data(doc.customer)
    buyer_tax_id = _resolve_customer_tax_id(customer_tax_data)
    return {
        "sellerNTNCNIC": (seller_tax_id or ""),
        "sellerBusinessName": (seller_name or ""),
        "sellerProvince": (seller_province or ""),
        "sellerAddress": (_get_party_address_text("Company", seller_name) or ""),
        "buyerNTNCNIC": (buyer_tax_id or ""),
        "buyerBusinessName": (customer_tax_data.get("customer_name") or doc.customer_name or ""),
        "buyerProvince": (doc.tax_category or ""),
        "buyerAddress": (_get_party_address_text("Customer", doc.customer) or ""),
        "buyerRegistrationType": "Registered" if buyer_tax_id else "Unregistered",
    }


def _build_invoice_payload(doc, invoice_type: str, party_fields: dict):
    """Shared payload builder used by Sales Invoice and POS Invoice flows."""
    first_sale_type = doc.items[0].custom_sale_type if doc.items else ""
    fbr_mode = _resolve_fbr_mode_or_throw()

    # --- Invoice-level fields ---
    payload = {
        "invoiceType": invoice_type,
        "invoiceDate": formatdate(doc.posting_date, "yyyy-mm-dd"),
        **party_fields,
        "invoiceRefNo": "",
        "items": [],
    }
    if fbr_mode == MODE_SANDBOX:
        scenario_id = get_scenario_id(first_sale_type)
        if not scenario_id:
            raise frappe.ValidationError(
                "Unable to resolve Scenario ID for Sandbox Testing mode. "
                "Please set a valid Sale Type on invoice items and ensure related "
                "FBR Sale Type mapping is configured."
            )
        payload["scenarioId"] = scenario_id

    # --- Items mapping ---
    for row in doc.items or []:
        tax_rate = _first_item_tax_rate(row.item_tax_template)
        value_excl_st = flt(row.rate)
        sales_tax_applicable = round((tax_rate * value_excl_st) / 100.0, 2)
        raw_sale_type = str(row.custom_sale_type or "").strip()
        scenario_id = get_scenario_id(raw_sale_type)
        payload_sale_type = map_sale_type_for_payload(raw_sale_type, scenario_id)

        item_entry = {
            "hsCode": (row.custom_hs_code or ""),
            "productDescription": (row.description or row.item_name or ""),
            "rate": format_rate(tax_rate),
            "uoM": str(row.stock_uom or row.uom or "").strip(),
            "quantity": flt(row.qty),
            "totalValues": 0.00,
            "valueSalesExcludingST": value_excl_st,
            "fixedNotifiedValueOrRetailPrice": 0.00,
            "salesTaxApplicable": sales_tax_applicable,
            "salesTaxWithheldAtSource": 0.00,
            "extraTax": 0.00,
            "furtherTax": 0.00,
            "sroScheduleNo": "",
            "fedPayable": 0.00,
            "discount": abs(flt(row.discount_amount or 0.0)),
            "saleType": payload_sale_type,
            "sroItemSerialNo": "",
        }
        payload["items"].append(item_entry)

    return payload


def format_rate(tax_rate):
    value = flt(tax_rate)
    # If the number is a whole number (e.g. 18.0), format without decimals
    if value.is_integer():
        return f"{int(value)}%"
    else:
        return f"{value}%"


def get_scenario_id(sale_type: str) -> str:
    """
    Fetch scenario_id from FBR Sale Type doctype
    based on the given sale_type.
    Returns empty string if not found.
    """
    normalized_sale_type = (sale_type or "").strip()
    if not normalized_sale_type:
        return ""

    try:
        scenario_id = frappe.db.get_value(
            "FBR Sale Type",  # Doctype name
            {
                "name": normalized_sale_type
            },  # or use {"sale_type": sale_type} if field differs
            "scenario_id",
        )
        return scenario_id or ""
    except Exception:
        return ""


def map_sale_type_for_payload(sale_type: str, scenario_id: str) -> str:
    """
    Normalize outgoing payload saleType while preserving scenario selection.

    For scenarios that intentionally have UI-distinct labels, normalize outgoing
    payload saleType to the canonical FBR value based on the scenario_id from the doctype.
    """
    normalized_sale_type = (sale_type or "").strip()
    if not normalized_sale_type:
        return ""

    if scenario_id in STANDARD_RATE_SCENARIO_IDS:
        return STANDARD_RATE_SALE_TYPE
    if scenario_id in REDUCED_RATE_SCENARIO_IDS:
        return REDUCED_RATE_SALE_TYPE
    if scenario_id in THIRD_SCHEDULE_SCENARIO_IDS:
        return THIRD_SCHEDULE_SALE_TYPE

    return normalized_sale_type


def _get_party_address_text(link_doctype: str, link_name: str) -> str:
    """
    Find Address via Dynamic Link child table:
      1) Get Address names from Dynamic Link where link_doctype/link_name match
      2) From those Address docs, prefer primary (is_primary_address), else latest
      3) Return "address_line1, address_line2, city, state, pincode"
    """
    if not (link_doctype and link_name):
        return ""

    # 1) Get parent Address names from Dynamic Link
    address_names = frappe.get_all(
        "Dynamic Link",
        filters={
            "parenttype": "Address",
            "link_doctype": link_doctype,
            "link_name": link_name,
        },
        pluck="parent",  # returns list of Address names
        limit=50,
    )

    if not address_names:
        return ""

    # 2) Pull those Address docs; prefer primary, else newest, and skip disabled
    addresses = frappe.get_all(
        "Address",
        filters={
            "name": ["in", address_names],
            "disabled": 0,
        },
        fields=[
            "name",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "pincode",
            "is_primary_address",
            "creation",
        ],
        order_by="is_primary_address desc, creation desc",
        limit=1,
    )

    if not addresses:
        return ""

    a = addresses[0]

    # 3) Compose a readable single-line address
    parts = [
        a.get("address_line1"),
        a.get("address_line2"),
        a.get("city"),
        a.get("state"),
        a.get("pincode"),
    ]
    return ", ".join([p for p in parts if p])


def _first_item_tax_rate(item_tax_template_name: str) -> float:
    """
    From Item Tax Template, get first taxes row's tax_rate.
    Returns 0.0 if not found.
    """
    if not item_tax_template_name:
        return 0.0
    try:
        itt = frappe.get_doc("Item Tax Template", item_tax_template_name)
        if getattr(itt, "taxes", None) and len(itt.taxes) > 0:
            return flt(itt.taxes[0].tax_rate)
    except Exception:
        pass
    return 0.0
