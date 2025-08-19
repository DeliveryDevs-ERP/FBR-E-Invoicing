import frappe
from frappe.utils import flt, formatdate

@frappe.whitelist()
def build_fbr_payload(sales_invoice_name: str):
    """
    Build FBR payload for a Sales Invoice per provided mapping.
    Returns a dict (JSON-serializable).
    """
    doc = frappe.get_doc('Sales Invoice', sales_invoice_name)

    # --- Party helpers ---
    seller_tax_id = frappe.db.get_value('Customer', doc.customer, 'tax_id') if doc.customer else None
    seller_name = frappe.db.get_value('Customer', doc.customer, 'customer_name') if doc.customer else None
    seller_province = doc.tax_category
    seller_address = _get_party_address_text('Customer', doc.customer)
    invoice_type = "Debit Note" if getattr(doc, "is_debit_note", 0) else "Sale Invoice"
    buyer_tax_id = frappe.db.get_value('Company', doc.company, 'tax_id') if doc.company else None
    buyer_name = doc.company
    buyer_province = doc.custom_province
    buyer_address = _get_party_address_text('Company', doc.company)
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
        value_excl_st = flt(row.rate)  # as requested: use unit rate as valueSalesExcludingST
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

    return payload


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
        pluck="parent",   # returns list of Address names
        limit=50
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
            "name", "address_line1", "address_line2",
            "city", "state", "pincode", "is_primary_address", "creation"
        ],
        order_by="is_primary_address desc, creation desc",
        limit=1
    )

    if not addresses:
        return ""

    a = addresses[0]

    # 3) Compose a readable single-line address
    parts = [a.get("address_line1"), a.get("address_line2"), a.get("city"), a.get("state"), a.get("pincode")]
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
