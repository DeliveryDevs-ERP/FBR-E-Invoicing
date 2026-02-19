import json
from functools import lru_cache

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now, now_datetime


DEFAULT_MAX_RETRIES = 5
DEFAULT_PRIORITY = 5
MIN_PRIORITY = 1
MAX_PRIORITY = 10
DEFAULT_SCHEDULER_RETRY_INTERVAL_MINUTES = 2
SCHEDULED_QUEUE_METHOD = "fbr_e_invoicing.api.fbr_queue.process_fbr_queue_scheduled"
STUCK_PROCESSING_TIMEOUT_MINUTES = 30
VALID_QUEUE_STATUSES = {"Pending", "Processing", "Completed", "Failed"}


def _get_effective_max_retries(queue_entry):
    max_retries = cint(getattr(queue_entry, "max_retries", 0) or 0)
    return max_retries if max_retries > 0 else DEFAULT_MAX_RETRIES


def _normalize_priority(priority, fallback=DEFAULT_PRIORITY):
    if priority is None or priority == "":
        return fallback
    value = cint(priority)
    return max(MIN_PRIORITY, min(value, MAX_PRIORITY))


def _normalize_max_retries(max_retries, fallback=DEFAULT_MAX_RETRIES):
    if max_retries is None or max_retries == "":
        return fallback
    value = cint(max_retries)
    return value if value > 0 else fallback


def _parse_cron_minute_interval(cron_expr):
    parts = str(cron_expr or "").split()
    if len(parts) != 5:
        return None

    minute_part = parts[0].strip()
    if minute_part == "*":
        return 1

    if "/" in minute_part:
        prefix, step = minute_part.split("/", 1)
        if prefix in {"*", "0"} and step.isdigit():
            step_value = cint(step)
            return step_value if step_value > 0 else None

    if minute_part.isdigit():
        return 60

    return None


@lru_cache(maxsize=1)
def _get_scheduler_retry_interval_minutes():
    scheduler_events = frappe.get_hooks("scheduler_events") or {}
    cron_events = scheduler_events.get("cron") or {}
    intervals = []

    for cron_expr, methods in cron_events.items():
        method_list = methods if isinstance(methods, (list, tuple)) else [methods]
        if SCHEDULED_QUEUE_METHOD not in method_list:
            continue

        interval = _parse_cron_minute_interval(cron_expr)
        if interval:
            intervals.append(interval)

    return min(intervals) if intervals else DEFAULT_SCHEDULER_RETRY_INTERVAL_MINUTES


def _get_next_retry_at(reference_time=None):
    retry_minutes = _get_scheduler_retry_interval_minutes()
    return add_to_date(reference_time or now_datetime(), minutes=retry_minutes)


@lru_cache(maxsize=1)
def _get_queue_table_columns():
    return set(frappe.db.get_table_columns("FBR Queue") or [])


def _compute_remaining_retries(retry_count, max_retries):
    return max(_normalize_max_retries(max_retries) - max(cint(retry_count or 0), 0), 0)


def _with_remaining_retries(update_values, retry_count, max_retries):
    if "remaining_retries" in _get_queue_table_columns():
        update_values["remaining_retries"] = _compute_remaining_retries(
            retry_count, max_retries
        )
    return update_values


def _with_fbr_response(update_values, response):
    if "fbr_response" in _get_queue_table_columns():
        update_values["fbr_response"] = (
            json.dumps(response, indent=2)
            if isinstance(response, dict) and response
            else ""
        )
    return update_values


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


def _normalize_queue_status(status):
    normalized_status = str(status or "").strip()
    return normalized_status if normalized_status in VALID_QUEUE_STATUSES else "Pending"


def _is_due_for_retry(queue_entry, reference_time=None):
    next_retry_at = getattr(queue_entry, "next_retry_at", None)
    if not next_retry_at:
        return True

    check_time = reference_time or now_datetime()
    return get_datetime(next_retry_at) <= check_time


def _has_retry_left(queue_entry):
    retry_count = cint(getattr(queue_entry, "retry_count", 0) or 0)
    return retry_count < _get_effective_max_retries(queue_entry)


def _mark_queue_retry_or_fail(queue_entry, error_message, response=None):
    retry_count = cint(getattr(queue_entry, "retry_count", 0) or 0) + 1
    max_retries = _get_effective_max_retries(queue_entry)

    update_values = {
        "retry_count": retry_count,
        "last_retry_at": now(),
        "error_message": error_message,
    }

    if retry_count >= max_retries:
        update_values.update({"status": "Failed", "next_retry_at": None})
    else:
        update_values.update(
            {
                "status": "Pending",
                "next_retry_at": _get_next_retry_at(),
            }
        )

    _with_remaining_retries(update_values, retry_count, max_retries)
    _with_fbr_response(update_values, response)
    frappe.db.set_value("FBR Queue", queue_entry.name, update_values)
    return update_values


def _mark_queue_terminal_failure(queue_entry, error_message, response=None):
    max_retries = _get_effective_max_retries(queue_entry)
    update_values = {
        "status": "Failed",
        "retry_count": max_retries,
        "last_retry_at": now(),
        "next_retry_at": None,
        "error_message": error_message,
    }
    _with_remaining_retries(update_values, max_retries, max_retries)
    _with_fbr_response(update_values, response)
    frappe.db.set_value("FBR Queue", queue_entry.name, update_values)
    return update_values


def resolve_open_queue_items_for_document(
    doctype, docname, outcome="completed", error_message="", response=None
):
    """Resolve open queue rows for a document after manual submission."""
    open_rows = frappe.get_all(
        "FBR Queue",
        filters={
            "document_type": doctype,
            "document_name": docname,
            "status": ["in", ["Pending", "Processing"]],
        },
        fields=["name", "retry_count", "max_retries"],
        limit_page_length=0,
    )

    if not open_rows:
        return {"resolved_count": 0}

    resolved_count = 0
    normalized_outcome = (outcome or "completed").strip().lower()
    for row in open_rows:
        if normalized_outcome == "failed":
            queue_entry = frappe._dict(
                {
                    "name": row.name,
                    "max_retries": row.max_retries,
                }
            )
            _mark_queue_terminal_failure(
                queue_entry,
                error_message or "Resolved as terminal failure by manual submission",
                response=response,
            )
        else:
            retry_count = cint(row.retry_count or 0)
            max_retries = _normalize_max_retries(
                row.max_retries, fallback=DEFAULT_MAX_RETRIES
            )
            update_values = {
                "status": "Completed",
                "completed_at": now(),
                "error_message": "",
                "next_retry_at": None,
            }
            _with_remaining_retries(update_values, retry_count, max_retries)
            _with_fbr_response(update_values, response)
            frappe.db.set_value("FBR Queue", row.name, update_values)

        resolved_count += 1

    return {"resolved_count": resolved_count}


def _get_due_pending_queue_items(limit=50):
    limit = cint(limit or 50)
    if limit <= 0:
        limit = 50

    return frappe.db.sql(
        """
        SELECT name
        FROM `tabFBR Queue`
        WHERE status = 'Pending'
          AND (next_retry_at IS NULL OR next_retry_at <= %s)
          AND retry_count < IFNULL(NULLIF(max_retries, 0), %s)
        ORDER BY priority DESC, created_at ASC
        LIMIT %s
        """,
        (now_datetime(), DEFAULT_MAX_RETRIES, limit),
        as_dict=True,
    )


def _mark_queue_item_failed_if_exhausted(queue_item_name):
    """Mark a pending queue item as failed if retries are exhausted."""
    queue_entry = frappe.db.get_value(
        "FBR Queue",
        queue_item_name,
        ["status", "retry_count", "max_retries"],
        as_dict=True,
    )
    if not queue_entry or queue_entry.status != "Pending":
        return False

    max_retries = (
        cint(queue_entry.max_retries) if cint(queue_entry.max_retries) > 0 else DEFAULT_MAX_RETRIES
    )
    if cint(queue_entry.retry_count) < max_retries:
        return False

    modified_by = frappe.session.user or "Administrator"
    frappe.db.sql(
        """
        UPDATE `tabFBR Queue`
        SET
            status = 'Failed',
            error_message = %s,
            next_retry_at = NULL,
            modified = %s,
            modified_by = %s
        WHERE name = %s
          AND status = 'Pending'
          AND retry_count >= COALESCE(NULLIF(max_retries, 0), %s)
        """,
        (
            "Max retries exceeded",
            now(),
            modified_by,
            queue_item_name,
            DEFAULT_MAX_RETRIES,
        ),
    )
    failed = frappe.db.get_value("FBR Queue", queue_item_name, "status") == "Failed"
    if failed and "remaining_retries" in _get_queue_table_columns():
        frappe.db.set_value(
            "FBR Queue", queue_item_name, "remaining_retries", 0, update_modified=False
        )
    return failed


def _claim_queue_item_for_processing(queue_item_name):
    """Atomically claim a queue item by transitioning Pending -> Processing."""
    current_time = now_datetime()
    modified_by = frappe.session.user or "Administrator"
    claimed_at = now()
    frappe.db.sql(
        """
        UPDATE `tabFBR Queue`
        SET
            status = 'Processing',
            modified = %s,
            modified_by = %s
        WHERE name = %s
          AND status = 'Pending'
          AND (next_retry_at IS NULL OR next_retry_at <= %s)
          AND retry_count < COALESCE(NULLIF(max_retries, 0), %s)
        """,
        (claimed_at, modified_by, queue_item_name, current_time, DEFAULT_MAX_RETRIES),
    )
    claimed_row = frappe.db.get_value(
        "FBR Queue",
        queue_item_name,
        ["status", "modified", "modified_by"],
        as_dict=True,
    )
    if not claimed_row or claimed_row.status != "Processing":
        return False

    return (
        claimed_row.modified_by == modified_by
        and get_datetime(claimed_row.modified) == get_datetime(claimed_at)
    )


def recover_stuck_and_retryable_items():
    """Recover stale Processing rows and retryable Failed rows."""
    now_dt = now_datetime()
    stuck_cutoff = add_to_date(now_dt, minutes=-STUCK_PROCESSING_TIMEOUT_MINUTES)

    stuck_recovered = 0
    failed_recovered = 0
    exhausted_count = 0

    stuck_items = frappe.get_all(
        "FBR Queue",
        filters={"status": "Processing", "modified": ["<=", stuck_cutoff]},
        fields=["name", "retry_count", "max_retries"],
        limit_page_length=1000,
    )

    for item in stuck_items:
        try:
            _mark_queue_retry_or_fail(
                item,
                f"Recovered stuck processing item after {STUCK_PROCESSING_TIMEOUT_MINUTES} minutes",
            )
            stuck_recovered += 1
        except Exception as e:
            frappe.log_error(
                f"Error recovering stuck queue item {item.name}: {str(e)}",
                "FBR Queue Recovery",
            )

    failed_items = frappe.get_all(
        "FBR Queue",
        filters={"status": "Failed"},
        fields=["name", "retry_count", "max_retries"],
        limit_page_length=1000,
    )

    for item in failed_items:
        if _has_retry_left(item):
            max_retries = _get_effective_max_retries(item)
            retry_count = cint(getattr(item, "retry_count", 0) or 0)
            update_values = {
                "status": "Pending",
                "error_message": "",
                "next_retry_at": _get_next_retry_at(now_dt),
            }
            _with_remaining_retries(update_values, retry_count, max_retries)
            frappe.db.set_value(
                "FBR Queue",
                item.name,
                update_values,
            )
            failed_recovered += 1
        else:
            exhausted_count += 1

    return {
        "stuck_recovered": stuck_recovered,
        "failed_recovered": failed_recovered,
        "exhausted_count": exhausted_count,
    }


@frappe.whitelist()
def add_to_queue(
    doctype, docname, status="Pending", error_message="", priority=None, max_retries=None
):
    """Add a document to the FBR queue."""
    try:
        normalized_status = _normalize_queue_status(status)

        existing = frappe.db.exists(
            "FBR Queue",
            {
                "document_type": doctype,
                "document_name": docname,
                "status": ["in", ["Pending", "Processing"]],
            },
        )

        if existing:
            queue_doc = frappe.get_doc("FBR Queue", existing)
            if queue_doc.status == "Processing":
                return {
                    "success": True,
                    "queue_id": queue_doc.name,
                    "state": "already_processing",
                }

            queue_update = {"error_message": error_message}
            effective_max_retries = _get_effective_max_retries(queue_doc)

            if priority is not None:
                queue_update["priority"] = _normalize_priority(
                    priority, fallback=_normalize_priority(queue_doc.priority, DEFAULT_PRIORITY)
                )

            if max_retries is not None:
                effective_max_retries = _normalize_max_retries(
                    max_retries, fallback=effective_max_retries
                )
                queue_update["max_retries"] = effective_max_retries

            if normalized_status == "Pending":
                queue_update.update({"status": "Pending", "next_retry_at": now_datetime()})
            else:
                queue_update.update({"status": normalized_status})

            retry_count = cint(queue_doc.retry_count or 0)
            _with_remaining_retries(queue_update, retry_count, effective_max_retries)
            _with_fbr_response(queue_update, None)

            frappe.db.set_value("FBR Queue", queue_doc.name, queue_update)
            queue_doc.reload()
            queue_state = "already_pending" if queue_doc.status == "Pending" else "updated"
        else:
            effective_priority = _normalize_priority(priority, fallback=DEFAULT_PRIORITY)
            effective_max_retries = _normalize_max_retries(
                max_retries, fallback=DEFAULT_MAX_RETRIES
            )
            queue_doc = frappe.new_doc("FBR Queue")
            queue_values = {
                "document_type": doctype,
                "document_name": docname,
                "status": normalized_status,
                "priority": effective_priority,
                "max_retries": effective_max_retries,
                "error_message": error_message,
                "retry_count": 0,
                "created_at": now(),
                "next_retry_at": now_datetime() if normalized_status == "Pending" else None,
            }
            _with_remaining_retries(queue_values, 0, effective_max_retries)
            _with_fbr_response(queue_values, None)
            queue_doc.update(
                queue_values
            )
            queue_doc.insert(ignore_permissions=True)
            queue_state = "queued"

        return {"success": True, "queue_id": queue_doc.name, "state": queue_state}

    except Exception as e:
        frappe.log_error(f"Error adding to FBR queue: {str(e)}", "FBR Queue")
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def process_queue(limit=50):
    """Process due pending items by enqueuing each as a background job."""
    try:
        queue_items = _get_due_pending_queue_items(limit=limit)

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
                    {"status": "Pending", "error_message": f"Enqueue failed: {str(e)}"},
                )
                frappe.log_error(
                    f"Error enqueueing queue item {item.name}: {str(e)}",
                    "FBR Queue Enqueue",
                )

        frappe.enqueue(
            "fbr_e_invoicing.api.fbr_queue.cleanup_old_queue_items",
            queue="short",
            enqueue_after_commit=True,
            job_id="fbr_queue_cleanup_old_items",
            deduplicate=True,
        )

        return {"enqueued_count": enqueued_count, "processed_count": enqueued_count}

    except Exception as e:
        frappe.log_error(f"Error processing FBR queue: {str(e)}", "FBR Queue")
        return {"enqueued_count": 0, "processed_count": 0, "error": str(e)}


def _process_single_queue_item(queue_item_name):
    """Process a single queue item in its own background job (auto-committed)."""
    if _mark_queue_item_failed_if_exhausted(queue_item_name):
        return

    if not _claim_queue_item_for_processing(queue_item_name):
        return

    if not frappe.db.exists("FBR Queue", queue_item_name):
        return

    queue_entry = frappe.get_doc("FBR Queue", queue_item_name)
    if queue_entry.status != "Processing":
        return

    try:
        result = process_queue_item(queue_entry)

        if result["success"]:
            queue_max_retries = _get_effective_max_retries(queue_entry)
            queue_retry_count = cint(getattr(queue_entry, "retry_count", 0) or 0)
            completed_update = {
                "status": "Completed",
                "completed_at": now(),
                "error_message": "",
                "next_retry_at": None,
            }
            _with_remaining_retries(completed_update, queue_retry_count, queue_max_retries)
            _with_fbr_response(completed_update, result.get("response"))
            frappe.db.set_value(
                "FBR Queue",
                queue_item_name,
                completed_update,
            )
        else:
            error_message = result.get("error", "Unknown error")
            if result.get("retryable", True):
                _mark_queue_retry_or_fail(queue_entry, error_message, result.get("response"))
            else:
                _mark_queue_terminal_failure(queue_entry, error_message, result.get("response"))

    except Exception as e:
        _mark_queue_retry_or_fail(queue_entry, str(e))
        frappe.log_error(
            f"Error processing queue item {queue_item_name}: {str(e)}",
            "FBR Queue Processing",
        )


def process_queue_item(queue_item):
    """Process a single queue item."""
    try:
        from fbr_e_invoicing.api.fbr_submission import (
            _persist_fbr_response_fields,
            submit_single_invoice,
        )

        submission_result = submit_single_invoice(
            queue_item.document_type, queue_item.document_name, is_retry=True
        )
        response = submission_result.get("response") or {}
        if isinstance(response, dict) and response:
            _persist_fbr_response_fields(
                queue_item.document_type,
                queue_item.document_name,
                response,
            )

        if submission_result.get("status") == "already_submitted":
            return {
                "success": True,
                "response": response if isinstance(response, dict) else {},
            }

        if not submission_result.get("success"):
            detailed_error = _build_detailed_error_message(
                response, submission_result.get("message") or "FBR submission failed"
            )
            return {
                "success": False,
                "error": detailed_error,
                "retryable": bool(submission_result.get("retryable")),
                "failure_type": submission_result.get("failure_type") or "submission_error",
                "response": response if isinstance(response, dict) else {},
            }

        status = response.get("validationResponse", {}).get("status", "")
        if status == "Valid":
            return {"success": True, "response": response}

        detailed_error = _build_detailed_error_message(
            response, f"FBR validation failed: {status or 'Invalid'}"
        )
        return {
            "success": False,
            "error": detailed_error,
            "retryable": False,
            "failure_type": "business_invalid",
            "response": response,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "retryable": True,
            "failure_type": "processing_error",
            "response": {},
        }


@frappe.whitelist()
def get_queue_status():
    """Get current queue status."""
    try:
        status_counts = frappe.db.sql(
            """
            SELECT
                status,
                COUNT(*) as count
            FROM `tabFBR Queue`
            GROUP BY status
            """,
            as_dict=True,
        )

        failed_item_fields = [
            "document_type",
            "document_name",
            "error_message",
            "retry_count",
            "created_at",
            "max_retries",
            "next_retry_at",
        ]
        if "remaining_retries" in _get_queue_table_columns():
            failed_item_fields.append("remaining_retries")

        failed_items = frappe.get_all(
            "FBR Queue",
            filters={"status": "Failed"},
            fields=failed_item_fields,
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
        failed_items = frappe.get_all(
            "FBR Queue",
            filters={"status": "Failed"},
            fields=["name", "retry_count", "max_retries"],
            limit_page_length=1000,
        )

        retryable_names = [item.name for item in failed_items if _has_retry_left(item)]
        retry_at = _get_next_retry_at()

        for name in retryable_names:
            item = next((row for row in failed_items if row.name == name), None)
            max_retries = _get_effective_max_retries(item) if item else DEFAULT_MAX_RETRIES
            retry_count = cint(getattr(item, "retry_count", 0) or 0) if item else 0
            update_values = {
                "status": "Pending",
                "error_message": "",
                "next_retry_at": retry_at,
            }
            _with_remaining_retries(update_values, retry_count, max_retries)
            _with_fbr_response(update_values, None)
            frappe.db.set_value(
                "FBR Queue",
                name,
                update_values,
            )

        return {"retry_count": len(retryable_names)}

    except Exception as e:
        frappe.log_error(f"Error retrying failed items: {str(e)}", "FBR Queue")
        return {"retry_count": 0, "error": str(e)}


def cleanup_old_queue_items():
    """Clean up old completed queue items."""
    try:
        cutoff_date = add_to_date(None, days=-30)

        frappe.db.sql(
            """
            DELETE FROM `tabFBR Queue`
            WHERE status = 'Completed' AND completed_at < %s
            """,
            cutoff_date,
        )

    except Exception as e:
        frappe.log_error(f"Error cleaning up queue: {str(e)}", "FBR Queue Cleanup")


def process_fbr_queue_scheduled():
    """Scheduled task to recover queue and process due pending items."""
    try:
        recovery = recover_stuck_and_retryable_items()

        pending_count = frappe.db.count("FBR Queue", {"status": "Pending"})
        result = {"enqueued_count": 0, "processed_count": 0}

        if pending_count > 0:
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
