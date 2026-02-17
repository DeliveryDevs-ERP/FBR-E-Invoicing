import frappe
import json
from frappe.utils import now
import requests
from requests.exceptions import HTTPError

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
            from fbr_e_invoicing.api.build_fbr_payload import build_pos_fbr_payload
            payload = build_pos_fbr_payload(docname)
        else:
            frappe.throw("Unknown Doctype error in submit_single_invoice function")
            return 500
        # Submit to FBR
        response = submit_to_fbr_api(payload, doc.name, doctype, is_retry)
        
        # Log the submission
        log_fbr_submission(doctype, docname, payload, response, "Success" if response.get("validationResponse", {}).get("status") == "Valid" else "Invalid")
        
        return response
        
    except Exception as e:
        error_message = str(e)

        # Log the error
        log_fbr_submission(
            doctype, docname, {}, {"error": error_message}, "Error", defer_insert=True
        )

        # Auto-queue non-retry failures for asynchronous retry.
        if not is_retry and doctype in ("Sales Invoice", "POS Invoice"):
            try:
                from fbr_e_invoicing.api.fbr_queue import add_to_queue

                queue_result = add_to_queue(
                    doctype=doctype,
                    docname=docname,
                    status="Pending",
                    error_message=error_message,
                    priority=5,
                )
                if not queue_result.get("success"):
                    frappe.log_error(
                        f"Failed to auto-queue {doctype} {docname}: {queue_result.get('error')}",
                        "FBR Auto Queue Failure",
                    )
            except Exception as queue_error:
                frappe.log_error(
                    f"Error auto-queueing failed submission for {doctype} {docname}: {str(queue_error)}",
                    "FBR Auto Queue Failure",
                )

        frappe.throw(f"FBR submission failed: {error_message}")

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
    
    return {"queued_count": queued_count}


@frappe.whitelist()
def bulk_submit_sales_invoices(docnames):
    """Queue selected Sales Invoices for FBR submission.

    Keeps FBR logging semantics unchanged:
    - No FBR Logs entry at queueing time
    - FBR Logs are written later during actual submit attempts
    """
    if isinstance(docnames, str):
        docnames = json.loads(docnames)

    if not isinstance(docnames, list):
        frappe.throw("docnames must be a list or JSON array")

    unique_docnames = []
    seen = set()
    for name in docnames:
        if not name:
            continue
        docname = str(name).strip()
        if not docname or docname in seen:
            continue
        seen.add(docname)
        unique_docnames.append(docname)

    if not unique_docnames:
        return {
            "queued_count": 0,
            "queued_invoices": [],
            "draft_invoices": [],
            "failed_invoices": [],
            "queue_route": "/app/fbr-queue",
        }

    invoices = frappe.get_all(
        "Sales Invoice",
        filters={"name": ["in", unique_docnames]},
        fields=["name", "docstatus"],
    )

    invoice_map = {row.name: row for row in invoices}
    draft_invoices = []
    queued_invoices = []
    failed_invoices = []

    from fbr_e_invoicing.api.fbr_queue import add_to_queue

    for docname in unique_docnames:
        doc = invoice_map.get(docname)
        if not doc:
            failed_invoices.append({"name": docname, "error": "Sales Invoice not found"})
            frappe.log_error(
                f"Sales Invoice {docname} not found while bulk queueing",
                "FBR Bulk Submit Sales Invoice",
            )
            continue

        if doc.docstatus == 0:
            draft_invoices.append(docname)
            continue

        queue_result = add_to_queue(
            doctype="Sales Invoice",
            docname=docname,
            status="Pending",
            priority=5,
        )
        if queue_result.get("success"):
            queued_invoices.append(docname)
        else:
            error_message = queue_result.get("error") or "Failed to add to queue"
            failed_invoices.append({"name": docname, "error": error_message})
            frappe.log_error(
                f"Error queueing Sales Invoice {docname}: {error_message}",
                "FBR Bulk Submit Sales Invoice",
            )

    return {
        "queued_count": len(queued_invoices),
        "queued_invoices": queued_invoices,
        "draft_invoices": draft_invoices,
        "failed_invoices": failed_invoices,
        "queue_route": "/app/fbr-queue",
    }

def submit_to_fbr_api(payload, document_name, document_type, is_retry=False):
    """Submit payload to FBR API via HTTP POST and return parsed JSON dict.

    Raises frappe.ValidationError (frappe.throw) with a readable message on failures.
    """


    fbr_settings = frappe.get_single("FBR E-Inv Setup")
    api_endpoint = (fbr_settings.api_endpoint or "").strip()
    token = (fbr_settings.pral_authorization_token or "").strip()

    if not api_endpoint or not token:
        frappe.throw("FBR API settings not configured. Please set API Endpoint and Authorization Token in 'FBR E-Inv Setup'.")

    verify_ssl = getattr(fbr_settings, "verify_ssl", True)
    connect_timeout = float(getattr(fbr_settings, "connect_timeout", 10.0))
    read_timeout = float(getattr(fbr_settings, "read_timeout", 30.0))
    timeout = (connect_timeout, read_timeout)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Document-Name": str(document_name),
        "X-Document-Type": str(document_type),
        "X-Client": "ERPNext FBR E-Invoicing",
        "X-Retry": "1" if is_retry else "0",
    }

    try:
        resp = requests.post(
            api_endpoint,
            json=payload,
            headers=headers,
            timeout=timeout,
            verify=bool(verify_ssl),
        )

        text = resp.text or ""
        try:
            data = resp.json() if text else {}
        except ValueError:
            data = {"raw": text}  

        try:
            resp.raise_for_status()
        except HTTPError as http_err:
            err_msg = None
            if isinstance(data, dict):
                err_msg = (
                    data.get("message")
                    or data.get("error")
                    or data.get("validationResponse", {}).get("error")
                    or data.get("validationResponse", {}).get("status")
                )
            status_line = f"HTTP {resp.status_code}"
            details = f" | Details: {err_msg}" if err_msg else (f" | Body: {text[:500]}" if text else "")
            frappe.throw(f"FBR API error {status_line}{details}")

        if not isinstance(data, dict):
            data = {"result": "success", "raw": text}

        return data
    
    except Exception as e:
        frappe.throw(f"FBR submission error: {str(e)}")

def log_fbr_submission(document_type, document_name, payload, response, status, defer_insert=False):
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
        if defer_insert:
            log_doc.deferred_insert()
        else:
            log_doc.insert(ignore_permissions=True)
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
