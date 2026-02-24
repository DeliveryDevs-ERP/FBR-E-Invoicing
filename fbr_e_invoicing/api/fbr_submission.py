import frappe
import json
from time import perf_counter
from frappe.utils import cint, get_datetime, now
import requests
from requests.exceptions import RequestException

RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429}


def _normalize_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    if text.casefold() in {"none", "null"}:
        return ""
    return text


def _normalize_datetime_for_db(value):
    text = _normalize_text(value)
    if not text:
        return None
    try:
        # Keep DB datetime writes safe even if API returns a loosely formatted value.
        return str(get_datetime(text))
    except Exception:
        return None


@frappe.whitelist()
def submit_single_invoice(doctype, docname, is_retry=False, retry_attempt=0):
    """
    Actually submit a single invoice to FBR API.
    Called ONLY by background worker via queue.
    """
    start_time = perf_counter()
    retry_attempt = max(0, cint(retry_attempt))
    if retry_attempt == 0 and is_retry:
        retry_attempt = 1

    # 1. Verification of document and state
    try:
        doc = frappe.get_doc(doctype, docname)
    except Exception as e:
        error_message = (
            f"Document {doctype} {docname} not found or error loading: {str(e)}"
        )
        return {
            "success": False,
            "error": error_message,
            "retryable": False,
            "failure_type": "document_error",
            "response": {
                "validationResponse": {"status": "Error", "error": error_message}
            },
        }

    existing_status = _normalize_text(getattr(doc, "custom_fbr_status", "")).lower()
    existing_invoice = _normalize_text(getattr(doc, "custom_fbr_invoice_number", ""))

    if existing_status == "valid" or existing_invoice:
        # Already submitted
        return {
            "success": True,
            "status": "already_submitted",
            "message": "Already completely submitted to FBR.",
            "response": _get_stored_fbr_response(doc, "Valid", existing_invoice),
            "retryable": False,
            "failure_type": "",
        }

    # 2. Build Payload
    payload_result = _build_payload(doctype, docname)
    if not payload_result.get("success"):
        error_message = payload_result.get("error") or "Unable to build FBR payload"
        fallback_response = {
            "validationResponse": {"status": "Error", "error": error_message}
        }
        log_fbr_submission(
            doctype,
            docname,
            {},
            fallback_response,
            "Error",
            retry_attempt=retry_attempt,
            processing_time=round((perf_counter() - start_time) * 1000, 2),
        )
        return {
            "success": False,
            "error": error_message,
            "retryable": False,
            "failure_type": payload_result.get("failure_type") or "payload_error",
            "response": fallback_response,
        }

    payload = payload_result["payload"]

    # 3. HTTP Submit
    api_result = submit_to_fbr_api(payload, doc.name, doctype, is_retry)
    response = api_result.get("data") or {}
    status_code = api_result.get("status_code")
    processing_time = round((perf_counter() - start_time) * 1000, 2)
    api_version = api_result.get("api_version")

    # 4. Handle HTTP Result
    if api_result.get("success"):
        fbr_status = response.get("validationResponse", {}).get("status", "")
        is_fbr_valid = fbr_status == "Valid"
        log_status = "Success" if is_fbr_valid else "Invalid"

        log_fbr_submission(
            doctype,
            docname,
            payload,
            response,
            log_status,
            response_status_code=status_code,
            retry_attempt=retry_attempt,
            processing_time=processing_time,
            api_version=api_version,
        )

        result = {
            "success": is_fbr_valid,  # Only full success if API returned 200 AND FBR said Valid
            "error": f"FBR validation failed: {fbr_status or 'Invalid'}"
            if not is_fbr_valid
            else "",
            "status": "valid" if is_fbr_valid else "invalid",
            "message": "Submitted to FBR"
            if is_fbr_valid
            else "Submitted to FBR with Invalid status",
            "response": response,
            "retryable": False,
            "failure_type": "" if is_fbr_valid else "business_invalid",
        }

        # Don't try to persist fields inside this function, let the caller (the queue worker) do it
        # so any database errors trying to save the invoice roll back cleanly with the queue state.
        return result

    # 5. Handle HTTP Failure
    error_message = api_result.get("error") or "FBR submission failed"
    retryable = bool(api_result.get("retryable"))

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
        api_version=api_version,
    )

    return {
        "success": False,
        "status": "error",
        "error": error_message,
        "response": fallback_response,
        "retryable": retryable,
        "failure_type": api_result.get("failure_type") or "http_error",
    }


@frappe.whitelist()
def bulk_submit_invoices(docnames):
    """
    Queue multiple Sales Invoices for FBR submission.
    POS invoices queue themselves automatically on submit.
    """
    if isinstance(docnames, str):
        try:
            docnames = json.loads(docnames)
        except Exception:
            docnames = [docnames]

    if not isinstance(docnames, list):
        frappe.throw("docnames must be a list or JSON array")

    unique_docnames = list(set([str(name).strip() for name in docnames if name]))
    if not unique_docnames:
        return {"queued_count": 0, "messages": ["No valid document names provided."]}

    invoices = frappe.get_all(
        "Sales Invoice",
        filters={"name": ["in", unique_docnames]},
        fields=["name", "docstatus", "custom_fbr_status", "custom_fbr_invoice_number"],
    )

    invoice_map = {row.name: row for row in invoices}
    results = {
        "queued": [],
        "drafts_skipped": [],
        "cancelled_skipped": [],
        "already_submitted_skipped": [],
        "failed_to_queue": [],
    }
    failed_to_queue_details = []

    from fbr_e_invoicing.api.fbr_queue import add_to_queue

    for docname in unique_docnames:
        doc = invoice_map.get(docname)
        if not doc:
            reason = "Not found"
            results["failed_to_queue"].append(f"{docname} ({reason})")
            failed_to_queue_details.append({"name": docname, "error": reason})
            continue

        if doc.docstatus == 0:
            results["drafts_skipped"].append(docname)
            continue
        if doc.docstatus == 2:
            results["cancelled_skipped"].append(docname)
            continue
        if doc.docstatus != 1:
            reason = f"Unsupported document status: {doc.docstatus}"
            results["failed_to_queue"].append(f"{docname} ({reason})")
            failed_to_queue_details.append({"name": docname, "error": reason})
            continue

        # Prevent double queues visually (queue script also catches this, but catch early here)
        if (
            str(doc.custom_fbr_status or "").strip().lower() == "valid"
            or str(doc.custom_fbr_invoice_number or "").strip()
        ):
            results["already_submitted_skipped"].append(docname)
            continue

        try:
            queue_result = add_to_queue(
                doctype="Sales Invoice", docname=docname, status="Pending"
            )
            if queue_result.get("success"):
                results["queued"].append(docname)
            else:
                reason = str(queue_result.get("error") or "Unknown error")
                results["failed_to_queue"].append(f"{docname} ({reason})")
                failed_to_queue_details.append({"name": docname, "error": reason})
        except Exception as e:
            reason = str(e)
            results["failed_to_queue"].append(f"{docname} ({reason})")
            failed_to_queue_details.append({"name": docname, "error": reason})

    # Trigger queue execution
    if results["queued"]:
        try:
            from fbr_e_invoicing.api.fbr_queue import process_queue

            process_queue(limit=len(results["queued"]))
        except Exception as e:
            frappe.log_error(
                f"Error triggering background processing: {str(e)}", "FBR Bulk Submit"
            )

    # Generate user-friendly summary message
    msg = []
    if results["queued"]:
        msg.append(f"Successfully queued {len(results['queued'])} invoices.")
    if results["drafts_skipped"]:
        msg.append(f"Skipped {len(results['drafts_skipped'])} Draft invoices.")
    if results["cancelled_skipped"]:
        msg.append(f"Skipped {len(results['cancelled_skipped'])} Cancelled invoices.")
    if results["already_submitted_skipped"]:
        msg.append(
            f"Skipped {len(results['already_submitted_skipped'])} already submitted invoices."
        )
    if results["failed_to_queue"]:
        msg.append(f"Failed to queue {len(results['failed_to_queue'])} invoices.")
    msg.append('(Note: Records with status "Draft" or "Cancelled" are not posted to FBR.)')

    return {
        "queued_count": len(results["queued"]),
        "results": results,
        "draft_invoices": list(results["drafts_skipped"]),
        "cancelled_invoices": list(results["cancelled_skipped"]),
        "already_submitted_invoices": list(results["already_submitted_skipped"]),
        "failed_invoices": failed_to_queue_details,
        "queue_route": "/app/fbr-queue",
        "message": " ".join(msg),
    }


@frappe.whitelist()
def bulk_submit_sales_invoices(docnames):
    """Compatibility shim for old UI button"""
    return bulk_submit_invoices(docnames)


def submit_pos_invoice_on_submit(doc, method):
    """
    Called by standard ERPNext hooks when a POS Invoice is submitted.
    Automatically adds it to the FBR queue for processing.
    """
    if not getattr(doc, "custom_submit_to_fbr", 0):
        return

    from fbr_e_invoicing.api.fbr_queue import add_to_queue

    try:
        add_to_queue(doctype=doc.doctype, docname=doc.name, status="Pending")
        frappe.msgprint(
            "Invoice has been queued for background FBR submission.",
            title="Queued for FBR",
            indicator="blue",
            alert=True,
        )
    except Exception as e:
        frappe.log_error(
            f"Failed to auto-queue POS Invoice {doc.name}: {str(e)}", "FBR Auto Queue"
        )


def submit_to_fbr_api(payload, document_name, document_type, is_retry=False):
    """Submit payload to FBR API via HTTP POST and return structured result."""
    fbr_settings = frappe.get_single("FBR E-Inv Setup")
    api_endpoint = (fbr_settings.api_endpoint or "").strip()
    token = (fbr_settings.pral_authorization_token or "").strip()

    if not api_endpoint or not token:
        return {
            "success": False,
            "error": "FBR API settings not configured in 'FBR E-Inv Setup'.",
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
            "error": f"FBR HTTP Connection error: {str(e)}",
            "status_code": None,
            "data": {},
            "api_version": "",
            "retryable": True,
            "failure_type": "network_error",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"FBR submission internal error: {str(e)}",
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
        log_doc.update(
            {
                "document_type": document_type,
                "document_name": document_name,
                "request_payload": json.dumps(payload, indent=2) if payload else "",
                "response_data": json.dumps(response, indent=2) if response else "",
                "status": status,
                "submitted_at": now(),
                "fbr_invoice_number": response.get("invoiceNumber", "")
                if response
                else "",
                "response_status_code": str(response_status_code or ""),
                "retry_attempt": retry_attempt or 0,
                "processing_time": processing_time
                if processing_time is not None
                else 0,
                "user_agent": user_agent or "",
                "ip_address": ip_address or "",
                "api_version": api_version or "",
                "validation_errors": (
                    response.get("validationResponse", {}).get("error")
                    if isinstance(response, dict)
                    else ""
                ),
            }
        )
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
            "error": "Unsupported Doctype in FBR API submission",
            "failure_type": "payload_error",
        }
    except Exception as e:
        return {"success": False, "error": str(e), "failure_type": "payload_error"}


def _is_retryable_http_status(status_code):
    if status_code is None:
        return False
    return status_code in RETRYABLE_HTTP_STATUS_CODES or status_code >= 500


def _persist_fbr_response_fields(doctype, docname, response):
    """Update Frappe document securely. Ensure data stays atomized with queue"""
    try:
        doc = frappe.get_doc(doctype, docname)

        doc.custom_fbr_invoice_number = _normalize_text(response.get("invoiceNumber", ""))
        doc.custom_fbr_datetime = _normalize_datetime_for_db(response.get("dated", ""))
        doc.custom_fbr_status = _normalize_text(
            response.get("validationResponse", {}).get("status", "")
        )

        # Determine raw storage field
        response_field = _get_response_storage_field(doctype)
        if response_field:
            setattr(doc, response_field, json.dumps(response, indent=2))

        # Update using DB API to avoid heavy document hooks
        frappe.db.set_value(
            doctype,
            docname,
            {
                "custom_fbr_invoice_number": doc.custom_fbr_invoice_number,
                "custom_fbr_datetime": doc.custom_fbr_datetime,
                "custom_fbr_status": doc.custom_fbr_status,
                **(
                    {response_field: getattr(doc, response_field)}
                    if response_field
                    else {}
                ),
            },
            update_modified=False,
        )
    except Exception as e:
        frappe.log_error(
            f"Error persisting FBR response fields for {doctype} {docname}: {str(e)}",
            "FBR Response Persistence",
        )


def _get_stored_fbr_response(doc, status="", invoice_number=""):
    response_field = _get_response_storage_field(doc.doctype)
    if response_field:
        raw_response = getattr(doc, response_field, "")
        if raw_response:
            try:
                return json.loads(raw_response)
            except Exception:
                pass

    fallback_response = {}
    normalized_invoice_number = _normalize_text(invoice_number)
    if normalized_invoice_number:
        fallback_response["invoiceNumber"] = normalized_invoice_number
    dated = _normalize_text(getattr(doc, "custom_fbr_datetime", ""))
    if dated:
        fallback_response["dated"] = dated
    normalized_status = _normalize_text(status)
    if normalized_status:
        fallback_response["validationResponse"] = {"status": normalized_status}
    return fallback_response


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
    return request_obj.headers.get("User-Agent", ""), getattr(
        frappe.local, "request_ip", ""
    ) or request_obj.environ.get("REMOTE_ADDR", "")


@frappe.whitelist()
def get_fbr_submission_stats():
    """Get FBR submission statistics"""
    try:
        stats = frappe.db.sql(
            """
            SELECT 
                status,
                COUNT(*) as count
            FROM `tabFBR Logs`
            WHERE DATE(submitted_at) = CURDATE()
            GROUP BY status
        """,
            as_dict=True,
        )

        # Get queue statistics
        queue_stats = frappe.db.sql(
            """
            SELECT status, COUNT(*) as count 
            FROM `tabFBR Queue` 
            GROUP BY status
        """,
            as_dict=True,
        )

        return {"today_stats": stats, "queue_stats": queue_stats}
    except Exception as e:
        frappe.log_error(f"Error getting FBR stats: {str(e)}", "FBR Stats")
        return {"error": str(e)}
