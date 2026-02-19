import frappe
import json
from time import perf_counter
from frappe.utils import now
import requests
from requests.exceptions import RequestException


RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429}


@frappe.whitelist()
def submit_single_invoice(doctype, docname, is_retry=False):
    """Submit a single invoice to FBR and return structured status payload."""
    start_time = perf_counter()
    payload = {}
    queue_id = None
    retry_attempt = 1 if is_retry else 0
    user_agent, ip_address = _get_request_context()

    try:
        doc = frappe.get_doc(doctype, docname)
    except Exception as e:
        error_message = str(e)
        processing_time = round((perf_counter() - start_time) * 1000, 2)
        fallback_response = {
            "validationResponse": {"status": "Error", "error": error_message}
        }
        log_fbr_submission(
            doctype,
            docname,
            payload,
            fallback_response,
            "Error",
            retry_attempt=retry_attempt,
            processing_time=processing_time,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        result = {
            "success": False,
            "status": "error",
            "message": error_message,
            "response": fallback_response,
            "queue_id": None,
            "retryable": False,
            "failure_type": "document_error",
        }
        result.update(fallback_response)
        return result
    payload_result = _build_payload(doctype, docname)
    if not payload_result.get("success"):
        error_message = payload_result.get("error") or "Unable to build FBR payload"
        retryable = False
        failure_type = payload_result.get("failure_type") or "payload_error"
        queue_id = _auto_queue_failed_submission(
            doctype, docname, error_message, is_retry, retryable
        )
        processing_time = round((perf_counter() - start_time) * 1000, 2)
        fallback_response = {
            "validationResponse": {"status": "Error", "error": error_message}
        }
        log_fbr_submission(
            doctype,
            docname,
            payload,
            fallback_response,
            "Error",
            retry_attempt=retry_attempt,
            processing_time=processing_time,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        result = {
            "success": False,
            "status": "error",
            "message": error_message,
            "response": fallback_response,
            "queue_id": queue_id,
            "retryable": retryable,
            "failure_type": failure_type,
        }
        result.update(fallback_response)
        return result

    payload = payload_result["payload"]
    api_result = submit_to_fbr_api(payload, doc.name, doctype, is_retry)
    response = api_result.get("data") or {}
    status_code = api_result.get("status_code")
    processing_time = round((perf_counter() - start_time) * 1000, 2)
    api_version = api_result.get("api_version")

    if api_result.get("success"):
        fbr_status = response.get("validationResponse", {}).get("status", "")
        log_status = "Success" if fbr_status == "Valid" else "Invalid"
        log_fbr_submission(
            doctype,
            docname,
            payload,
            response,
            log_status,
            response_status_code=status_code,
            retry_attempt=retry_attempt,
            processing_time=processing_time,
            user_agent=user_agent,
            ip_address=ip_address,
            api_version=api_version,
        )
        result = {
            "success": True,
            "status": "valid" if fbr_status == "Valid" else "invalid",
            "message": (
                "Submitted to FBR"
                if fbr_status == "Valid"
                else "Submitted to FBR with Invalid status"
            ),
            "response": response,
            "queue_id": None,
            "retryable": False,
            "failure_type": "" if fbr_status == "Valid" else "business_invalid",
        }
        if isinstance(response, dict):
            result.update(response)
        return result

    error_message = api_result.get("error") or "FBR submission failed"
    retryable = bool(api_result.get("retryable"))
    failure_type = api_result.get("failure_type") or "http_error"
    queue_id = _auto_queue_failed_submission(
        doctype, docname, error_message, is_retry, retryable
    )
    fallback_response = response if isinstance(response, dict) else {}
    if "validationResponse" not in fallback_response:
        fallback_response["validationResponse"] = {
            "status": "Error",
            "error": error_message,
        }

    log_fbr_submission(
        doctype,
        docname,
        payload,
        fallback_response,
        "Error",
        response_status_code=status_code,
        retry_attempt=retry_attempt,
        processing_time=processing_time,
        user_agent=user_agent,
        ip_address=ip_address,
        api_version=api_version,
    )

    result = {
        "success": False,
        "status": "error",
        "message": error_message,
        "response": fallback_response,
        "queue_id": queue_id,
        "retryable": retryable,
        "failure_type": failure_type,
    }
    result.update(fallback_response)
    return result


def submit_pos_invoice_on_submit(doc, method=None):
    """Server-side POS fallback to keep submission reliable without client scripts."""
    if not getattr(doc, "custom_submit_to_fbr", 0):
        return

    if getattr(doc, "custom_fbr_invoice_number", ""):
        return

    try:
        submission_result = submit_single_invoice("POS Invoice", doc.name, is_retry=False)
        response = submission_result.get("response") or {}
        if isinstance(response, dict) and response:
            _persist_fbr_response_fields("POS Invoice", doc.name, response)

        if not submission_result.get("success"):
            frappe.log_error(
                f"POS Invoice {doc.name} FBR submit fallback failed: "
                f"{submission_result.get('message') or 'Unknown error'}",
                "FBR POS Auto Submit Fallback",
            )
    except Exception as e:
        frappe.log_error(
            f"Error in POS Invoice fallback submit for {doc.name}: {str(e)}",
            "FBR POS Auto Submit Fallback",
        )


@frappe.whitelist()
def bulk_submit_invoices(doctype, docnames):
    """Submit multiple invoices to FBR queue"""
    if isinstance(docnames, str):
        docnames = json.loads(docnames)

    if not isinstance(docnames, list):
        frappe.throw("docnames must be a list or JSON array")

    queued_count = 0

    unique_docnames = []
    seen = set()
    for name in docnames:
        docname = str(name or "").strip()
        if not docname or docname in seen:
            continue
        seen.add(docname)
        unique_docnames.append(docname)

    from fbr_e_invoicing.api.fbr_queue import add_to_queue

    for docname in unique_docnames:
        try:
            doc = frappe.get_doc(doctype, docname)

            # Check if already submitted
            if doc.custom_fbr_invoice_number:
                continue

            queue_result = add_to_queue(
                doctype=doctype,
                docname=docname,
                status="Pending",
                priority=5,
            )
            if queue_result.get("success"):
                queued_count += 1
            else:
                frappe.log_error(
                    f"Error queueing {doctype} {docname}: {queue_result.get('error')}",
                    "FBR Bulk Submit",
                )
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
    """Submit payload to FBR API via HTTP POST and return structured result."""
    fbr_settings = frappe.get_single("FBR E-Inv Setup")
    api_endpoint = (fbr_settings.api_endpoint or "").strip()
    token = (fbr_settings.pral_authorization_token or "").strip()

    if not api_endpoint or not token:
        return {
            "success": False,
            "error": (
                "FBR API settings not configured. Please set API Endpoint and Authorization "
                "Token in 'FBR E-Inv Setup'."
            ),
            "status_code": None,
            "data": {},
            "api_version": "",
            "retryable": False,
            "failure_type": "config_error",
        }

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
    except RequestException as e:
        return {
            "success": False,
            "error": f"FBR submission error: {str(e)}",
            "status_code": None,
            "data": {},
            "api_version": "",
            "retryable": True,
            "failure_type": "network_error",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"FBR submission error: {str(e)}",
            "status_code": None,
            "data": {},
            "api_version": "",
            "retryable": False,
            "failure_type": "unknown_error",
        }

    text = resp.text or ""
    try:
        data = resp.json() if text else {}
    except ValueError:
        data = {"raw": text}

    api_version = ""
    if isinstance(data, dict):
        api_version = str(data.get("apiVersion") or data.get("api_version") or "")

    if resp.status_code >= 400:
        err_msg = None
        if isinstance(data, dict):
            err_msg = (
                data.get("message")
                or data.get("error")
                or data.get("validationResponse", {}).get("error")
                or data.get("validationResponse", {}).get("status")
            )
        status_line = f"HTTP {resp.status_code}"
        details = (
            f" | Details: {err_msg}"
            if err_msg
            else (f" | Body: {text[:500]}" if text else "")
        )
        retryable = _is_retryable_http_status(resp.status_code)
        return {
            "success": False,
            "error": f"FBR API error {status_line}{details}",
            "status_code": resp.status_code,
            "data": data if isinstance(data, dict) else {"raw": text},
            "api_version": api_version,
            "retryable": retryable,
            "failure_type": "http_error",
        }

    if not isinstance(data, dict):
        data = {"result": "success", "raw": text}

    return {
        "success": True,
        "error": "",
        "status_code": resp.status_code,
        "data": data,
        "api_version": api_version,
        "retryable": False,
        "failure_type": "",
    }

def log_fbr_submission(
    document_type,
    document_name,
    payload,
    response,
    status,
    defer_insert=False,
    response_status_code=None,
    retry_attempt=0,
    processing_time=None,
    user_agent="",
    ip_address="",
    api_version="",
):
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
            "fbr_invoice_number": response.get("invoiceNumber", "") if response else "",
            "response_status_code": str(response_status_code or ""),
            "retry_attempt": retry_attempt or 0,
            "processing_time": processing_time if processing_time is not None else 0,
            "user_agent": user_agent or "",
            "ip_address": ip_address or "",
            "api_version": api_version or "",
            "validation_errors": (
                response.get("validationResponse", {}).get("error")
                if isinstance(response, dict)
                else ""
            ),
        })
        if defer_insert:
            log_doc.deferred_insert()
        else:
            log_doc.insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(f"Error logging FBR submission: {str(e)}", "FBR Logging")


def _build_payload(doctype, docname):
    try:
        if doctype == "Sales Invoice":
            from fbr_e_invoicing.api.build_fbr_payload import build_fbr_payload

            return {"success": True, "payload": build_fbr_payload(docname)}
        if doctype == "POS Invoice":
            from fbr_e_invoicing.api.build_fbr_payload import build_pos_fbr_payload

            return {"success": True, "payload": build_pos_fbr_payload(docname)}
        return {
            "success": False,
            "error": "Unknown Doctype error in submit_single_invoice function",
            "failure_type": "payload_error",
        }
    except Exception as e:
        return {"success": False, "error": str(e), "failure_type": "payload_error"}


def _auto_queue_failed_submission(doctype, docname, error_message, is_retry, retryable=False):
    if is_retry or not retryable or doctype not in ("Sales Invoice", "POS Invoice"):
        return None

    try:
        from fbr_e_invoicing.api.fbr_queue import add_to_queue

        queue_result = add_to_queue(
            doctype=doctype,
            docname=docname,
            status="Pending",
            error_message=error_message,
            priority=5,
        )
        if queue_result.get("success"):
            return queue_result.get("queue_id")
        frappe.log_error(
            f"Failed to auto-queue {doctype} {docname}: {queue_result.get('error')}",
            "FBR Auto Queue Failure",
        )
    except Exception as queue_error:
        frappe.log_error(
            f"Error auto-queueing failed submission for {doctype} {docname}: {str(queue_error)}",
            "FBR Auto Queue Failure",
        )

    return None


def _is_retryable_http_status(status_code):
    if status_code is None:
        return False
    return status_code in RETRYABLE_HTTP_STATUS_CODES or status_code >= 500


def _persist_fbr_response_fields(doctype, docname, response):
    update_values = {
        "custom_fbr_invoice_number": response.get("invoiceNumber", ""),
        "custom_fbr_datetime": response.get("dated", ""),
        "custom_fbr_status": response.get("validationResponse", {}).get("status", ""),
    }

    response_field = _get_response_storage_field(doctype)
    if response_field:
        update_values[response_field] = json.dumps(response, indent=2)

    frappe.db.set_value(doctype, docname, update_values)


def _get_response_storage_field(doctype):
    fields = set(frappe.db.get_table_columns(doctype) or [])
    if "custom_fbr_response" in fields:
        return "custom_fbr_response"
    if "custom_fbr_responce" in fields:
        return "custom_fbr_responce"
    return None


def _get_request_context():
    request_obj = getattr(frappe.local, "request", None)
    if not request_obj:
        return "", ""

    user_agent = request_obj.headers.get("User-Agent", "")
    ip_address = getattr(frappe.local, "request_ip", "") or request_obj.environ.get(
        "REMOTE_ADDR", ""
    )
    return user_agent, ip_address

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
