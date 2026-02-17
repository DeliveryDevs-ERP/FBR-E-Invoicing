import frappe
import json
from frappe.utils import add_to_date, cint, get_datetime, now, now_datetime


DEFAULT_MAX_RETRIES = 5
MAX_RETRY_DELAY_MINUTES = 60
STUCK_PROCESSING_TIMEOUT_MINUTES = 30


def _get_effective_max_retries(queue_entry):
    max_retries = cint(getattr(queue_entry, "max_retries", 0) or 0)
    return max_retries if max_retries > 0 else DEFAULT_MAX_RETRIES


def _get_retry_delay_minutes(retry_count):
    safe_retry_count = max(cint(retry_count or 0), 1)
    return min(2 ** (safe_retry_count - 1), MAX_RETRY_DELAY_MINUTES)


def _is_due_for_retry(queue_entry, reference_time=None):
    next_retry_at = getattr(queue_entry, "next_retry_at", None)
    if not next_retry_at:
        return True

    check_time = reference_time or now_datetime()
    return get_datetime(next_retry_at) <= check_time


def _has_retry_left(queue_entry):
    retry_count = cint(getattr(queue_entry, "retry_count", 0) or 0)
    return retry_count < _get_effective_max_retries(queue_entry)


def _mark_queue_retry_or_fail(queue_entry, error_message):
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
        delay_minutes = _get_retry_delay_minutes(retry_count)
        update_values.update(
            {
                "status": "Pending",
                "next_retry_at": add_to_date(now_datetime(), minutes=delay_minutes),
            }
        )

    frappe.db.set_value("FBR Queue", queue_entry.name, update_values)
    return update_values


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
            frappe.db.set_value(
                "FBR Queue",
                item.name,
                {"status": "Pending", "error_message": "", "next_retry_at": now_dt},
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
def add_to_queue(doctype, docname, status="Pending", error_message="", priority=5):
    """Add a document to the FBR queue."""
    try:
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
            queue_doc.status = status
            queue_doc.error_message = error_message
            queue_doc.priority = priority
            if status == "Pending":
                queue_doc.next_retry_at = now_datetime()
            queue_doc.save(ignore_permissions=True)
        else:
            queue_doc = frappe.new_doc("FBR Queue")
            queue_doc.update(
                {
                    "document_type": doctype,
                    "document_name": docname,
                    "status": status,
                    "priority": priority,
                    "error_message": error_message,
                    "retry_count": 0,
                    "created_at": now(),
                    "next_retry_at": now_datetime() if status == "Pending" else None,
                }
            )
            queue_doc.insert(ignore_permissions=True)

        return {"success": True, "queue_id": queue_doc.name}

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
    if not frappe.db.exists("FBR Queue", queue_item_name):
        return

    queue_entry = frappe.get_doc("FBR Queue", queue_item_name)
    if queue_entry.status != "Pending":
        return

    if not _has_retry_left(queue_entry):
        frappe.db.set_value(
            "FBR Queue",
            queue_item_name,
            {
                "status": "Failed",
                "error_message": "Max retries exceeded",
                "next_retry_at": None,
            },
        )
        return

    if not _is_due_for_retry(queue_entry):
        return

    try:
        frappe.db.set_value("FBR Queue", queue_item_name, "status", "Processing")
        queue_entry.reload()
        result = process_queue_item(queue_entry)

        if result["success"]:
            frappe.db.set_value(
                "FBR Queue",
                queue_item_name,
                {
                    "status": "Completed",
                    "completed_at": now(),
                    "error_message": "",
                    "next_retry_at": None,
                },
            )
        else:
            _mark_queue_retry_or_fail(
                queue_entry, result.get("error", "Unknown error")
            )

    except Exception as e:
        _mark_queue_retry_or_fail(queue_entry, str(e))
        frappe.log_error(
            f"Error processing queue item {queue_item_name}: {str(e)}",
            "FBR Queue Processing",
        )


def process_queue_item(queue_item):
    """Process a single queue item."""
    try:
        from fbr_e_invoicing.api.fbr_submission import submit_single_invoice

        response = submit_single_invoice(
            queue_item.document_type, queue_item.document_name, is_retry=True
        )

        doc = frappe.get_lazy_doc(queue_item.document_type, queue_item.document_name)

        if queue_item.document_type == "Sales Invoice":
            doc.custom_fbr_response = json.dumps(response, indent=2)
            doc.custom_fbr_invoice_number = response.get("invoiceNumber", "")
            doc.custom_fbr_datetime = response.get("dated", "")
            doc.custom_fbr_status = response.get("validationResponse", {}).get(
                "status", ""
            )
        else:  # POS Invoice
            doc.custom_fbr_response = json.dumps(response, indent=2)
            doc.custom_fbr_invoice_number = response.get("invoiceNumber", "")
            doc.custom_fbr_datetime = response.get("dated", "")
            doc.custom_fbr_status = response.get("validationResponse", {}).get(
                "status", ""
            )

        doc.save(ignore_permissions=True)

        status = response.get("validationResponse", {}).get("status", "")
        if status == "Valid":
            return {"success": True}

        return {"success": False, "error": f"FBR validation failed: {status}"}

    except Exception as e:
        return {"success": False, "error": str(e)}


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
        failed_items = frappe.get_all(
            "FBR Queue",
            filters={"status": "Failed"},
            fields=["name", "retry_count", "max_retries"],
            limit_page_length=1000,
        )

        retryable_names = [item.name for item in failed_items if _has_retry_left(item)]
        retry_at = now_datetime()

        for name in retryable_names:
            frappe.db.set_value(
                "FBR Queue",
                name,
                {"status": "Pending", "error_message": "", "next_retry_at": retry_at},
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
