import frappe
import json
from frappe.utils import flt, formatdate


def get(doc, method=None):
    """
    Called on POS Invoice validate.
    Builds the FBR payload and saves JSON string to custom_payload field.
    """
    # --- Party helpers ---
    seller_tax_id = frappe.db.get_value('Customer', doc.customer, 'tax_id') if doc.customer else None
    seller_name = frappe.db.get_value('Customer', doc.customer, 'customer_name') if doc.customer else None
    seller_province = doc.tax_category
    seller_address = _get_party_address_text('Customer', doc.customer)
    # invoice_type = "Return" if getattr(doc, "is_return", 0) else "Sale Invoice"
    invoice_type = "POS Invoice"


    buyer_name = frappe.db.get_value('POS Profile', doc.pos_profile, 'company') if doc.pos_profile else None
    buyer_tax_id = frappe.db.get_value('Company', buyer_name, 'tax_id') if buyer_name else None
    buyer_province = frappe.db.get_value('Company', buyer_name, 'custom_province') if buyer_name else None
    buyer_address = _get_party_address_text('Company', buyer_name)
    buyer_registration_type = "Registered" if buyer_tax_id else "Unregistered"

    # --- Invoice-level fields ---
    payload = {
        "invoiceType": invoice_type,
        "invoiceDate": formatdate(doc.posting_date, "yyyy-mm-dd"),
        "sellerNTNCNIC": (seller_tax_id or ""),
        "sellerBusinessName": (seller_name or ""),
        "sellerProvince": (seller_province or ""),
        "sellerAddress": (seller_address or ""),
        "buyerNTNCNIC": (buyer_tax_id or ""),
        "buyerBusinessName": (buyer_name or ""),
        "buyerProvince": (buyer_province or ""),
        "buyerAddress": (buyer_address or ""),
        "buyerRegistrationType": buyer_registration_type,
        "invoiceRefNo": "",
        "items": []
    }

    # --- Items mapping ---
    for row in (doc.items or []):
        tax_rate = _first_item_tax_rate(row.item_tax_template)
        value_excl_st = flt(row.rate)   # as in POS Invoice
        sales_tax_applicable = round((tax_rate * value_excl_st) / 100.0, 2)

        item_entry = {
            "hsCode": (row.custom_hs_code or ""),
            "productDescription": (row.description or row.item_name or ""),
            "rate": f"{flt(tax_rate)}%",
            "uoM": (row.uom or ""),
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
            "saleType": "Goods at standard rate (default)",
            "sroItemSerialNo": ""
        }
        payload["items"].append(item_entry)

    # --- Save JSON to custom_payload field ---
    frappe.db.set_value("POS Invoice", doc.name, "custom_payload", json.dumps(payload, indent=2))


def _get_party_address_text(link_doctype: str, link_name: str) -> str:
    """Get address text similar to Sales Invoice version."""
    if not (link_doctype and link_name):
        return ""

    address_names = frappe.get_all(
        "Dynamic Link",
        filters={"parenttype": "Address", "link_doctype": link_doctype, "link_name": link_name},
        pluck="parent",
        limit=50,
    )
    if not address_names:
        return ""

    addresses = frappe.get_all(
        "Address",
        filters={"name": ["in", address_names], "disabled": 0},
        fields=[
            "name", "address_line1", "address_line2",
            "city", "state", "pincode", "is_primary_address", "creation"
        ],
        order_by="is_primary_address desc, creation desc",
        limit=1
    )
    if not addresses:
        return ""

    a = addresses[0]
    parts = [a.get("address_line1"), a.get("address_line2"), a.get("city"), a.get("state"), a.get("pincode")]
    return ", ".join([p for p in parts if p])


def _first_item_tax_rate(item_tax_template_name: str) -> float:
    """Get first tax_rate from Item Tax Template."""
    if not item_tax_template_name:
        return 0.0
    try:
        itt = frappe.get_doc("Item Tax Template", item_tax_template_name)
        if getattr(itt, "taxes", None) and len(itt.taxes) > 0:
            return flt(itt.taxes[0].tax_rate)
    except Exception:
        pass
    return 0.0
