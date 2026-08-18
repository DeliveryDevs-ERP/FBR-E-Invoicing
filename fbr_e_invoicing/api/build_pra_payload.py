import frappe
from frappe.utils import flt, get_datetime

from fbr_e_invoicing.api.build_fbr_payload import (
    _get_customer_tax_data,
    _resolve_customer_tax_id,
)
from fbr_e_invoicing.utils import PRA_PAYMENT_MODE_MAP, _get_pra_pos_id


INVOICE_TYPE_NEW = 1
INVOICE_TYPE_DEBIT = 2
INVOICE_TYPE_CREDIT = 3

DEFAULT_PAYMENT_MODE = "Cheque"


def _get_customer_phone(customer_name: str | None) -> str:
    """Resolve the buyer's phone number from their primary/newest Contact."""
    if not customer_name:
        return ""

    contact_names = frappe.get_all(
        "Dynamic Link",
        filters={
            "parenttype": "Contact",
            "link_doctype": "Customer",
            "link_name": customer_name,
        },
        pluck="parent",
        limit=50,
    )
    if not contact_names:
        return ""

    contacts = frappe.get_all(
        "Contact",
        filters={"name": ["in", contact_names]},
        fields=["mobile_no", "phone", "is_primary_contact", "creation"],
        order_by="is_primary_contact desc, creation desc",
        limit=1,
    )
    if not contacts:
        return ""

    return (contacts[0].get("mobile_no") or contacts[0].get("phone") or "").strip()


@frappe.whitelist()
def build_pra_payload(sales_invoice_name: str):
    """
    Build the PRA IMS invoice payload for a Sales Invoice per the
    POS_COMPONENT_and_eIMS_User_Manual.pdf schema. Returns a dict.
    """
    doc = frappe.get_doc("Sales Invoice", sales_invoice_name)

    is_return = bool(getattr(doc, "is_return", 0))
    invoice_type = INVOICE_TYPE_CREDIT if is_return else INVOICE_TYPE_NEW

    customer_tax_data = _get_customer_tax_data(doc.customer)
    buyer_tax_id = _resolve_customer_tax_id(customer_tax_data)
    buyer_nic = (customer_tax_data.get("nic") or "").strip()

    pos_id = _get_pra_pos_id(doc.company)
    if not pos_id:
        frappe.throw(
            frappe._(
                "PRA POS ID is not set on Company {0} (PRA tab)."
            ).format(doc.company)
        )

    payment_mode_label = (doc.get("custom_pra_payment_mode") or DEFAULT_PAYMENT_MODE).strip()
    payment_mode = PRA_PAYMENT_MODE_MAP.get(
        payment_mode_label, PRA_PAYMENT_MODE_MAP[DEFAULT_PAYMENT_MODE]
    )

    invoice_datetime = get_datetime(f"{doc.posting_date} {doc.posting_time or '00:00:00'}")

    payload = {
        "InvoiceNumber": "",
        "POSID": pos_id,
        "USIN": doc.name,
        "RefUSIN": doc.return_against if is_return else None,
        "DateTime": invoice_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        "BuyerName": (customer_tax_data.get("customer_name") or doc.customer_name or ""),
        "BuyerPNTN": (buyer_tax_id or ""),
        "BuyerCNIC": (buyer_nic or ""),
        "BuyerPhoneNumber": _get_customer_phone(doc.customer),
        "TotalQuantity": flt(sum(flt(row.qty) for row in doc.items or [])),
        "TotalSaleValue": flt(doc.net_total),
        "TotalTaxCharged": flt(doc.total_taxes_and_charges),
        "Discount": abs(flt(doc.discount_amount or 0.0)),
        "FurtherTax": 0.0,
        "TotalBillAmount": flt(doc.grand_total),
        "PaymentMode": payment_mode,
        "InvoiceType": invoice_type,
        "Items": [],
    }

    missing_pct_codes = []
    for idx, row in enumerate(doc.items or [], 1):
        pct_code = row.get("custom_pra_pct_code")
        if not pct_code:
            missing_pct_codes.append(f"Row {idx}: {row.item_name}")
            continue

        sale_value = flt(row.amount)
        tax_charged = flt(row.get("custom_tax_amount") or 0.0)

        payload["Items"].append(
            {
                "ItemCode": row.item_code,
                "ItemName": row.item_name,
                "PCTCode": pct_code,
                "Quantity": flt(row.qty),
                "TaxRate": flt(row.get("custom_tax_rate") or 0.0),
                "SaleValue": sale_value,
                "TotalAmount": round(sale_value + tax_charged, 2),
                "TaxCharged": tax_charged,
                "Discount": abs(flt(row.discount_amount or 0.0)),
                "FurtherTax": 0.0,
                "InvoiceType": invoice_type,
                "RefUSIN": doc.return_against if is_return else None,
            }
        )

    if missing_pct_codes:
        frappe.throw(
            frappe._("Following items are missing PRA PCT Code required for PRA:<br>{0}").format(
                "<br>".join(missing_pct_codes)
            )
        )

    return payload
