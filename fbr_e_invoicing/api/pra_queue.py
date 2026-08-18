import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now, now_datetime

DEFAULT_MAX_RETRIES = 5
STUCK_PROCESSING_TIMEOUT_MINUTES = 30
VALID_QUEUE_STATUSES = {"Pending", "Processing", "Completed", "Failed"}
SUPPORTED_DOCUMENT_TYPES = {"Sales Invoice"}
NON_RETRYABLE_INVALID_FAILURE_TYPES = {
    "payload_error",
    "business_invalid",
    "http_validation_error",
}


def _normalize_queue_status(status):
    normalized_status = str(status or "").strip()
    return normalized_status if normalized_status in VALID_QUEUE_STATUSES else "Pending"


def _has_retry_left(queue_doc):
    return cint(queue_doc.retry_count) < DEFAULT_MAX_RETRIES


def _sync_retry_fields(queue_doc):
    if queue_doc.meta.has_field("remaining_retries"):
        queue_doc.remaining_retries = max(0, DEFAULT_MAX_RETRIES - cint(queue_doc.retry_count))
    return DEFAULT_MAX_RETRIES


def _get_next_retry_at(reference_time=None):
    # Failed items become eligible immediately and are picked up on next scheduler run.
    return reference_time or now_datetime()


def _set_queue_response(queue_doc, response=None):
    if response and isinstance(response, dict) and queue_doc.meta.has_field("pra_response"):
        queue_doc.pra_response = json.dumps(response, indent=2)


def _remove_terminal_queue_item(queue_doc):
    if frappe.db.exists("PRA Queue", queue_doc.name):
        frappe.delete_doc("PRA Queue", queue_doc.name, ignore_permissions=True)


def _mark_queue_retry_or_fail(
    queue_doc,
    error_message,
    response=None,
    retryable=True,
    increment_attempt=True,
):
    if increment_attempt:
        queue_doc.retry_count = cint(queue_doc.retry_count) + 1

    queue_doc.last_retry_at = now()
    queue_doc.error_message = error_message
    _set_queue_response(queue_doc, response)

    max_retries = _sync_retry_fields(queue_doc)
    queue_doc.status = "Failed"

    if retryable and cint(queue_doc.retry_count) < max_retries:
        queue_doc.next_retry_at = _get_next_retry_at()
    else:
        queue_doc.next_retry_at = None

    queue_doc.save(ignore_permissions=True)


def _mark_queue_terminal_failure(queue_doc, error_message, response=None, increment_attempt=True):
    _mark_queue_retry_or_fail(
        queue_doc,
        error_message,
        response=response,
        retryable=False,
        increment_attempt=increment_attempt,
    )


def _build_detailed_error_message(response, fallback_message):
    fallback = fallback_message or "PRA submission failed"
    if not isinstance(response, dict) or not response:
        return fallback

    details = [f"PRA submission failed (Code={response.get('Code') or 'unknown'})"]
    if response.get("Response"):
        details.append(str(response.get("Response")))
    if response.get("Errors"):
        details.append(f"errors={response.get('Errors')}")
    details.append("full_response=" + json.dumps(response, ensure_ascii=False))
    return " | ".join(details)


def _has_status_row(status, exclude_name=None):
    filters = {"status": status}
    if exclude_name:
        filters["name"] = ["!=", exclude_name]
    return bool(frappe.db.exists("PRA Queue", filters))


def _enqueue_queue_item(queue_item_name):
    frappe.enqueue(
        "fbr_e_invoicing.api.pra_queue._process_single_queue_item",
        queue="short",
        queue_item_name=queue_item_name,
        enqueue_after_commit=True,
        job_id=f"pra_queue_item::{queue_item_name}",
        deduplicate=True,
    )


def _claim_and_start_queue_item(queue_item_name):
    claimed_name = frappe.db.get_value(
        "PRA Queue",
        {"name": queue_item_name, "status": "Pending"},
        "name",
        for_update=True,
        skip_locked=True,
    )
    if not claimed_name:
        return False

    frappe.db.set_value(
        "PRA Queue",
        claimed_name,
        {
            "status": "Processing",
            "next_retry_at": None,
        },
    )
    _enqueue_queue_item(claimed_name)
    return True


def _kick_queue_once():
    try:
        process_queue(limit=1)
    except Exception as e:
        frappe.log_error(
            f"Error triggering queue kick: {str(e)}",
            "PRA Queue Kick",
        )


def _is_already_submitted(doctype, docname):
    doc_pra_status, doc_pra_invoice_number = frappe.db.get_value(
        doctype,
        docname,
        ["custom_pra_status", "custom_pra_invoice_number"],
    )
    return bool(
        str(doc_pra_status or "").strip().lower() == "success"
        or str(doc_pra_invoice_number or "").strip()
    )


def recover_stuck_and_retryable_items():
    """Recover stale Processing rows and retry due Failed rows."""
    now_dt = now_datetime()
    stuck_cutoff = add_to_date(now_dt, minutes=-STUCK_PROCESSING_TIMEOUT_MINUTES)

    stuck_recovered = 0
    failed_recovered = 0

    stuck_items = frappe.get_all(
        "PRA Queue", filters={"status": "Processing", "modified": ["<=", stuck_cutoff]}
    )
    for item in stuck_items:
        try:
            doc = frappe.get_doc("PRA Queue", item.name)
            _mark_queue_retry_or_fail(
                doc,
                f"Recovered stuck processing item after {STUCK_PROCESSING_TIMEOUT_MINUTES} minutes",
                retryable=True,
                increment_attempt=True,
            )
            stuck_recovered += 1
        except Exception as e:
            frappe.log_error(
                f"Error recovering stuck PRA Queue item {item.name}: {str(e)}"
            )

    failed_items = frappe.get_all(
        "PRA Queue",
        filters={"status": "Failed", "next_retry_at": ["<=", now_dt]},
    )
    for item in failed_items:
        try:
            doc = frappe.get_doc("PRA Queue", item.name)
            if _has_retry_left(doc):
                doc.status = "Pending"
                doc.next_retry_at = now_dt
                _sync_retry_fields(doc)
                doc.save(ignore_permissions=True)
                failed_recovered += 1
            else:
                _remove_terminal_queue_item(doc)
        except Exception as e:
            frappe.log_error(
                f"Error recovering failed PRA Queue item {item.name}: {str(e)}"
            )

    terminal_failed_items = frappe.get_all(
        "PRA Queue",
        filters={"status": "Failed", "retry_count": [">=", DEFAULT_MAX_RETRIES]},
    )
    for item in terminal_failed_items:
        try:
            doc = frappe.get_doc("PRA Queue", item.name)
            _remove_terminal_queue_item(doc)
        except Exception as e:
            frappe.log_error(
                f"Error removing terminal failed PRA Queue item {item.name}: {str(e)}"
            )

    return {"stuck_recovered": stuck_recovered, "failed_recovered": failed_recovered}


@frappe.whitelist()
def add_to_queue(
    doctype: str,
    docname: str,
    status: str = "Pending",
    error_message: str = "",
    force_immediate: bool = False,
    append_to_back: bool = False,
):
    """Add a document to the PRA queue with strict duplicate prevention."""
    try:
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

        if _is_already_submitted(doctype, docname):
            frappe.throw(
                f"{doctype} '{docname}' has already been successfully submitted to PRA."
            )

        existing_queue_id = frappe.db.exists(
            "PRA Queue", {"document_type": doctype, "document_name": docname}
        )

        normalized_status = _normalize_queue_status(status)

        if existing_queue_id:
            queue_doc = frappe.get_doc("PRA Queue", existing_queue_id)
            changed = False
            previous_status = queue_doc.status

            if queue_doc.status == "Failed":
                queue_doc.status = "Pending"
                queue_doc.next_retry_at = now_datetime()
                queue_doc.error_message = ""
                if append_to_back:
                    queue_doc.created_at = now()
                changed = True

            elif queue_doc.status == "Pending":
                if force_immediate:
                    queue_doc.next_retry_at = now_datetime()
                    changed = True
                if append_to_back:
                    queue_doc.created_at = now()
                    changed = True

            _sync_retry_fields(queue_doc)

            if changed:
                queue_doc.save(ignore_permissions=True)

            if previous_status == "Processing":
                state = "already_processing"
            elif previous_status == "Failed":
                state = "requeued"
            else:
                state = "already_queued"

            return {
                "success": True,
                "queue_id": queue_doc.name,
                "state": state,
            }

        queue_doc = frappe.new_doc("PRA Queue")
        queue_doc.document_type = doctype
        queue_doc.document_name = docname
        queue_doc.status = normalized_status
        queue_doc.error_message = error_message
        queue_doc.retry_count = 0
        queue_doc.created_at = now()
        queue_doc.next_retry_at = (
            now_datetime() if normalized_status == "Pending" else None
        )
        _sync_retry_fields(queue_doc)

        queue_doc.insert(ignore_permissions=True)

        if force_immediate and queue_doc.status == "Pending":
            queue_doc.next_retry_at = now_datetime()
            if append_to_back:
                queue_doc.created_at = now()
            queue_doc.save(ignore_permissions=True)

        return {"success": True, "queue_id": queue_doc.name, "state": "queued"}

    except Exception as e:
        frappe.log_error(
            f"Error adding {doctype} {docname} to PRA queue: {str(e)}", "PRA Queue"
        )
        if getattr(e, "http_status_code", None) == 417 or isinstance(
            e, frappe.ValidationError
        ):
            raise e
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def post_invoice_to_pra(doctype: str, docname: str):
    """Manual/automatic Post to PRA endpoint with deterministic queue state transitions."""
    try:
        if doctype not in SUPPORTED_DOCUMENT_TYPES:
            frappe.throw(f"Unsupported doctype for PRA posting: {doctype}")

        if _is_already_submitted(doctype, docname):
            return {
                "success": True,
                "state": "already_submitted",
                "message": "Invoice is already submitted to PRA.",
            }

        existing_queue_id = frappe.db.exists(
            "PRA Queue", {"document_type": doctype, "document_name": docname}
        )

        if existing_queue_id:
            queue_doc = frappe.get_doc("PRA Queue", existing_queue_id)
            if queue_doc.status == "Processing":
                return {
                    "success": True,
                    "state": "already_processing",
                    "queue_id": queue_doc.name,
                    "message": "Invoice is already being processed by PRA.",
                }

            if queue_doc.status == "Failed":
                queue_doc.status = "Pending"
                queue_doc.next_retry_at = now_datetime()
                queue_doc.error_message = ""
                queue_doc.created_at = now()
                _sync_retry_fields(queue_doc)
                queue_doc.save(ignore_permissions=True)

                _kick_queue_once()
                queue_doc.reload()
                return {
                    "success": True,
                    "state": "moved_to_processing"
                    if queue_doc.status == "Processing"
                    else "moved_to_pending",
                    "queue_id": queue_doc.name,
                }

        backlog_exists = _has_status_row("Processing") or _has_status_row("Pending")

        queue_result = add_to_queue(
            doctype=doctype,
            docname=docname,
            status="Pending",
            force_immediate=True,
        )

        if not queue_result.get("success"):
            return {
                "success": False,
                "state": "error",
                "error": queue_result.get("error") or "Unable to queue invoice for PRA",
            }

        queue_doc = frappe.get_doc("PRA Queue", queue_result.get("queue_id"))

        if not backlog_exists:
            _claim_and_start_queue_item(queue_doc.name)

        _kick_queue_once()
        queue_doc.reload()

        return {
            "success": True,
            "state": "moved_to_processing"
            if queue_doc.status == "Processing"
            else "moved_to_pending",
            "queue_id": queue_doc.name,
        }

    except Exception as e:
        frappe.log_error(
            f"Error posting {doctype} {docname} to PRA: {str(e)}",
            "PRA Manual Post",
        )
        if getattr(e, "http_status_code", None) == 417 or isinstance(
            e, frappe.ValidationError
        ):
            raise e
        return {"success": False, "state": "error", "error": str(e)}


@frappe.whitelist()
def process_queue(limit: int | str = 50):
    """Process due pending items by enqueuing background jobs in strict order."""
    try:
        limit = max(1, cint(limit) or 1)
        enqueued_count = 0

        while enqueued_count < limit:
            if _has_status_row("Processing"):
                break

            queue_items = frappe.get_all(
                "PRA Queue",
                filters={"status": "Pending", "next_retry_at": ["<=", now_datetime()]},
                order_by="created_at ASC, name ASC",
                limit=1,
            )
            if not queue_items:
                break

            item_name = queue_items[0].name
            try:
                if _claim_and_start_queue_item(item_name):
                    enqueued_count += 1
                else:
                    continue
            except Exception as e:
                frappe.db.set_value(
                    "PRA Queue",
                    item_name,
                    {
                        "status": "Pending",
                        "next_retry_at": now_datetime(),
                        "error_message": f"Enqueue failed: {str(e)}",
                    },
                )
                break

        return {"enqueued_count": enqueued_count, "processed_count": enqueued_count}

    except Exception as e:
        frappe.log_error(f"Error processing PRA queue: {str(e)}", "PRA Queue")
        return {"enqueued_count": 0, "processed_count": 0, "error": str(e)}


def _process_single_queue_item(queue_item_name):
    """Process a single queue item in its own background job."""
    queue_entry = None

    try:
        if not frappe.db.exists("PRA Queue", queue_item_name):
            return

        queue_entry = frappe.get_doc("PRA Queue", queue_item_name)
        if queue_entry.status != "Processing":
            return

        previous_remaining_retries = (
            cint(queue_entry.remaining_retries)
            if queue_entry.meta.has_field("remaining_retries")
            else None
        )
        _sync_retry_fields(queue_entry)
        retry_fields_changed = False
        if queue_entry.meta.has_field("remaining_retries") and previous_remaining_retries is not None:
            retry_fields_changed = (
                cint(queue_entry.remaining_retries) != previous_remaining_retries
            )
        if retry_fields_changed:
            queue_entry.save(ignore_permissions=True)

        if not _has_retry_left(queue_entry):
            _remove_terminal_queue_item(queue_entry)
            return

        # Import locally to avoid circular dependencies
        from fbr_e_invoicing.api.pra_submission import (
            submit_single_pra_invoice,
            _persist_pra_response_fields,
            log_pra_submission,
        )

        retry_attempt = cint(queue_entry.retry_count) + 1
        docstatus = frappe.db.get_value(
            queue_entry.document_type, queue_entry.document_name, "docstatus"
        )
        if docstatus != 1:
            docstatus_reason = (
                "not found"
                if docstatus is None
                else (
                    "draft"
                    if docstatus == 0
                    else "cancelled" if docstatus == 2 else str(docstatus)
                )
            )
            error_message = (
                "Skipped PRA submission because document is no longer submitted "
                f"(docstatus={docstatus_reason})."
            )
            response = {"Code": "", "Response": error_message, "Errors": error_message}
            log_pra_submission(
                queue_entry.document_type,
                queue_entry.document_name,
                {},
                response,
                "Error",
                retry_attempt=retry_attempt,
            )
            _persist_pra_response_fields(
                queue_entry.document_type,
                queue_entry.document_name,
                response,
            )
            frappe.delete_doc("PRA Queue", queue_item_name, ignore_permissions=True)
            return

        submission_result = submit_single_pra_invoice(
            queue_entry.document_type,
            queue_entry.document_name,
            is_retry=True,
            retry_attempt=retry_attempt,
        )

        response = submission_result.get("response")
        if not isinstance(response, dict) or not response:
            response = {
                "Code": "",
                "Response": submission_result.get("error")
                or submission_result.get("message")
                or "PRA submission failed",
            }

        _persist_pra_response_fields(
            queue_entry.document_type,
            queue_entry.document_name,
            response,
        )

        if (
            submission_result.get("success")
            or submission_result.get("status") == "already_submitted"
        ):
            frappe.delete_doc("PRA Queue", queue_item_name, ignore_permissions=True)
            return

        error_message = _build_detailed_error_message(
            response, submission_result.get("message") or "PRA submission failed"
        )

        if submission_result.get("retryable", True):
            _mark_queue_retry_or_fail(
                queue_entry,
                error_message,
                response=response,
                retryable=True,
                increment_attempt=True,
            )
            if not _has_retry_left(queue_entry):
                _remove_terminal_queue_item(queue_entry)
                return
        else:
            failure_type = str(submission_result.get("failure_type") or "").strip().lower()
            if failure_type in NON_RETRYABLE_INVALID_FAILURE_TYPES:
                frappe.delete_doc("PRA Queue", queue_item_name, ignore_permissions=True)
                return
            _mark_queue_terminal_failure(
                queue_entry,
                error_message,
                response=response,
                increment_attempt=True,
            )

    except Exception as e:
        frappe.log_error(
            f"Error executing queue item {queue_item_name}: {str(e)}",
            "PRA Queue Processing",
        )

        if not queue_entry and frappe.db.exists("PRA Queue", queue_item_name):
            queue_entry = frappe.get_doc("PRA Queue", queue_item_name)

        if queue_entry:
            response = {"Code": "", "Response": f"Exception: {str(e)}"}
            try:
                from fbr_e_invoicing.api.pra_submission import (
                    _persist_pra_response_fields,
                    log_pra_submission,
                )

                retry_attempt = cint(queue_entry.retry_count) + 1
                log_pra_submission(
                    queue_entry.document_type,
                    queue_entry.document_name,
                    {},
                    response,
                    "Error",
                    retry_attempt=retry_attempt,
                )

                _persist_pra_response_fields(
                    queue_entry.document_type,
                    queue_entry.document_name,
                    response,
                )
            except Exception as persist_error:
                frappe.log_error(
                    f"Error persisting exception response for queue item {queue_item_name}: {str(persist_error)}",
                    "PRA Queue Processing",
                )

            _mark_queue_retry_or_fail(
                queue_entry,
                f"Exception: {str(e)}",
                response=response,
                retryable=True,
                increment_attempt=True,
            )
            if not _has_retry_left(queue_entry):
                _remove_terminal_queue_item(queue_entry)
    finally:
        _kick_queue_once()


def cleanup_pra_queue_on_cancel(doc, method=None):
    """
    On successful cancellation, remove related retryable queue rows and log cancellation.

    Only Pending/Failed queue items are eligible for cleanup; Processing is
    intentionally left untouched.
    """
    try:
        queue_rows = frappe.get_all(
            "PRA Queue",
            filters={
                "document_type": doc.doctype,
                "document_name": doc.name,
                "status": ["in", ["Pending", "Failed"]],
            },
            fields=["name", "status"],
            order_by="creation asc",
            limit_page_length=0,
        )
        if not queue_rows:
            return

        deleted_rows = []
        for row in queue_rows:
            try:
                frappe.delete_doc("PRA Queue", row.name, ignore_permissions=True)
                deleted_rows.append({"queue_id": row.name, "status": row.status})
            except Exception as e:
                frappe.log_error(
                    f"Failed deleting PRA Queue row {row.name} during cancel of {doc.doctype} {doc.name}: {str(e)}",
                    "PRA Cancel Queue Cleanup",
                )

        if not deleted_rows:
            return

        log_doc = frappe.new_doc("PRA Logs")
        log_doc.update(
            {
                "document_type": doc.doctype,
                "document_name": doc.name,
                "status": "Cancelled",
                "submitted_at": now(),
                "response_data": json.dumps(
                    {
                        "message": "Invoice cancelled before PRA submission; removed related queue items.",
                        "removed_queue_items": deleted_rows,
                    },
                    indent=2,
                ),
            }
        )
        log_doc.insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(
            f"Error cleaning queue/logging cancellation for {doc.doctype} {doc.name}: {str(e)}",
            "PRA Cancel Queue Cleanup",
        )


@frappe.whitelist()
def get_document_queue_state(doctype: str, docname: str):
    """Get queue state for a specific document."""
    queue_row = frappe.db.get_value(
        "PRA Queue",
        {"document_type": doctype, "document_name": docname},
        ["name", "status", "retry_count", "next_retry_at"],
        as_dict=True,
    )

    return {
        "queue_exists": bool(queue_row),
        "queue": queue_row or {},
    }


@frappe.whitelist()
def delete_queue_items(queue_ids: list[str] | str):
    """Delete queue records (hard delete)."""
    if isinstance(queue_ids, str):
        try:
            queue_ids = json.loads(queue_ids)
        except Exception:
            queue_ids = [queue_ids]

    if not isinstance(queue_ids, list):
        frappe.throw(_("queue_ids must be a list or JSON array"))

    results = []
    deleted_count = 0

    for raw_id in queue_ids:
        queue_id = str(raw_id or "").strip()
        if not queue_id:
            continue

        if not frappe.db.exists("PRA Queue", queue_id):
            results.append({"queue_id": queue_id, "status": "not_found"})
            continue

        queue_doc = frappe.get_doc("PRA Queue", queue_id)
        if not frappe.has_permission("PRA Queue", ptype="delete", doc=queue_doc):
            results.append({"queue_id": queue_id, "status": "not_allowed"})
            continue

        if queue_doc.status == "Processing":
            results.append({"queue_id": queue_id, "status": "processing_locked"})
            continue

        try:
            frappe.delete_doc("PRA Queue", queue_id)
            deleted_count += 1
            results.append({"queue_id": queue_id, "status": "deleted"})
        except Exception as e:
            results.append(
                {"queue_id": queue_id, "status": "error", "error": str(e)}
            )

    return {
        "deleted_count": deleted_count,
        "results": results,
    }


@frappe.whitelist()
def get_queue_status():
    """Get current queue status."""
    try:
        status_counts = frappe.db.sql(
            """
            SELECT status, COUNT(*) as count
            FROM `tabPRA Queue`
            GROUP BY status
            """,
            as_dict=True,
        )

        failed_items = frappe.get_all(
            "PRA Queue",
            filters={"status": "Failed"},
            fields=[
                "document_type",
                "document_name",
                "error_message",
                "retry_count",
                "created_at",
                "next_retry_at",
            ],
            order_by="modified desc",
            limit=10,
        )

        return {"status_counts": status_counts, "failed_items": failed_items}

    except Exception as e:
        frappe.log_error(f"Error getting queue status: {str(e)}", "PRA Queue")
        return {"status_counts": [], "failed_items": []}


@frappe.whitelist()
def retry_failed_items():
    """Manually move retryable failed items back to pending and trigger processing."""
    try:
        failed_items = frappe.get_all("PRA Queue", filters={"status": "Failed"})
        retry_count = 0
        now_dt = now_datetime()

        for item in failed_items:
            doc = frappe.get_doc("PRA Queue", item.name)
            if _has_retry_left(doc):
                doc.status = "Pending"
                doc.next_retry_at = now_dt
                _sync_retry_fields(doc)
                doc.save(ignore_permissions=True)
                retry_count += 1

        _kick_queue_once()

        return {"retry_count": retry_count}
    except Exception as e:
        frappe.log_error(f"Error retrying failed items: {str(e)}", "PRA Queue")
        return {"retry_count": 0, "error": str(e)}


def process_pra_queue_scheduled():
    """Scheduled task to recover queue and process due pending items."""
    try:
        recovery = recover_stuck_and_retryable_items()
        result = process_queue(limit=1)

        if (
            recovery.get("stuck_recovered")
            or recovery.get("failed_recovered")
            or result.get("enqueued_count")
        ):
            frappe.log_error(
                f"Scheduled PRA queue processing: recovery={recovery}, queue={result}",
                "PRA Queue Scheduled",
            )
    except Exception as e:
        frappe.log_error(
            f"Error in scheduled PRA queue processing: {str(e)}", "PRA Queue Scheduled"
        )
