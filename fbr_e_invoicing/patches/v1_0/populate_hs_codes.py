import frappe
from fbr_e_invoicing.utils import sync_hs_codes
def execute():
    # 1. FORCE the schema update immediately
    # This adds the 'description' column to the database
    frappe.reload_doc("fbr_e_invoicing", "doctype", "hs_code")
    # 2. Now it is safe to insert data
    sync_hs_codes()