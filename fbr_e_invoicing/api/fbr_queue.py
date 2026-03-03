import json
from datetime import datetime

import frappe
from croniter import croniter
from frappe.utils import add_to_date, cint, now, now_datetime

DEFAULT_MAX_RETRIES = 5
DEFAULT_PRIORITY = 5
MIN_PRIORITY = 1
MAX_PRIORITY = 10
STUCK_PROCESSING_TIMEOUT_MINUTES = 30
VALID_QUEUE_STATUSES = {"Pending", "Processing", "Completed", "Failed"}
SCHEDULED_RETRY_METHOD = "fbr_e_invoicing.api.fbr_queue.process_fbr_queue_scheduled"
SUPPORTED_DOCUMENT_TYPES = {"Sales Invoice", "POS Invoice"}


def _get_effective_max_retries(max_retries):
    value = cint(max_retries)
    return value if value > 0 else DEFAULT_MAX_RETRIES


def _normalize_priority(priority):
    if priority is None or priority == "":
        return DEFAULT_PRIORITY
    value = cint(priority)
    return max(MIN_PRIORITY, min(value, MAX_PRIORITY))


def _normalize_queue_status(status):
    normalized_status = str(status or "").strip()
    return normalized_status if normalized_status in VALID_QUEUE_STATUSES else "Pending"


def _has_retry_left(queue_doc):
    return cint(queue_doc.retry_count) < _get_effective_max_retries(queue_doc.max_retries)


def _sync_retry_fields(queue_doc):
    effective_max_retries = _get_effective_max_retries(queue_doc.max_retries)
    queue_doc.max_retries = effective_max_retries
    if queue_doc.meta.has_field("remaining_retries"):
        queue_doc.remaining_retries = max(
            0, effective_max_retries - cint(queue_doc.retry_count)
        )
    return effective_max_retries


def _get_next_retry_at(reference_time=None):
    reference = reference_time or now_datetime()

    try:
        cron_format = frappe.db.get_value(
            "Scheduled Job Type",
            {"method": SCHEDULED_RETRY_METHOD, "stopped": 0},
            "cron_format",
        )
        if cron_format:
            next_execution = croniter(cron_format, reference).get_next(datetime)
            if next_execution and next_execution > reference:
                return next_execution
    except Exception as e:
        frappe.log_error(
            f"Unable to resolve scheduler-based retry time: {str(e)}",
            "FBR Queue Retry Schedule",
        )

    # Fallback to next scheduler tick eligibility when scheduler metadata isn't available.
    return reference


def _set_queue_response(queue_doc, response=None):
    if response and isinstance(response, dict) and queue_doc.meta.has_field("fbr_response"):
        queue_doc.fbr_response = json.dumps(response, indent=2)


def _ensure_retry_capacity(queue_doc, minimum_additional_attempts=1):
    current_max = _get_effective_max_retries(queue_doc.max_retries)
    required_max = max(current_max, cint(queue_doc.retry_count) + minimum_additional_attempts)
    if required_max != current_max:
        queue_doc.max_retries = required_max
        return True
    queue_doc.max_retries = current_max
    return False


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


def _has_status_row(status, exclude_name=None):
    filters = {"status": status}
    if exclude_name:
        filters["name"] = ["!=", exclude_name]
    return bool(frappe.db.exists("FBR Queue", filters))


def _enqueue_queue_item(queue_item_name):
    frappe.enqueue(
        "fbr_e_invoicing.api.fbr_queue._process_single_queue_item",
        queue="short",
        queue_item_name=queue_item_name,
        enqueue_after_commit=True,
        job_id=f"fbr_queue_item::{queue_item_name}",
        deduplicate=True,
    )


def _claim_and_start_queue_item(queue_item_name):
    claimed_name = frappe.db.get_value(
        "FBR Queue",
        {"name": queue_item_name, "status": "Pending"},
        "name",
        for_update=True,
        skip_locked=True,
    )
    if not claimed_name:
        return False

    frappe.db.set_value(
        "FBR Queue",
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
            "FBR Queue Kick",
        )


def _is_already_submitted(doctype, docname):
    doc_fbr_status, doc_fbr_invoice_number = frappe.db.get_value(
        doctype,
        docname,
        ["custom_fbr_status", "custom_fbr_invoice_number"],
    )
    return bool(
        str(doc_fbr_status or "").strip().lower() == "valid"
        or str(doc_fbr_invoice_number or "").strip()
    )


def recover_stuck_and_retryable_items():
    """Recover stale Processing rows and retry due Failed rows."""
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
                retryable=True,
                increment_attempt=True,
            )
            stuck_recovered += 1
        except Exception as e:
            frappe.log_error(
                f"Error recovering stuck FBR Queue item {item.name}: {str(e)}"
            )

    failed_items = frappe.get_all(
        "FBR Queue",
        filters={"status": "Failed", "next_retry_at": ["<=", now_dt]},
    )
    for item in failed_items:
        try:
            doc = frappe.get_doc("FBR Queue", item.name)
            if _has_retry_left(doc):
                doc.status = "Pending"
                doc.next_retry_at = now_dt
                _sync_retry_fields(doc)
                doc.save(ignore_permissions=True)
                failed_recovered += 1
            else:
                if doc.next_retry_at:
                    doc.next_retry_at = None
                    _sync_retry_fields(doc)
                    doc.save(ignore_permissions=True)
        except Exception as e:
            frappe.log_error(
                f"Error recovering failed FBR Queue item {item.name}: {str(e)}"
            )

    return {"stuck_recovered": stuck_recovered, "failed_recovered": failed_recovered}


@frappe.whitelist()
def add_to_queue(
    doctype: str,
    docname: str,
    status: str = "Pending",
    error_message: str = "",
    priority: int | str | None = None,
    max_retries: int | str | None = None,
    force_immediate: bool = False,
    append_to_back: bool = False,
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
        if _is_already_submitted(doctype, docname):
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
            changed = False
            previous_status = queue_doc.status

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

            if queue_doc.status == "Failed":
                _ensure_retry_capacity(queue_doc, minimum_additional_attempts=1)
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

        if force_immediate and queue_doc.status == "Pending":
            queue_doc.next_retry_at = now_datetime()
            if append_to_back:
                queue_doc.created_at = now()
            queue_doc.save(ignore_permissions=True)

        return {"success": True, "queue_id": queue_doc.name, "state": "queued"}

    except Exception as e:
        frappe.log_error(
            f"Error adding {doctype} {docname} to FBR queue: {str(e)}", "FBR Queue"
        )
        if getattr(e, "http_status_code", None) == 417 or isinstance(
            e, frappe.ValidationError
        ):
            raise e
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def post_invoice_to_fbr(doctype: str, docname: str):
    """Manual Post to FBR endpoint with deterministic queue state transitions."""
    try:
        if doctype not in SUPPORTED_DOCUMENT_TYPES:
            frappe.throw(f"Unsupported doctype for FBR posting: {doctype}")

        if _is_already_submitted(doctype, docname):
            return {
                "success": True,
                "state": "already_submitted",
                "message": "Invoice is already submitted to FBR.",
            }

        existing_queue_id = frappe.db.exists(
            "FBR Queue", {"document_type": doctype, "document_name": docname}
        )

        # Manual repost for Failed: bypass retry wait, but append behind existing pending queue.
        if existing_queue_id:
            queue_doc = frappe.get_doc("FBR Queue", existing_queue_id)
            if queue_doc.status == "Processing":
                return {
                    "success": True,
                    "state": "already_processing",
                    "queue_id": queue_doc.name,
                    "message": "Invoice is already being processed by FBR.",
                }

            if queue_doc.status == "Failed":
                _ensure_retry_capacity(queue_doc, minimum_additional_attempts=1)
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
                "error": queue_result.get("error") or "Unable to queue invoice for FBR",
            }

        queue_doc = frappe.get_doc("FBR Queue", queue_result.get("queue_id"))

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
            f"Error posting {doctype} {docname} to FBR: {str(e)}",
            "FBR Manual Post",
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
            # Single active queue item at a time for deterministic ordering.
            if _has_status_row("Processing"):
                break

            queue_items = frappe.get_all(
                "FBR Queue",
                filters={"status": "Pending", "next_retry_at": ["<=", now_datetime()]},
                order_by="priority DESC, created_at ASC",
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
                    "FBR Queue",
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
        frappe.log_error(f"Error processing FBR queue: {str(e)}", "FBR Queue")
        return {"enqueued_count": 0, "processed_count": 0, "error": str(e)}


def _process_single_queue_item(queue_item_name):
    """Process a single queue item in its own background job."""
    queue_entry = None

    try:
        if not frappe.db.exists("FBR Queue", queue_item_name):
            return

        queue_entry = frappe.get_doc("FBR Queue", queue_item_name)
        if queue_entry.status != "Processing":
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

        if not _has_retry_left(queue_entry):
            _mark_queue_terminal_failure(
                queue_entry,
                "Max retries exceeded",
                increment_attempt=False,
            )
            return

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
                else (
                    "draft"
                    if docstatus == 0
                    else "cancelled" if docstatus == 2 else str(docstatus)
                )
            )
            error_message = (
                "Skipped FBR submission because document is no longer submitted "
                f"(docstatus={docstatus_reason})."
            )
            response = {"validationResponse": {"status": "Error", "error": error_message}}
            log_fbr_submission(
                queue_entry.document_type,
                queue_entry.document_name,
                {},
                response,
                "Error",
                retry_attempt=retry_attempt,
            )
            _persist_fbr_response_fields(
                queue_entry.document_type,
                queue_entry.document_name,
                response,
            )
            frappe.delete_doc("FBR Queue", queue_item_name, ignore_permissions=True)
            return

        submission_result = submit_single_invoice(
            queue_entry.document_type,
            queue_entry.document_name,
            is_retry=True,
            retry_attempt=retry_attempt,
        )

        response = submission_result.get("response")
        if not isinstance(response, dict) or not response:
            response = {
                "validationResponse": {
                    "status": "Error",
                    "error": submission_result.get("error")
                    or submission_result.get("message")
                    or "FBR submission failed",
                }
            }

        _persist_fbr_response_fields(
            queue_entry.document_type,
            queue_entry.document_name,
            response,
        )

        if (
            submission_result.get("success")
            or submission_result.get("status") == "already_submitted"
        ):
            frappe.delete_doc("FBR Queue", queue_item_name, ignore_permissions=True)
            return

        error_message = _build_detailed_error_message(
            response, submission_result.get("message") or "FBR submission failed"
        )

        if submission_result.get("retryable", True):
            _mark_queue_retry_or_fail(
                queue_entry,
                error_message,
                response=response,
                retryable=True,
                increment_attempt=True,
            )
        else:
            _mark_queue_terminal_failure(
                queue_entry,
                error_message,
                response=response,
                increment_attempt=True,
            )

    except Exception as e:
        frappe.log_error(
            f"Error executing queue item {queue_item_name}: {str(e)}",
            "FBR Queue Processing",
        )

        if not queue_entry and frappe.db.exists("FBR Queue", queue_item_name):
            queue_entry = frappe.get_doc("FBR Queue", queue_item_name)

        if queue_entry:
            response = {
                "validationResponse": {
                    "status": "Error",
                    "error": f"Exception: {str(e)}",
                }
            }
            try:
                from fbr_e_invoicing.api.fbr_submission import _persist_fbr_response_fields

                _persist_fbr_response_fields(
                    queue_entry.document_type,
                    queue_entry.document_name,
                    response,
                )
            except Exception as persist_error:
                frappe.log_error(
                    f"Error persisting exception response for queue item {queue_item_name}: {str(persist_error)}",
                    "FBR Queue Processing",
                )

            _mark_queue_retry_or_fail(
                queue_entry,
                f"Exception: {str(e)}",
                response=response,
                retryable=True,
                increment_attempt=True,
            )
    finally:
        _kick_queue_once()


@frappe.whitelist()
def get_document_queue_state(doctype: str, docname: str):
    """Get queue state for a specific document."""
    queue_row = frappe.db.get_value(
        "FBR Queue",
        {"document_type": doctype, "document_name": docname},
        ["name", "status", "retry_count", "max_retries", "next_retry_at"],
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
        frappe.throw("queue_ids must be a list or JSON array")

    results = []
    deleted_count = 0

    for raw_id in queue_ids:
        queue_id = str(raw_id or "").strip()
        if not queue_id:
            continue

        if not frappe.db.exists("FBR Queue", queue_id):
            results.append({"queue_id": queue_id, "status": "not_found"})
            continue

        queue_doc = frappe.get_doc("FBR Queue", queue_id)
        if not frappe.has_permission("FBR Queue", ptype="delete", doc=queue_doc):
            results.append({"queue_id": queue_id, "status": "not_allowed"})
            continue

        if queue_doc.status == "Processing":
            results.append({"queue_id": queue_id, "status": "processing_locked"})
            continue

        try:
            frappe.delete_doc("FBR Queue", queue_id)
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
    """Manually move retryable failed items back to pending and trigger processing."""
    try:
        failed_items = frappe.get_all("FBR Queue", filters={"status": "Failed"})
        retry_count = 0
        now_dt = now_datetime()

        for item in failed_items:
            doc = frappe.get_doc("FBR Queue", item.name)
            if _has_retry_left(doc):
                doc.status = "Pending"
                doc.next_retry_at = now_dt
                _sync_retry_fields(doc)
                doc.save(ignore_permissions=True)
                retry_count += 1

        _kick_queue_once()

        return {"retry_count": retry_count}
    except Exception as e:
        frappe.log_error(f"Error retrying failed items: {str(e)}", "FBR Queue")
        return {"retry_count": 0, "error": str(e)}


def process_fbr_queue_scheduled():
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
                f"Scheduled FBR queue processing: recovery={recovery}, queue={result}",
                "FBR Queue Scheduled",
            )
    except Exception as e:
        frappe.log_error(
            f"Error in scheduled FBR queue processing: {str(e)}", "FBR Queue Scheduled"
        )
