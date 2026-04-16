import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


POS_INVOICE_ITEM_TAX_FIELDS = [
    {
        "fieldname": "custom_tax_rate",
        "label": "Tax Rate",
        "fieldtype": "Float",
        "insert_after": "item_tax_template",
        "read_only": 1,
        "in_list_view": 1,
    },
    {
        "fieldname": "custom_tax_amount",
        "label": "Tax Amount",
        "fieldtype": "Currency",
        "insert_after": "custom_tax_rate",
        "read_only": 1,
        "in_list_view": 1,
    },
]


def execute():
    create_custom_fields(
        {"POS Invoice Item": POS_INVOICE_ITEM_TAX_FIELDS},
        update=True,
    )
    frappe.clear_cache(doctype="POS Invoice Item")
    frappe.clear_cache(doctype="POS Invoice")
