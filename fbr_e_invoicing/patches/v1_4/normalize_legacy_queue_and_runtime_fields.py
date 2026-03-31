import frappe
from frappe.utils import add_to_date, cint, now_datetime

from fbr_e_invoicing.api.fbr_queue import (
    DEFAULT_MAX_RETRIES,
    STUCK_PROCESSING_TIMEOUT_MINUTES,
)


VALID_QUEUE_STATUSES = {"Pending", "Processing", "Completed", "Failed"}
SUPPORTED_INVOICE_DOCTYPES = {"Sales Invoice", "POS Invoice"}


def execute():
    _backfill_response_typo("Sales Invoice")
    _backfill_response_typo("POS Invoice")
    _backfill_submit_to_fbr_default("Sales Invoice")
    _backfill_submit_to_fbr_default("POS Invoice")
    _normalize_queue_rows()

    frappe.clear_cache(doctype="Sales Invoice")
    frappe.clear_cache(doctype="POS Invoice")
    frappe.clear_cache(doctype="FBR Queue")


def _backfill_response_typo(doctype):
    if doctype not in SUPPORTED_INVOICE_DOCTYPES:
        return

    columns = set(frappe.db.get_table_columns(doctype) or [])
    if "custom_fbr_response" not in columns or "custom_fbr_responce" not in columns:
        return

    if doctype == "Sales Invoice":
        frappe.db.sql(
            """
            UPDATE `tabSales Invoice`
            SET custom_fbr_response = CASE
                WHEN IFNULL(custom_fbr_response, '') = '' THEN custom_fbr_responce
                ELSE custom_fbr_response
            END
            WHERE IFNULL(custom_fbr_responce, '') != ''
            """
        )
    elif doctype == "POS Invoice":
        frappe.db.sql(
            """
            UPDATE `tabPOS Invoice`
            SET custom_fbr_response = CASE
                WHEN IFNULL(custom_fbr_response, '') = '' THEN custom_fbr_responce
                ELSE custom_fbr_response
            END
            WHERE IFNULL(custom_fbr_responce, '') != ''
            """
        )


def _backfill_submit_to_fbr_default(doctype):
    if doctype not in SUPPORTED_INVOICE_DOCTYPES:
        return

    columns = set(frappe.db.get_table_columns(doctype) or [])
    if "custom_submit_to_fbr" not in columns:
        return

    if doctype == "Sales Invoice":
        frappe.db.sql(
            """
            UPDATE `tabSales Invoice`
            SET custom_submit_to_fbr = 1
            WHERE custom_submit_to_fbr IS NULL
            """
        )
    elif doctype == "POS Invoice":
        frappe.db.sql(
            """
            UPDATE `tabPOS Invoice`
            SET custom_submit_to_fbr = 1
            WHERE custom_submit_to_fbr IS NULL
            """
        )


def _normalize_queue_rows():
    if not frappe.db.exists("DocType", "FBR Queue"):
        return

    columns = set(frappe.db.get_table_columns("FBR Queue") or [])
    if not columns:
        return

    if "retry_count" in columns:
        frappe.db.sql(
            """
            UPDATE `tabFBR Queue`
            SET retry_count = 0
            WHERE retry_count IS NULL
            """
        )

    if "status" in columns:
        frappe.db.sql(
            """
            UPDATE `tabFBR Queue`
            SET status = CASE
                WHEN status IS NULL OR TRIM(status) = '' THEN 'Pending'
                WHEN status IN ('Queued', 'In Queue') THEN 'Pending'
                WHEN status = 'Error' THEN 'Failed'
                ELSE status
            END
            WHERE status IS NULL
                OR TRIM(status) = ''
                OR status IN ('Queued', 'In Queue', 'Error')
            """
        )

        invalid_statuses = frappe.get_all(
            "FBR Queue",
            filters={"status": ["not in", list(VALID_QUEUE_STATUSES)]},
            pluck="name",
        )
        for name in invalid_statuses:
            frappe.db.set_value(
                "FBR Queue",
                name,
                "status",
                "Pending",
                update_modified=False,
            )

    if "remaining_retries" in columns and "retry_count" in columns:
        if "max_retries" in columns:
            frappe.db.sql(
                """
                UPDATE `tabFBR Queue`
                SET remaining_retries = GREATEST(
                    0,
                    COALESCE(NULLIF(max_retries, 0), %(default_max_retries)s)
                    - IFNULL(retry_count, 0)
                )
                """,
                {"default_max_retries": DEFAULT_MAX_RETRIES},
            )
        else:
            frappe.db.sql(
                """
                UPDATE `tabFBR Queue`
                SET remaining_retries = GREATEST(
                    0,
                    %(default_max_retries)s - IFNULL(retry_count, 0)
                )
                """,
                {"default_max_retries": DEFAULT_MAX_RETRIES},
            )

    if "next_retry_at" in columns:
        now_dt = now_datetime()
        frappe.db.sql(
            """
            UPDATE `tabFBR Queue`
            SET next_retry_at = %(now_dt)s
            WHERE status = 'Pending'
              AND next_retry_at IS NULL
            """,
            {"now_dt": now_dt},
        )

        if "retry_count" in columns:
            if "max_retries" in columns:
                frappe.db.sql(
                    """
                    UPDATE `tabFBR Queue`
                    SET next_retry_at = %(now_dt)s
                    WHERE status = 'Failed'
                      AND next_retry_at IS NULL
                      AND IFNULL(retry_count, 0) < COALESCE(
                          NULLIF(max_retries, 0),
                          %(default_max_retries)s
                      )
                    """,
                    {
                        "now_dt": now_dt,
                        "default_max_retries": DEFAULT_MAX_RETRIES,
                    },
                )
            else:
                frappe.db.sql(
                    """
                    UPDATE `tabFBR Queue`
                    SET next_retry_at = %(now_dt)s
                    WHERE status = 'Failed'
                      AND next_retry_at IS NULL
                      AND IFNULL(retry_count, 0) < %(default_max_retries)s
                    """,
                    {
                        "now_dt": now_dt,
                        "default_max_retries": DEFAULT_MAX_RETRIES,
                    },
                )

        if "modified" in columns and "status" in columns and "error_message" in columns:
            stuck_cutoff = add_to_date(
                now_dt, minutes=-cint(STUCK_PROCESSING_TIMEOUT_MINUTES or 30)
            )
            recovery_note = (
                "Recovered during v16 parity migration from legacy Processing state."
            )
            frappe.db.sql(
                """
                UPDATE `tabFBR Queue`
                SET status = 'Failed',
                    next_retry_at = %(now_dt)s,
                    error_message = CASE
                        WHEN IFNULL(error_message, '') = '' THEN %(recovery_note)s
                        ELSE CONCAT(error_message, '\n', %(recovery_note)s)
                    END
                WHERE status = 'Processing'
                  AND modified <= %(stuck_cutoff)s
                """,
                {
                    "now_dt": now_dt,
                    "recovery_note": recovery_note,
                    "stuck_cutoff": stuck_cutoff,
                },
            )
