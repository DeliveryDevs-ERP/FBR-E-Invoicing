import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


POS_INVOICE_FIELDS = [
    {
        "fieldname": "custom_province",
        "label": "Province",
        "fieldtype": "Link",
        "options": "Province",
        "insert_after": "posting_date",
    },
    {
        "fieldname": "custom_submit_to_fbr",
        "label": "Submit to FBR",
        "fieldtype": "Check",
        "default": "1",
        "insert_after": "custom_province",
    },
    {
        "fieldname": "custom_fbr_invoice_number",
        "label": "FBR Invoice Number",
        "fieldtype": "Data",
        "insert_after": "custom_submit_to_fbr",
    },
    {
        "fieldname": "custom_fbr_datetime",
        "label": "FBR Datetime",
        "fieldtype": "Datetime",
        "insert_after": "custom_fbr_invoice_number",
    },
    {
        "fieldname": "custom_fbr_status",
        "label": "FBR Status",
        "fieldtype": "Data",
        "insert_after": "custom_fbr_datetime",
    },
    {
        "fieldname": "custom_fbr_response",
        "label": "FBR Response",
        "fieldtype": "Text",
        "insert_after": "custom_fbr_status",
    },
]


POS_INVOICE_ITEM_FIELDS = [
    {
        "fieldname": "custom_hs_code",
        "label": "HS Code",
        "fieldtype": "Link",
        "options": "HS Code",
        "insert_after": "customer_item_code",
        "fetch_from": "item_code.custom_hs_code",
    },
    {
        "fieldname": "custom_sale_type",
        "label": "Sale Type",
        "fieldtype": "Link",
        "options": "FBR Sale Type",
        "insert_after": "custom_hs_code",
        "fetch_from": "item_code.custom_sale_type",
    },
]


def execute():
    _ensure_pos_custom_fields()
    frappe.clear_cache(doctype="POS Invoice")
    frappe.clear_cache(doctype="POS Invoice Item")


def _ensure_pos_custom_fields():
    create_custom_fields(
        {
            "POS Invoice": POS_INVOICE_FIELDS,
            "POS Invoice Item": POS_INVOICE_ITEM_FIELDS,
        },
        update=True,
    )
