import frappe
import json
from time import perf_counter
from frappe import _
from frappe.utils import cint, get_datetime, now
import requests
from requests.exceptions import RequestException

RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429}
NON_RETRYABLE_INVALID_HTTP_STATUS_CODES = {400, 422}
PRA_SUCCESS_CODE = "100"


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
        return str(get_datetime(text))
    except Exception:
        return None


@frappe.whitelist()
def submit_single_pra_invoice(
    doctype: str,
    docname: str,
    is_retry: bool = False,
    retry_attempt: int = 0,
):
    """
    Actually submit a single invoice to the PRA IMS API.
    Called ONLY by background worker via queue.
    """
    start_time = perf_counter()
    retry_attempt = max(0, cint(retry_attempt))
    if retry_attempt == 0 and is_retry:
        retry_attempt = 1

    try:
        doc = frappe.get_doc(doctype, docname)
    except Exception as e:
        error_message = f"Document {doctype} {docname} not found or error loading: {str(e)}"
        return {
            "success": False,
            "error": error_message,
            "retryable": False,
            "failure_type": "document_error",
            "response": {"Code": "", "Response": error_message, "Errors": error_message},
        }

    existing_status = _normalize_text(getattr(doc, "custom_pra_status", "")).lower()
    existing_invoice = _normalize_text(getattr(doc, "custom_pra_invoice_number", ""))

    if existing_status == "success" or existing_invoice:
        return {
            "success": True,
            "status": "already_submitted",
            "message": "Already completely submitted to PRA.",
            "response": {"InvoiceNumber": existing_invoice, "Code": PRA_SUCCESS_CODE},
            "retryable": False,
            "failure_type": "",
        }

    payload_result = _build_payload(doctype, docname)
    if not payload_result.get("success"):
        error_message = payload_result.get("error") or "Unable to build PRA payload"
        fallback_response = {"Code": "", "Response": error_message, "Errors": error_message}
        log_pra_submission(
            doctype,
            docname,
            {},
            fallback_response,
            "Invalid",
            retry_attempt=retry_attempt,
            processing_time=round((perf_counter() - start_time) * 1000, 2),
        )
        return {
            "success": False,
            "status": "invalid",
            "error": error_message,
            "retryable": False,
            "failure_type": payload_result.get("failure_type") or "payload_error",
            "response": fallback_response,
        }

    payload = payload_result["payload"]

    api_result = submit_to_pra_api(payload, doc.name, doctype, getattr(doc, "company", None), is_retry)
    response = api_result.get("data") or {}
    status_code = api_result.get("status_code")
    processing_time = round((perf_counter() - start_time) * 1000, 2)

    if api_result.get("success"):
        response_code = str(response.get("Code") or "")
        is_pra_valid = response_code == PRA_SUCCESS_CODE
        log_status = "Success" if is_pra_valid else "Invalid"

        log_pra_submission(
            doctype,
            docname,
            payload,
            response,
            log_status,
            response_status_code=status_code,
            retry_attempt=retry_attempt,
            processing_time=processing_time,
        )

        return {
            "success": is_pra_valid,
            "error": ""
            if is_pra_valid
            else f"PRA validation failed: Code {response_code or 'unknown'} - {response.get('Response', '')}",
            "status": "valid" if is_pra_valid else "invalid",
            "message": "Submitted to PRA" if is_pra_valid else "Submitted to PRA with error status",
            "response": response,
            "retryable": False,
            "failure_type": "" if is_pra_valid else "business_invalid",
        }

    error_message = api_result.get("error") or "PRA submission failed"
    retryable = bool(api_result.get("retryable"))
    failure_type = api_result.get("failure_type") or "http_error"
    is_non_retryable_invalid = not retryable and failure_type == "http_validation_error"

    fallback_response = response if isinstance(response, dict) else {}
    if not fallback_response.get("Response"):
        fallback_response["Response"] = error_message

    log_pra_submission(
        doctype,
        docname,
        payload,
        fallback_response,
        "Invalid" if is_non_retryable_invalid else "Error",
        response_status_code=status_code,
        retry_attempt=retry_attempt,
        processing_time=processing_time,
    )

    return {
        "success": False,
        "status": "invalid" if is_non_retryable_invalid else "error",
        "error": error_message,
        "response": fallback_response,
        "retryable": retryable,
        "failure_type": failure_type,
    }


def submit_to_pra_api(payload, document_name, document_type, company=None, is_retry=False):
    """Submit payload to the PRA IMS API via HTTP POST and return a structured result.

    Endpoint is fixed per mode (Sandbox/Production - see
    `fbr_e_invoicing.utils.PRA_SANDBOX_URL`/`PRA_PRODUCTION_URL`); only the
    Bearer token is Company-specific (falls back to the shared sandbox token
    in Sandbox mode). See `fbr_e_invoicing.utils._get_pra_endpoint`/`_get_pra_token`.
    """
    from fbr_e_invoicing.utils import _get_pra_endpoint, _get_pra_token

    api_endpoint = _get_pra_endpoint(company)
    token = _get_pra_token(company)
    company_label = company or "(default)"

    if not token:
        return {
            "success": False,
            "error": (
                f"PRA Access Token could not be resolved for Company '{company_label}'. "
                f"Open the Company form and fill in 'PRA Access Token' on the PRA tab."
            ),
            "status_code": None,
            "data": {},
            "retryable": True,
            "failure_type": "config_error",
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Document-Name": str(document_name),
        "X-Document-Type": str(document_type),
        "X-Client": "ERPNext PRA E-Invoicing",
        "X-Retry": "1" if is_retry else "0",
    }

    try:
        resp = requests.post(
            api_endpoint,
            json=payload,
            headers=headers,
            timeout=(10.0, 30.0),
            verify=True,
        )
    except RequestException as e:
        return {
            "success": False,
            "error": f"PRA HTTP Connection error: {str(e)}",
            "status_code": None,
            "data": {},
            "retryable": True,
            "failure_type": "network_error",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"PRA submission internal error: {str(e)}",
            "status_code": None,
            "data": {},
            "retryable": False,
            "failure_type": "unknown_error",
        }

    text = resp.text or ""
    try:
        data = resp.json() if text else {}
    except ValueError:
        data = {"raw": text}

    if resp.status_code >= 400:
        err_msg = None
        if isinstance(data, dict):
            err_msg = data.get("Response") or data.get("Errors") or data.get("message")
        status_line = f"HTTP {resp.status_code}"
        details = f" | Details: {err_msg}" if err_msg else (f" | Body: {text[:500]}" if text else "")

        failure_type = _classify_http_failure_type(resp.status_code)
        retryable = _is_retryable_http_status(resp.status_code)
        return {
            "success": False,
            "error": f"PRA API error {status_line}{details}",
            "status_code": resp.status_code,
            "data": data if isinstance(data, dict) else {"raw": text},
            "retryable": retryable,
            "failure_type": failure_type,
        }

    if not isinstance(data, dict):
        data = {"Response": "success", "raw": text}

    return {
        "success": True,
        "error": "",
        "status_code": resp.status_code,
        "data": data,
        "retryable": False,
        "failure_type": "",
    }


def log_pra_submission(
    document_type,
    document_name,
    payload,
    response,
    status,
    response_status_code=None,
    retry_attempt=0,
    processing_time=None,
):
    """Log a PRA submission attempt to PRA Logs (append-only audit trail)."""
    try:
        log_doc = frappe.new_doc("PRA Logs")
        log_doc.update(
            {
                "document_type": document_type,
                "document_name": document_name,
                "request_payload": json.dumps(payload, indent=2) if payload else "",
                "response_data": json.dumps(response, indent=2) if response else "",
                "status": status,
                "submitted_at": now(),
                "pra_invoice_number": response.get("InvoiceNumber", "") if response else "",
                "response_status_code": str(response_status_code or ""),
                "retry_attempt": retry_attempt or 0,
                "processing_time": processing_time if processing_time is not None else 0,
                "validation_errors": (response.get("Errors") or "") if isinstance(response, dict) else "",
            }
        )
        log_doc.insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(f"Error logging PRA submission: {str(e)}", "PRA Logging")


def _build_payload(doctype, docname):
    try:
        if doctype == "Sales Invoice":
            from fbr_e_invoicing.api.build_pra_payload import build_pra_payload

            return {"success": True, "payload": build_pra_payload(docname)}

        return {
            "success": False,
            "error": "Unsupported Doctype in PRA API submission",
            "failure_type": "payload_error",
        }
    except Exception as e:
        return {"success": False, "error": str(e), "failure_type": "payload_error"}


def _classify_http_failure_type(status_code):
    if status_code in NON_RETRYABLE_INVALID_HTTP_STATUS_CODES:
        return "http_validation_error"
    return "http_error"


def _is_retryable_http_status(status_code):
    if status_code is None:
        return False
    if status_code in NON_RETRYABLE_INVALID_HTTP_STATUS_CODES:
        return False
    return status_code >= 400 or status_code in RETRYABLE_HTTP_STATUS_CODES


def _persist_pra_response_fields(doctype, docname, response):
    """Persist PRA response fields via the DB API, bypassing document hooks
    so the write stays atomic with the queue's own state transition."""
    try:
        code = str(response.get("Code") or "")
        is_success = code == PRA_SUCCESS_CODE
        # PRA returns literal placeholder text like "Not Available" for
        # InvoiceNumber on failure (unlike FBR, which leaves it blank) - only
        # trust it on an actual success so failed attempts don't get
        # mistaken for "already submitted" by `_is_already_submitted`.
        invoice_number = _normalize_text(response.get("InvoiceNumber", "")) if is_success else ""
        status = "Success" if is_success else _normalize_text(response.get("Response", "")) or "Error"

        frappe.db.set_value(
            doctype,
            docname,
            {
                "custom_pra_invoice_number": invoice_number,
                "custom_pra_datetime": _normalize_datetime_for_db(now()),
                "custom_pra_status": status,
                "custom_pra_response": json.dumps(response, indent=2),
            },
            update_modified=False,
        )
    except Exception as e:
        frappe.log_error(
            f"Error persisting PRA response fields for {doctype} {docname}: {str(e)}",
            "PRA Response Persistence",
        )
