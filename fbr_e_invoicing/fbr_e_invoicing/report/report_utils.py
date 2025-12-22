# Copyright (c) 2025, osama.ahmed@deliverydevs.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _

def get_columns(filters, province=None):
    """
    Get columns with optional province-specific columns
    """
    # Base columns (common to all provinces)
    columns = [
        {"label": _("Sr"), "fieldname": "sr", "fieldtype": "Data", "width": 40},
        {"label": _("Item"), "fieldname": "item_name", "fieldtype": "Data", "width": 150},
        {"label": _("Name of Buyer"), "fieldname": "buyer_name", "fieldtype": "Data", "width": 180},
        {"label": _("NTN"), "fieldname": "ntn", "fieldtype": "Data", "width": 120},
        {"label": _("CNIC"), "fieldname": "cnic", "fieldtype": "Data", "width": 130},
        {"label": _("District"), "fieldname": "district", "fieldtype": "Data", "width": 80},
        {"label": _("Buyer Type"), "fieldname": "buyer_type", "fieldtype": "Data", "width": 90},
        {"label": _("Doc Type"), "fieldname": "doc_type", "fieldtype": "Data", "width": 100},
        {"label": _("Doc No"), "fieldname": "doc_number", "fieldtype": "Link", "options": "Sales Invoice", "width": 130},
        {"label": _("Doc Date"), "fieldname": "doc_date", "fieldtype": "Date", "width": 100},
        {"label": _("HS Code"), "fieldname": "hs_code", "fieldtype": "Link", "options": "HS Code", "width": 90},
        {"label": _("Sale Type"), "fieldname": "sale_type", "fieldtype": "Data", "width": 100},
        {"label": _("Rate"), "fieldname": "rate", "fieldtype": "Percent", "width": 60},
        {"label": _("Value Excl. Tax"), "fieldname": "value_excl_tax", "fieldtype": "Currency", "width": 120},
        {"label": _("Sales Tax"), "fieldname": "sales_tax", "fieldtype": "Currency", "width": 100},
        {"label": _("FBR Status"), "fieldname": "fbr_status", "fieldtype": "Data", "width": 80},
        {"label": _("FBR Invoice No"), "fieldname": "fbr_invoice_number", "fieldtype": "Data", "width": 120},
    ]
    
    # Add province-specific columns
    province_columns = get_province_specific_columns(province)
    if province_columns:
        # Insert province columns after district (position 6)
        insert_position = 6
        for i, col in enumerate(province_columns):
            columns.insert(insert_position + i, col)
    
    return columns

def get_province_specific_columns(province):
    """
    Define province-specific columns here
    Add more as needed in the future
    """
    province_columns = {
        "PUNJAB": [
            # Example: Add these fields in future if needed
            # {"label": _("Punjab Tax ID"), "fieldname": "punjab_tax_id", "fieldtype": "Data", "width": 120},
        ],
        "SINDH": [
            # Example: Add these fields in future if needed
            # {"label": _("Sindh Revenue ID"), "fieldname": "sindh_revenue_id", "fieldtype": "Data", "width": 120},
        ],
        "KYBER PAKHTUNKHWA": [],
        "BALOCHISTAN": [],
        "GILGIT BALTISTAN": [],
        "AZAD JAMMU AND KASHMIR": [],
        "CAPITAL TERRITORY": [],
    }
    
    return province_columns.get(province, [])

def get_data(filters, province=None):
    """
    Get data with optional province-specific data processing
    """
    si = frappe.qb.DocType("Sales Invoice")
    sii = frappe.qb.DocType("Sales Invoice Item")

    # Base query
    query = (
        frappe.qb.from_(sii)
        .inner_join(si)
        .on(sii.parent == si.name)
        .select(
            sii.item_name,
            sii.net_amount,
            sii.custom_hs_code,
            sii.custom_tax_rate,
            sii.custom_tax_amount,
            sii.custom_sale_type,
            sii.parent,
            si.posting_date,  
            si.customer_name,
            si.ntn,
            si.nic,
            si.tax_category,
            si.customer,
            si.docstatus,
            si.custom_fbr_status,
            si.custom_fbr_invoice_number
        )
    )
    
    # Add province-specific fields to query (for future use)
    additional_fields = get_province_specific_fields(province)
    if additional_fields:
        for field_name, field_ref in additional_fields.items():
            query = query.select(field_ref)

    # --- FILTERS ---
    status_filter = filters.get("report_status")
    if status_filter == "Draft":
        query = query.where(si.docstatus == 0)
    elif status_filter == "Cancelled":
        query = query.where(si.docstatus == 2)
    else:
        query = query.where(si.docstatus == 1)

    fbr_filter = filters.get("fbr_status")
    if fbr_filter == "Valid":
        query = query.where(si.custom_fbr_status == "Valid")
    elif fbr_filter == "Invalid":
        query = query.where(
            (si.custom_fbr_status != "Valid") | (si.custom_fbr_status.isnull())
        )

    if filters.get("company"):
        query = query.where(si.company == filters.get("company"))

    if filters.get("from_date") and filters.get("to_date"):
        query = query.where(si.posting_date[filters.get("from_date"):filters.get("to_date")])

    if filters.get("tax_category"):
        query = query.where(si.tax_category == filters.get("tax_category"))

    if filters.get("customer"):
        query = query.where(si.customer == filters.get("customer"))

    # Sort
    query = query.orderby(si.posting_date, order=frappe.qb.desc).orderby(si.name, order=frappe.qb.desc)

    items = query.run(as_dict=True)

    # Fetch Customer Types
    customer_ids = list(set([d.customer for d in items if d.customer]))
    customer_type_map = {}
    if customer_ids:
        customers = frappe.get_all("Customer", filters={"name": ["in", customer_ids]}, fields=["name", "customer_type"])
        for c in customers:
            customer_type_map[c.name] = c.customer_type

    data = []
    total_value = 0.0
    total_tax = 0.0
    sr_counter = 1

    for row in items:
        c_type = customer_type_map.get(row.customer, "")
        
        display_ntn = row.ntn if c_type != "Individual" else ""
        display_cnic = row.nic if c_type == "Individual" else ""

        row_val = row.net_amount or 0.0
        row_tax = row.custom_tax_amount or 0.0

        # NEW CODE - Treat NULL/empty as Invalid
        fbr_status = row.custom_fbr_status or "Invalid"  # Default to Invalid if empty
        if fbr_status == "Valid":
            fbr_invoice_number = row.custom_fbr_invoice_number or ""
        else:
            # Any status other than "Valid" (including "Invalid", NULL, empty) shows "Pending"
            fbr_invoice_number = "Pending"
        
        row_data = {
            "sr": sr_counter,
            "item_name": row.item_name,
            "ntn": display_ntn,
            "cnic": display_cnic,
            "buyer_name": row.customer_name,
            "district": "",
            "buyer_type": c_type,
            "doc_type": "Sales Invoice",
            "doc_number": row.parent,
            "doc_date": row.posting_date,
            "hs_code": row.custom_hs_code,
            "sale_type": row.custom_sale_type,
            "rate": row.custom_tax_rate,
            "value_excl_tax": row_val,
            "sales_tax": row_tax,
            "fbr_status": fbr_status,
            "fbr_invoice_number": fbr_invoice_number,
        }
        
        # Add province-specific data (for future use)
        if province and additional_fields:
            for field_name in additional_fields.keys():
                row_data[field_name] = row.get(field_name, "")

        data.append(row_data)
        
        total_value += row_val
        total_tax += row_tax
        sr_counter += 1

    # Totals Row
    if data:
        total_row = {
            "sr": "",
            "item_name": "<b>TOTAL</b>",
            "ntn": "", 
            "cnic": "", 
            "buyer_name": "",
            "district": "",
            "buyer_type": "",
            "doc_type": "",
            "doc_number": "",
            "doc_date": "",
            "hs_code": "",
            "sale_type": "",
            "rate": None,
            "value_excl_tax": total_value,
            "sales_tax": total_tax,
            "fbr_status": "",
            "fbr_invoice_number": "",
        }
        
        # Add empty values for province-specific fields in total row
        if province and additional_fields:
            for field_name in additional_fields.keys():
                total_row[field_name] = ""
        
        data.append(total_row)

    return data

def get_province_specific_fields(province):
    """
    Define which fields to fetch from database for each province
    Add more as needed in the future
    """
    si = frappe.qb.DocType("Sales Invoice")
    
    province_fields = {
        "PUNJAB": {
            # Example: Uncomment and add custom fields when needed
            # "punjab_tax_id": si.custom_punjab_tax_id,
        },
        "SINDH": {
            # Example: Uncomment and add custom fields when needed
            # "sindh_revenue_id": si.custom_sindh_revenue_id,
        },
        "KYBER PAKHTUNKHWA": {},
        "BALOCHISTAN": {},
        "GILGIT BALTISTAN": {},
        "AZAD JAMMU AND KASHMIR": {},
        "CAPITAL TERRITORY": {},
    }
    
    return province_fields.get(province, {})