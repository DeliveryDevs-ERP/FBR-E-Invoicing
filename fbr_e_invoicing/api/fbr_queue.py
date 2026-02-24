import json
import frappe
from frappe.utils import add_to_date, cint, now, now_datetime

DEFAULT_MAX_RETRIES = 5
DEFAULT_PRIORITY = 5
MIN_PRIORITY = 1
MAX_PRIORITY = 10
DEFAULT_SCHEDULER_RETRY_INTERVAL_MINUTES = 2
STUCK_PROCESSING_TIMEOUT_MINUTES = 30
VALID_QUEUE_STATUSES = {"Pending", "Processing", "Completed", "Failed"}


def _get_effective_max_retries(max_retries):
    value = cint(max_retries)
    return value if value > 0 else DEFAULT_MAX_RETRIES


def _normalize_priority(priority):
    if priority is None or priority == "":
        return DEFAULT_PRIORITY
    value = cint(priority)
    return max(MIN_PRIORITY, min(value, MAX_PRIORITY))


def _get_next_retry_at(reference_time=None):
    return add_to_date(
        reference_time or now_datetime(),
        minutes=DEFAULT_SCHEDULER_RETRY_INTERVAL_MINUTES,
    )


def _normalize_queue_status(status):
    normalized_status = str(status or "").strip()
    return normalized_status if normalized_status in VALID_QUEUE_STATUSES else "Pending"


def _has_retry_left(queue_doc):
    return cint(queue_doc.retry_count) < _get_effective_max_retries(
        queue_doc.max_retries
    )


def _sync_retry_fields(queue_doc):
    effective_max_retries = _get_effective_max_retries(queue_doc.max_retries)
    queue_doc.max_retries = effective_max_retries
    if queue_doc.meta.has_field("remaining_retries"):
        queue_doc.remaining_retries = max(
            0, effective_max_retries - cint(queue_doc.retry_count)
        )
    return effective_max_retries


def _mark_queue_retry_or_fail(queue_doc, error_message, response=None):
    queue_doc.retry_count = cint(queue_doc.retry_count) + 1
    queue_doc.last_retry_at = now()
    queue_doc.error_message = error_message

    if response and isinstance(response, dict):
        if queue_doc.meta.has_field("fbr_response"):
            queue_doc.fbr_response = json.dumps(response, indent=2)

    max_retries = _sync_retry_fields(queue_doc)

    if queue_doc.retry_count >= max_retries:
        queue_doc.status = "Failed"
        queue_doc.next_retry_at = None
    else:
        queue_doc.status = "Pending"
        queue_doc.next_retry_at = _get_next_retry_at()

    queue_doc.save(ignore_permissions=True)


def _mark_queue_terminal_failure(queue_doc, error_message, response=None):
    max_retries = _get_effective_max_retries(queue_doc.max_retries)
    queue_doc.max_retries = max_retries
    queue_doc.status = "Failed"
    queue_doc.retry_count = max_retries
    queue_doc.last_retry_at = now()
    queue_doc.next_retry_at = None
    queue_doc.error_message = error_message

    if response and isinstance(response, dict):
        if queue_doc.meta.has_field("fbr_response"):
            queue_doc.fbr_response = json.dumps(response, indent=2)

    _sync_retry_fields(queue_doc)

    queue_doc.save(ignore_permissions=True)


def _build_detailed_error_message(response, fallback_message):
    fallback = fallback_message or "FBR submission failed"
    if not isinstance(response, dict) or not response:
        return fallback

    validation = response.get("validationResponse") or {}
    status = validation.get("status") or ""
    status_code = validation.get("statusCode") or ""
    header_parts = ["FBR validation failed"]
    meta = []
    if status:
        meta.append(f"status={status}")
    if status_code:
        meta.append(f"statusCode={status_code}")
    if meta:
        header_parts.append(f"({', '.join(meta)})")
    details = [" ".join(header_parts)]

    top_error = validation.get("error")
    if top_error:
        details.append(str(top_error))

    invoice_statuses = validation.get("invoiceStatuses")
    if isinstance(invoice_statuses, list):
        item_errors = []
        for row in invoice_statuses:
            if not isinstance(row, dict):
                continue
            parts = []
            if row.get("itemSNo"):
                parts.append(f"item={row.get('itemSNo')}")
            if row.get("statusCode"):
                parts.append(f"statusCode={row.get('statusCode')}")
            if row.get("errorCode"):
                parts.append(f"errorCode={row.get('errorCode')}")
            if row.get("error"):
                parts.append(f"error={row.get('error')}")
            elif row.get("status"):
                parts.append(f"status={row.get('status')}")
            if parts:
                item_errors.append(", ".join(parts))
        if item_errors:
            details.append("invoiceStatuses: " + " | ".join(item_errors))

    details.append("full_response=" + json.dumps(response, ensure_ascii=False))
    return " | ".join(details)


def recover_stuck_and_retryable_items():
    """Recover stale Processing rows and retryable Failed rows."""
    now_dt = now_datetime()
    stuck_cutoff = add_to_date(now_dt, minutes=-STUCK_PROCESSING_TIMEOUT_MINUTES)

    stuck_recovered = 0
    failed_recovered = 0

    stuck_items = frappe.get_all(
        "FBR Queue", filters={"status": "Processing", "modified": ["<=", stuck_cutoff]}
    )
    for item in stuck_items:
        try:
            doc = frappe.get_doc("FBR Queue", item.name)
            _mark_queue_retry_or_fail(
                doc,
                f"Recovered stuck processing item after {STUCK_PROCESSING_TIMEOUT_MINUTES} minutes",
            )
            stuck_recovered += 1
        except Exception as e:
            frappe.log_error(
                f"Error recovering stuck FBR Queue item {item.name}: {str(e)}"
            )

    failed_items = frappe.get_all("FBR Queue", filters={"status": "Failed"})
    for item in failed_items:
        try:
            doc = frappe.get_doc("FBR Queue", item.name)
            if _has_retry_left(doc):
                doc.status = "Pending"
                doc.error_message = ""
                doc.next_retry_at = _get_next_retry_at(now_dt)
                _sync_retry_fields(doc)
                doc.save(ignore_permissions=True)
                failed_recovered += 1
        except Exception as e:
            frappe.log_error(
                f"Error recovering failed FBR Queue item {item.name}: {str(e)}"
            )

    return {"stuck_recovered": stuck_recovered, "failed_recovered": failed_recovered}


@frappe.whitelist()
def add_to_queue(
    doctype,
    docname,
    status="Pending",
    error_message="",
    priority=None,
    max_retries=None,
):
    """Add a document to the FBR queue with strict duplicate prevention."""
    try:
        # 1. Document status check
        docstatus = frappe.db.get_value(doctype, docname, "docstatus")
        if docstatus is None:
            frappe.throw(f"Cannot queue {doctype} '{docname}' because it does not exist.")
        if docstatus == 0:
            frappe.throw(f"Cannot queue {doctype} '{docname}' because it is a Draft.")
        if docstatus == 2:
            frappe.throw(
                f"Cannot queue {doctype} '{docname}' because it is Cancelled."
            )
        if docstatus != 1:
            frappe.throw(
                f"Cannot queue {doctype} '{docname}' because only submitted records are allowed."
            )

        # 2. Duplicate success check
        doc_fbr_status, doc_fbr_invoice_number = frappe.db.get_value(
            doctype, docname, ["custom_fbr_status", "custom_fbr_invoice_number"]
        )
        if (
            str(doc_fbr_status or "").strip().lower() == "valid"
            or str(doc_fbr_invoice_number or "").strip()
        ):
            frappe.throw(
                f"{doctype} '{docname}' has already been successfully submitted to FBR."
            )

        # 3. Existing queue check
        existing_queue_id = frappe.db.exists(
            "FBR Queue", {"document_type": doctype, "document_name": docname}
        )

        normalized_status = _normalize_queue_status(status)
        effective_priority = _normalize_priority(priority)
        effective_max_retries = _get_effective_max_retries(max_retries)

        if existing_queue_id:
            queue_doc = frappe.get_doc("FBR Queue", existing_queue_id)
            if queue_doc.status in ("Pending", "Processing"):
                # Update settings if provided, but don't re-queue
                changed = False
                previous_max_retries = cint(queue_doc.max_retries)
                previous_remaining_retries = (
                    cint(queue_doc.remaining_retries)
                    if queue_doc.meta.has_field("remaining_retries")
                    else None
                )
                if (
                    priority is not None
                    and getattr(queue_doc, "priority", None) != effective_priority
                ):
                    queue_doc.priority = effective_priority
                    changed = True
                if (
                    max_retries is not None
                    and getattr(queue_doc, "max_retries", None) != effective_max_retries
                ):
                    queue_doc.max_retries = effective_max_retries
                    changed = True
                _sync_retry_fields(queue_doc)
                if cint(queue_doc.max_retries) != previous_max_retries:
                    changed = True
                if queue_doc.meta.has_field("remaining_retries"):
                    if cint(queue_doc.remaining_retries) != previous_remaining_retries:
                        changed = True
                if changed:
                    queue_doc.save(ignore_permissions=True)
                    frappe.msgprint(
                        f"Updated queue priority/retries for {doctype} '{docname}'."
                    )
                return {
                    "success": True,
                    "queue_id": queue_doc.name,
                    "state": "already_queued",
                }

            if queue_doc.status == "Failed":
                if not _has_retry_left(queue_doc):
                    # Retries exhausted. Reset and requeue.
                    queue_doc.status = "Pending"
                    queue_doc.retry_count = 0
                    queue_doc.error_message = ""
                    queue_doc.next_retry_at = now_datetime()
                    if priority is not None:
                        queue_doc.priority = effective_priority
                    if max_retries is not None:
                        queue_doc.max_retries = effective_max_retries

                    _sync_retry_fields(queue_doc)

                    queue_doc.save(ignore_permissions=True)
                    return {
                        "success": True,
                        "queue_id": queue_doc.name,
                        "state": "requeued",
                    }
                else:
                    # Failed item still has retries left (e.g. max_retries increased). Requeue it.
                    queue_doc.status = "Pending"
                    queue_doc.error_message = ""
                    queue_doc.next_retry_at = now_datetime()
                    if priority is not None:
                        queue_doc.priority = effective_priority
                    if max_retries is not None:
                        queue_doc.max_retries = effective_max_retries
                    _sync_retry_fields(queue_doc)
                    queue_doc.save(ignore_permissions=True)
                    return {
                        "success": True,
                        "queue_id": queue_doc.name,
                        "state": "requeued",
                    }

        # 4. Create new
        queue_doc = frappe.new_doc("FBR Queue")
        queue_doc.document_type = doctype
        queue_doc.document_name = docname
        queue_doc.status = normalized_status
        queue_doc.priority = effective_priority
        queue_doc.max_retries = effective_max_retries
        queue_doc.error_message = error_message
        queue_doc.retry_count = 0
        queue_doc.created_at = now()
        queue_doc.next_retry_at = (
            now_datetime() if normalized_status == "Pending" else None
        )
        _sync_retry_fields(queue_doc)

        queue_doc.insert(ignore_permissions=True)

        return {"success": True, "queue_id": queue_doc.name, "state": "queued"}

    except Exception as e:
        frappe.log_error(
            f"Error adding {doctype} {docname} to FBR queue: {str(e)}", "FBR Queue"
        )
        if getattr(e, "http_status_code", None) == 417 or isinstance(
            e, frappe.ValidationError
        ):
            raise e  # Let frappe throw pop up to UI if it's our validation
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def process_queue(limit=50):
    """Process due pending items by enqueuing each as a background job."""
    try:
        limit = cint(limit) or 50
        queue_items = frappe.get_all(
            "FBR Queue",
            filters={"status": "Pending", "next_retry_at": ["<=", now_datetime()]},
            order_by="priority DESC, created_at ASC",
            limit=limit,
        )

        enqueued_count = 0
        for item in queue_items:
            try:
                frappe.enqueue(
                    "fbr_e_invoicing.api.fbr_queue._process_single_queue_item",
                    queue="short",
                    queue_item_name=item.name,
                    enqueue_after_commit=True,
                    job_id=f"fbr_queue_item::{item.name}",
                    deduplicate=True,
                )
                enqueued_count += 1
            except Exception as e:
                frappe.db.set_value(
                    "FBR Queue",
                    item.name,
                    {"error_message": f"Enqueue failed: {str(e)}"},
                )

        return {"enqueued_count": enqueued_count, "processed_count": enqueued_count}

    except Exception as e:
        frappe.log_error(f"Error processing FBR queue: {str(e)}", "FBR Queue")
        return {"enqueued_count": 0, "processed_count": 0, "error": str(e)}


def _process_single_queue_item(queue_item_name):
    """Process a single queue item in its own background job."""
    try:
        if not frappe.db.exists("FBR Queue", queue_item_name):
            return

        queue_entry = frappe.get_doc("FBR Queue", queue_item_name)
        if queue_entry.status != "Pending":
            return

        previous_max_retries = cint(queue_entry.max_retries)
        previous_remaining_retries = (
            cint(queue_entry.remaining_retries)
            if queue_entry.meta.has_field("remaining_retries")
            else None
        )
        _sync_retry_fields(queue_entry)
        retry_fields_changed = cint(queue_entry.max_retries) != previous_max_retries
        if queue_entry.meta.has_field("remaining_retries"):
            retry_fields_changed = retry_fields_changed or (
                cint(queue_entry.remaining_retries) != previous_remaining_retries
            )
        if retry_fields_changed:
            queue_entry.save(ignore_permissions=True)
            frappe.db.commit()

        if not _has_retry_left(queue_entry):
            _mark_queue_terminal_failure(queue_entry, "Max retries exceeded")
            return

        # Simple Claim
        queue_entry.status = "Processing"
        queue_entry.save(ignore_permissions=True)
        frappe.db.commit()  # Force commit so other workers see it's processing

        # Import locally to avoid circular dependencies
        from fbr_e_invoicing.api.fbr_submission import (
            submit_single_invoice,
            _persist_fbr_response_fields,
            log_fbr_submission,
        )

        retry_attempt = cint(queue_entry.retry_count) + 1
        docstatus = frappe.db.get_value(
            queue_entry.document_type, queue_entry.document_name, "docstatus"
        )
        if docstatus != 1:
            docstatus_reason = (
                "not found"
                if docstatus is None
                else ("draft" if docstatus == 0 else "cancelled" if docstatus == 2 else str(docstatus))
            )
            error_message = (
                "Skipped FBR submission because document is no longer submitted "
                f"(docstatus={docstatus_reason})."
            )
            log_fbr_submission(
                queue_entry.document_type,
                queue_entry.document_name,
                {},
                {"validationResponse": {"status": "Error", "error": error_message}},
                "Error",
                retry_attempt=retry_attempt,
            )
            frappe.delete_doc("FBR Queue", queue_item_name, ignore_permissions=True)
            return

        submission_result = submit_single_invoice(
            queue_entry.document_type,
            queue_entry.document_name,
            is_retry=True,
            retry_attempt=retry_attempt,
        )

        response = submission_result.get("response") or {}
        if isinstance(response, dict) and response:
            _persist_fbr_response_fields(
                queue_entry.document_type,
                queue_entry.document_name,
                response,
            )

        if (
            submission_result.get("success")
            or submission_result.get("status") == "already_submitted"
        ):
            # DELETE ON SUCCESS
            frappe.delete_doc("FBR Queue", queue_item_name, ignore_permissions=True)
            return

        # Handle failure
        error_message = _build_detailed_error_message(
            response, submission_result.get("message") or "FBR submission failed"
        )

        # Business-invalid payloads are not queue/system failures; keep them out of queue backlog.
        if submission_result.get("failure_type") == "business_invalid":
            frappe.delete_doc("FBR Queue", queue_item_name, ignore_permissions=True)
        elif submission_result.get("retryable", True):
            _mark_queue_retry_or_fail(queue_entry, error_message, response)
        else:
            _mark_queue_terminal_failure(queue_entry, error_message, response)

    except Exception as e:
        frappe.log_error(
            f"Error executing queue item {queue_item_name}: {str(e)}",
            "FBR Queue Processing",
        )
        queue_entry = frappe.get_doc("FBR Queue", queue_item_name)
        _mark_queue_retry_or_fail(queue_entry, f"Exception: {str(e)}")


@frappe.whitelist()
def get_queue_status():
    """Get current queue status."""
    try:
        status_counts = frappe.db.sql(
            """
            SELECT status, COUNT(*) as count
            FROM `tabFBR Queue`
            GROUP BY status
            """,
            as_dict=True,
        )

        failed_items = frappe.get_all(
            "FBR Queue",
            filters={"status": "Failed"},
            fields=[
                "document_type",
                "document_name",
                "error_message",
                "retry_count",
                "created_at",
                "max_retries",
                "next_retry_at",
            ],
            order_by="modified desc",
            limit=10,
        )

        return {"status_counts": status_counts, "failed_items": failed_items}

    except Exception as e:
        frappe.log_error(f"Error getting queue status: {str(e)}", "FBR Queue")
        return {"status_counts": [], "failed_items": []}


@frappe.whitelist()
def retry_failed_items():
    """Retry all failed items that still have retries available."""
    try:
        failed_items = frappe.get_all("FBR Queue", filters={"status": "Failed"})
        retry_count = 0
        now_dt = now_datetime()

        for item in failed_items:
            doc = frappe.get_doc("FBR Queue", item.name)
            if _has_retry_left(doc):
                doc.status = "Pending"
                doc.error_message = ""
                doc.next_retry_at = _get_next_retry_at(now_dt)
                _sync_retry_fields(doc)
                doc.save(ignore_permissions=True)
                retry_count += 1

        return {"retry_count": retry_count}
    except Exception as e:
        frappe.log_error(f"Error retrying failed items: {str(e)}", "FBR Queue")
        return {"retry_count": 0, "error": str(e)}


def process_fbr_queue_scheduled():
    """Scheduled task to recover queue and process due pending items."""
    try:
        recovery = recover_stuck_and_retryable_items()
        result = process_queue(limit=20)

        if (
            recovery.get("stuck_recovered")
            or recovery.get("failed_recovered")
            or result.get("enqueued_count")
        ):
            frappe.log_error(
                f"Scheduled FBR queue processing: recovery={recovery}, queue={result}",
                "FBR Queue Scheduled",
            )
    except Exception as e:
        frappe.log_error(
            f"Error in scheduled FBR queue processing: {str(e)}", "FBR Queue Scheduled"
        )
