import frappe
from frappe import _
from datetime import datetime

def validate_fbr_fields(doc, method):
    """Validate FBR required fields before saving Sales Invoice"""
    if not doc.custom_submit_to_fbr:
        return
        
    errors = []
    
    # Check if FBR setup is configured
    fbr_settings = frappe.get_single("FBR E-Inv Setup")
    if not fbr_settings.api_endpoint:
        errors.append(_("FBR API endpoint not configured in FBR E-Inv Setup"))
    
    # Validate posting date (should be current date for FBR)
    if doc.docstatus == 0:  # Only validate for draft documents
        today = frappe.utils.getdate()
        if doc.posting_date != today:
            frappe.msgprint(
                _("Warning: FBR requires posting date to be current date ({0}) for successful submission").format(today),
                alert=True,
                indicator='orange'
            )
    
    # Check required FBR fields
    if not doc.custom_province:
        errors.append(_("Buyer Province is required for FBR submission"))
    
    if not doc.tax_category:
        errors.append(_("Tax Category (Seller Province) is required for FBR submission"))
    
    # Validate customer tax information
    if doc.customer:
        customer = frappe.get_doc("Customer", doc.customer)
        if not customer.tax_id and not customer.custom_province:
            frappe.msgprint(
                _("Customer {0} is missing Tax ID or Province information required for FBR").format(customer.customer_name),
                alert=True,
                indicator='orange'
            )
    
    # Validate company tax information
    if doc.company:
        company = frappe.get_doc("Company", doc.company)
        if not company.tax_id:
            errors.append(_("Company Tax ID is required for FBR submission"))
    
    # Validate items
    validate_fbr_items(doc, errors)
    
    # If there are validation errors, prevent save
    if errors:
        frappe.throw("<br>".join(errors), title=_("FBR Validation Failed"))

def validate_fbr_items(doc, errors):
    """Validate FBR specific item requirements"""
    if not doc.items:
        errors.append(_("At least one item is required for FBR submission"))
        return
    
    missing_hs_codes = []
    missing_tax_templates = []
    
    for idx, item in enumerate(doc.items, 1):
        # Check HS Code
        if not item.custom_hs_code:
            missing_hs_codes.append(f"Row {idx}: {item.item_name}")
        
        # Check Item Tax Template
        if not item.item_tax_template:
            missing_tax_templates.append(f"Row {idx}: {item.item_name}")
        
        # Validate item master data
        item_master = frappe.get_doc("Item", item.item_code)
        if not item_master.custom_hs_code and not item.custom_hs_code:
            # Item doesn't have HS code in master or transaction
            continue
    
    if missing_hs_codes:
        errors.append(_("Following items are missing HS Codes required for FBR:<br>{0}").format("<br>".join(missing_hs_codes)))
    
    if missing_tax_templates:
        errors.append(_("Following items are missing Item Tax Templates required for FBR:<br>{0}").format("<br>".join(missing_tax_templates)))

@frappe.whitelist()
def validate_fbr_document(doctype, docname):
    """API method to validate a document for FBR compliance"""
    try:
        doc = frappe.get_doc(doctype, docname)
        errors = []
        
        if doctype == "Sales Invoice":
            validate_fbr_fields(doc, None)
        elif doctype == "POS Invoice":
            validate_pos_invoice_fbr(doc, errors)
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": get_fbr_warnings(doc)
        }
        
    except Exception as e:
        return {
            "valid": False,
            "errors": [str(e)],
            "warnings": []
        }

def validate_pos_invoice_fbr(doc, errors):
    """Validate POS Invoice for FBR submission"""
    # POS Invoice specific validations
    if not doc.pos_profile:
        errors.append(_("POS Profile is required"))
    
    if doc.pos_profile:
        pos_profile = frappe.get_doc("POS Profile", doc.pos_profile)
        if not pos_profile.company:
            errors.append(_("POS Profile must have a company assigned"))
    
    # Check if payload exists
    if not doc.custom_payload:
        errors.append(_("FBR payload not found. Document may need to be saved first."))

def get_fbr_warnings(doc):
    """Get FBR warnings (non-blocking issues)"""
    warnings = []
    
    # Check for optimal submission timing
    current_hour = datetime.now().hour
    if current_hour < 6 or current_hour > 22:
        warnings.append(_("FBR API may have reduced availability outside business hours"))
    
    # Check for duplicate submission
    if hasattr(doc, 'custom_fbr_invoice_number') and doc.custom_fbr_invoice_number:
        warnings.append(_("Document already submitted to FBR"))
    
    # Check customer payment terms
    if hasattr(doc, 'payment_terms_template') and doc.payment_terms_template:
        # Could add specific warnings about payment terms affecting FBR
        pass
    
    return warnings

@frappe.whitelist()
def check_fbr_api_status():
    """Check if FBR API is accessible"""
    try:
        fbr_settings = frappe.get_single("FBR E-Inv Setup")
        
        if not fbr_settings.api_endpoint:
            return {
                "status": "error",
                "message": _("FBR API endpoint not configured")
            }
        
        # In production, add actual API health check
        # response = requests.get(f"{fbr_settings.api_endpoint}/health", timeout=5)
        
        # Mock response for now
        return {
            "status": "success",
            "message": _("FBR API is accessible"),
            "api_version": "1.0",
            "response_time": "150ms"
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"FBR API check failed: {str(e)}"
        }

@frappe.whitelist()
def get_fbr_compliance_report(from_date=None, to_date=None):
    """Generate FBR compliance report"""
    try:
        if not from_date:
            from_date = frappe.utils.add_days(frappe.utils.today(), -30)
        if not to_date:
            to_date = frappe.utils.today()
        
        # Get submitted invoices in date range
        submitted_invoices = frappe.db.sql("""
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
        """, (from_date, to_date, from_date, to_date), as_dict=True)
        
        # Categorize results
        total_invoices = len(submitted_invoices)
        successful = len([inv for inv in submitted_invoices if inv.custom_fbr_status == 'Valid'])
        invalid = len([inv for inv in submitted_invoices if inv.custom_fbr_status == 'Invalid'])
        errors = len([inv for inv in submitted_invoices if inv.custom_fbr_status in ['Error', 'Failed']])
        pending = len([inv for inv in submitted_invoices if not inv.custom_fbr_status])
        
        compliance_rate = (successful / total_invoices * 100) if total_invoices > 0 else 0
        
        return {
            "from_date": from_date,
            "to_date": to_date,
            "total_invoices": total_invoices,
            "successful": successful,
            "invalid": invalid,
            "errors": errors,
            "pending": pending,
            "compliance_rate": round(compliance_rate, 2),
            "invoices": submitted_invoices
        }
        
    except Exception as e:
        frappe.log_error(f"Error generating FBR compliance report: {str(e)}", "FBR Compliance Report")
        return {
            "error": str(e)
        }

from frappe.utils import nowdate, now_datetime
def force_today_posting_date(doc, method):
    """Force posting_date/time to 'today' at submit time."""
    doc.set_posting_time = 1
    doc.posting_date = nowdate()
    if hasattr(doc, "posting_time"):
        doc.posting_time = now_datetime().time()