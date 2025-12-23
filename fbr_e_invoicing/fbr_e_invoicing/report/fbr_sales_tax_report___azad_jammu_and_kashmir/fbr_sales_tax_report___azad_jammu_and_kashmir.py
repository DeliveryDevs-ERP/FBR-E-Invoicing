# Copyright (c) 2025, osama.ahmed@deliverydevs.com and contributors
# For license information, please see license.txt

# import frappe


import frappe
from fbr_e_invoicing.fbr_e_invoicing.report.report_utils import get_columns, get_data

def execute(filters=None):
    if not filters:
        filters = {}
    
    # Force Sindh tax category
    filters["tax_category"] = "AZAD JAMMU AND KASHMIR"
    
    # Get columns and data with Sindh-specific handling
    columns = get_columns(filters, province="AZAD JAMMU AND KASHMIR")
    data = get_data(filters, province="AZAD JAMMU AND KASHMIR")
    
    return columns, data