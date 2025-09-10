import frappe
import json
import requests
from datetime import datetime
from frappe.utils import now, flt

@frappe.whitelist()
def submit_single_invoice(doctype, docname, is_retry=False):
    """Submit a single invoice to FBR"""
    try:
        # Get the document
        doc = frappe.get_doc(doctype, docname)
        
        # Build the payload
        if doctype == "Sales Invoice":
            from fbr_e_invoicing.api.build_fbr_payload import build_fbr_payload
            payload = build_fbr_payload(docname)
        elif doctype == "POS Invoice":  # POS Invoice
            # Get payload from the document
            if not doc.custom_payload:
                frappe.throw("No FBR payload found. Please regenerate the payload.")
            payload = json.loads(doc.custom_payload)
        else:
            frappe.throw("Unknown Doctype error in submit_single_invoice function")
            return 500
        # Submit to FBR
        response = submit_to_fbr_api(payload, doc.name, doctype, is_retry)
        
        # Log the submission
        log_fbr_submission(doctype, docname, payload, response, "Success" if response.get("validationResponse", {}).get("status") == "Valid" else "Invalid")
        
        return response
        
    except Exception as e:
        # Log the error
        log_fbr_submission(doctype, docname, {}, {"error": str(e)}, "Error")
        frappe.throw(f"FBR submission failed: {str(e)}")

@frappe.whitelist()
def bulk_submit_invoices(doctype, docnames):
    """Submit multiple invoices to FBR queue"""
    if isinstance(docnames, str):
        docnames = json.loads(docnames)
    
    queued_count = 0
    
    for docname in docnames:
        try:
            doc = frappe.get_doc(doctype, docname)
            
            # Check if already submitted
            if doc.custom_fbr_invoice_number:
                continue
                
            # Add to queue
            queue_doc = frappe.new_doc("FBR Queue")
            queue_doc.update({
                "document_type": doctype,
                "document_name": docname,
                "status": "Pending",
                "priority": 5,  # Normal priority
                "created_at": now()
            })
            queue_doc.insert(ignore_permissions=True)
            
            # Update document status
            # doc.db_set("custom_fbr_queue_status", "Queued")
            
            queued_count += 1
            
        except Exception as e:
            frappe.log_error(f"Error queuing {doctype} {docname}: {str(e)}", "FBR Bulk Submit")
            continue
    
    # Commit the changes
    frappe.db.commit()
    
    return {"queued_count": queued_count}

def submit_to_fbr_api(payload, document_name, document_type, is_retry=False):
    """Submit payload to FBR API"""
    try:
        # Get FBR settings
        fbr_settings = frappe.get_single("FBR E-Inv Setup")
        
        if not fbr_settings.api_endpoint or not fbr_settings.pral_authorization_token:
            frappe.throw("FBR API settings not configured. Please check FBR E-Inv Setup.")
        
        # Prepare headers
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {fbr_settings.pral_authorization_token}"
        }
        
        # For now, return a mock response since we don't have the actual FBR API endpoint
        # In production, replace this with actual API call:
        # response = requests.post(fbr_settings.api_endpoint, json=payload, headers=headers, timeout=30)
        # response.raise_for_status()
        # return response.json()
        
        # Mock response for development
        mock_response = {
            "invoiceNumber": f"FBR{document_name}{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "dated": now(),
            "validationResponse": {
                "statusCode": "00",
                "status": "Valid" if not is_retry else "Invalid",
                "error": "" if not is_retry else "Sample validation error",
                "invoiceStatuses": [
                    {
                        "itemSNo": "1",
                        "statusCode": "00",
                        "status": "Valid" if not is_retry else "Invalid",
                        "invoiceNo": f"FBR{document_name}-1",
                        "errorCode": "" if not is_retry else "E001",
                        "error": "" if not is_retry else "Sample item error"
                    }
                ]
            }
        }
        
        return mock_response
        
    except requests.exceptions.RequestException as e:
        frappe.throw(f"FBR API request failed: {str(e)}")
    except Exception as e:
        frappe.throw(f"FBR submission error: {str(e)}")

def log_fbr_submission(document_type, document_name, payload, response, status):
    """Log FBR submission to FBR Logs"""
    try:
        log_doc = frappe.new_doc("FBR Logs")
        log_doc.update({
            "document_type": document_type,
            "document_name": document_name,
            "request_payload": json.dumps(payload, indent=2) if payload else "",
            "response_data": json.dumps(response, indent=2) if response else "",
            "status": status,
            "submitted_at": now(),
            "fbr_invoice_number": response.get("invoiceNumber", "") if response else ""
        })
        log_doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(f"Error logging FBR submission: {str(e)}", "FBR Logging")

@frappe.whitelist()
def get_fbr_submission_stats():
    """Get FBR submission statistics"""
    try:
        stats = frappe.db.sql("""
            SELECT 
                status,
                COUNT(*) as count
            FROM `tabFBR Logs`
            WHERE DATE(submitted_at) = CURDATE()
            GROUP BY status
        """, as_dict=True)
        
        # Get queue statistics
        queue_stats = frappe.db.sql("""
            SELECT 
                status,
                COUNT(*) as count
            FROM `tabFBR Queue`
            GROUP BY status
        """, as_dict=True)
        
        return {
            "today_submissions": stats,
            "queue_status": queue_stats
        }
        
    except Exception as e:
        frappe.log_error(f"Error getting FBR stats: {str(e)}", "FBR Stats")
        return {"today_submissions": [], "queue_status": []}