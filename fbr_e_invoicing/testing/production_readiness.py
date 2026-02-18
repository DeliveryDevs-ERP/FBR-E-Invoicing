import json
from datetime import datetime
from pathlib import Path

import frappe
import requests
from frappe.utils import add_to_date, now_datetime, nowdate


REQUIRED_PROVINCES = {
    "PUNJAB",
    "SINDH",
    "KYBER PAKHTUNKHWA",
    "BALOCHISTAN",
    "CAPITAL TERRITORY",
    "GILGIT BALTISTAN",
    "AZAD JAMMU AND KASHMIR",
}

REQUIRED_FIELDS = {
    "Sales Invoice": [
        "custom_submit_to_fbr",
        "custom_province",
        "custom_fbr_invoice_number",
        "custom_fbr_datetime",
        "custom_fbr_status",
        "custom_fbr_response",
    ],
    "POS Invoice": [
        "custom_submit_to_fbr",
        "custom_province",
        "custom_fbr_invoice_number",
        "custom_fbr_datetime",
        "custom_fbr_status",
        "custom_fbr_response",
    ],
    "Sales Invoice Item": ["custom_hs_code", "custom_sale_type", "item_tax_template"],
    "POS Invoice Item": ["custom_hs_code", "custom_sale_type", "item_tax_template"],
}

STANDARD_SALE_TYPE = "Goods at Standard Rate (default)"
CANONICAL_STANDARD_SALE_TYPE = "Goods at standard rate (default)"
SANDBOX_MODE_LABEL = "Sandbox Testing"
PRODUCTION_MODE_LABEL = "Production"
INVALID_MODE_LABEL = "Invalid Mode - Test"
DUPLICATE_WARNING_TEXT = "Document already submitted to FBR"


@frappe.whitelist()
def run_campaign(
    seller_tax_id="2868087",
    run_id=None,
    phase="full",
    keep_records=True,
):
    run_id = run_id or f"FBRTEST-{datetime.now().strftime('%Y%m%d-%H%M')}"
    phase = (phase or "full").strip().lower()
    if phase not in {"baseline", "postfix", "full"}:
        frappe.throw("phase must be one of: baseline, postfix, full")

    results = {"run_id": run_id, "site": frappe.local.site, "phase": phase}

    if phase in {"baseline", "full"}:
        results["baseline"] = _run_phase(
            phase_name="baseline",
            run_id=run_id,
            seller_tax_id=seller_tax_id,
            keep_records=bool(keep_records),
        )

    if phase in {"postfix", "full"}:
        results["postfix"] = _run_phase(
            phase_name="postfix",
            run_id=run_id,
            seller_tax_id=seller_tax_id,
            keep_records=bool(keep_records),
        )

    go_no_go = _derive_go_no_go(results)
    results["go_no_go"] = go_no_go
    results["generated_at"] = now_datetime().isoformat()

    run_dir = _get_run_dir(run_id)
    _write_json(run_dir / f"{phase}_result.json", results)
    _write_text(run_dir / f"{phase}_summary.md", _to_markdown(results))

    return {
        "run_id": run_id,
        "phase": phase,
        "go_no_go": go_no_go,
        "run_dir": str(run_dir),
    }


def _run_phase(phase_name, run_id, seller_tax_id, keep_records):
    snapshot = _collect_snapshot()
    setup_checks = _run_setup_checks(snapshot)
    api_checks = _run_api_checks(seller_tax_id)
    queue_checks = _run_queue_retry_checks(run_id, phase_name)
    recovery_checks = _run_recovery_checks(run_id, phase_name)
    bulk_checks = _run_bulk_checks(run_id, phase_name)
    warning_checks = _run_warning_checks(run_id, phase_name)
    queue_level_checks = _run_queue_level_checks(run_id, phase_name)
    double_submit_checks = _run_double_submit_checks(run_id, phase_name)

    if phase_name == "baseline":
        submission_checks = _run_baseline_submission_checks(run_id, seller_tax_id)
    else:
        submission_checks = _run_postfix_submission_checks(run_id, seller_tax_id)

    result = {
        "snapshot": snapshot,
        "setup_checks": setup_checks,
        "api_checks": api_checks,
        "submission_checks": submission_checks,
        "queue_retry_checks": queue_checks,
        "recovery_checks": recovery_checks,
        "bulk_checks": bulk_checks,
        "warning_checks": warning_checks,
        "queue_level_checks": queue_level_checks,
        "double_submit_checks": double_submit_checks,
        "keep_records": keep_records,
    }

    run_dir = _get_run_dir(run_id)
    _write_json(run_dir / f"{phase_name}_detail.json", result)
    return result


def _collect_snapshot():
    setup_doc = frappe.get_single("FBR E-Inv Setup")
    snapshot = {
        "timestamp": now_datetime().isoformat(),
        "setup": {
            "api_endpoint": (setup_doc.api_endpoint or "").strip(),
            "token_present": bool((setup_doc.pral_authorization_token or "").strip()),
            "master_data_retrieved": int(getattr(setup_doc, "master_data_retrieved", 0) or 0),
        },
        "counts": {
            "Sales Invoice": frappe.db.count("Sales Invoice"),
            "POS Invoice": frappe.db.count("POS Invoice"),
            "FBR Queue": frappe.db.count("FBR Queue"),
            "FBR Logs": frappe.db.count("FBR Logs"),
            "Province": frappe.db.count("Province"),
            "HS Code": frappe.db.count("HS Code"),
            "FBR Sale Type": frappe.db.count("FBR Sale Type"),
            "Print Format (FBR Sales Invoice)": frappe.db.count(
                "Print Format", {"name": "FBR Sales Invoice"}
            ),
            "Print Format (FBR POS Invoice)": frappe.db.count(
                "Print Format", {"name": "FBR POS Invoice"}
            ),
        },
        "columns": {
            dt: frappe.db.get_table_columns(dt)
            for dt in (
                "Sales Invoice",
                "POS Invoice",
                "Sales Invoice Item",
                "POS Invoice Item",
            )
        },
        "recent_queue": frappe.get_all(
            "FBR Queue",
            fields=[
                "name",
                "document_type",
                "document_name",
                "status",
                "retry_count",
                "max_retries",
                "next_retry_at",
                "error_message",
            ],
            order_by="modified desc",
            limit_page_length=10,
        ),
        "recent_logs": frappe.get_all(
            "FBR Logs",
            fields=[
                "name",
                "document_type",
                "document_name",
                "status",
                "response_status_code",
                "retry_attempt",
                "submitted_at",
                "fbr_invoice_number",
                "validation_errors",
            ],
            order_by="modified desc",
            limit_page_length=10,
        ),
    }
    return snapshot


def _run_setup_checks(snapshot):
    provinces = set(frappe.get_all("Province", pluck="name", limit_page_length=0))
    workspace_ok = bool(frappe.db.exists("Workspace", "Pak Compliance"))
    sidebar_ok = bool(frappe.db.exists("Workspace Sidebar", "Pak Compliance"))
    html_block_ok = bool(frappe.db.exists("Custom HTML Block", "FBR Setup Instructions Block"))
    sales_pf_ok = bool(frappe.db.exists("Print Format", "FBR Sales Invoice"))
    pos_pf_ok = bool(frappe.db.exists("Print Format", "FBR POS Invoice"))

    field_checks = {}
    for dt, fields in REQUIRED_FIELDS.items():
        cols = set(snapshot["columns"].get(dt, []))
        field_checks[dt] = {field: field in cols for field in fields}

    return {
        "required_provinces_present": REQUIRED_PROVINCES.issubset(provinces),
        "missing_provinces": sorted(REQUIRED_PROVINCES.difference(provinces)),
        "hs_codes_count": snapshot["counts"]["HS Code"],
        "sale_types_count": snapshot["counts"]["FBR Sale Type"],
        "workspace_exists": workspace_ok,
        "workspace_sidebar_exists": sidebar_ok,
        "setup_html_block_exists": html_block_ok,
        "sales_print_format_exists": sales_pf_ok,
        "pos_print_format_exists": pos_pf_ok,
        "required_field_presence": field_checks,
    }


def _run_api_checks(seller_tax_id):
    setup_doc = frappe.get_single("FBR E-Inv Setup")
    endpoint = (setup_doc.api_endpoint or "").strip()
    token = (setup_doc.pral_authorization_token or "").strip()

    token_probe = {"ok": False, "status_code": None, "error": ""}
    if token:
        try:
            resp = requests.get(
                "https://gw.fbr.gov.pk/pdi/v1/provinces",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            token_probe.update({"ok": resp.status_code == 200, "status_code": resp.status_code})
        except Exception as exc:
            token_probe["error"] = str(exc)

    from fbr_e_invoicing.api.fbr_validation import check_fbr_api_status

    api_status = check_fbr_api_status()

    return {
        "endpoint": endpoint,
        "seller_tax_id_under_test": seller_tax_id,
        "token_probe": token_probe,
        "api_status": api_status,
    }


def _run_baseline_submission_checks(run_id, seller_tax_id):
    app_style_probe = _post_probe(
        seller_tax_id=seller_tax_id,
        scenario_id="SN026",
        sale_type=STANDARD_SALE_TYPE,
        buyer_registration_type="Unregistered",
        buyer_tax_id="",
        uom="Nos",
        rate="17%",
    )

    canonical_probe = _post_probe(
        seller_tax_id=seller_tax_id,
        scenario_id="SN002",
        sale_type=CANONICAL_STANDARD_SALE_TYPE,
        buyer_registration_type="Unregistered",
        buyer_tax_id="",
        uom="Numbers, pieces, units",
        rate="18%",
    )

    pos_baseline = _attempt_pos_submission(run_id=run_id, label="baseline")
    mode_payload_checks = _run_mode_payload_checks(run_id=f"{run_id}-BASELINE")

    return {
        "app_style_probe": app_style_probe,
        "canonical_probe": canonical_probe,
        "pos_submission_baseline": pos_baseline,
        "mode_payload_checks": mode_payload_checks,
    }


def _run_postfix_submission_checks(run_id, seller_tax_id):
    company = _get_or_create_company_for_test()
    frappe.db.set_value("Company", company, "tax_id", seller_tax_id)
    frappe.db.commit()

    _ensure_uom("Numbers, pieces, units")
    tax_template = _ensure_item_tax_template(company=company, title="FBR Test 18 - {}".format(company))
    item_code = _ensure_test_item(
        run_id=run_id,
        hs_code="0101.2100",
        stock_uom="Numbers, pieces, units",
        sale_type=STANDARD_SALE_TYPE,
    )
    customer = _ensure_test_customer(run_id)

    sales_invoice_name = _create_sales_invoice_for_submission(
        run_id=run_id,
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=tax_template,
    )
    sales_result = _submit_and_persist("Sales Invoice", sales_invoice_name)

    previous_invoice_type = _set_pos_invoice_mode_for_test()
    try:
        pos_invoice_name = _create_pos_invoice_for_submission(
            run_id=run_id,
            company=company,
            customer=customer,
            item_code=item_code,
            item_tax_template=tax_template,
        )
        pos_result = _submit_pos_via_server_fallback(pos_invoice_name)
    finally:
        _restore_pos_invoice_mode(previous_invoice_type)

    mode_payload_checks = _run_mode_payload_checks(
        run_id=f"{run_id}-POSTFIX",
        sales_invoice_name=sales_invoice_name,
        pos_invoice_name=pos_invoice_name,
    )

    return {
        "sales_invoice_name": sales_invoice_name,
        "sales_submission": sales_result,
        "pos_invoice_name": pos_invoice_name,
        "pos_submission": pos_result,
        "mode_payload_checks": mode_payload_checks,
    }


def _run_queue_retry_checks(run_id, phase_name):
    from fbr_e_invoicing.api import fbr_queue

    company = _get_or_create_company_for_test()
    customer = _ensure_test_customer(run_id)
    item_code = _ensure_test_item(run_id, "0101.2100", "Nos", STANDARD_SALE_TYPE)
    item_tax_template = _ensure_item_tax_template(company, f"FBR Test 18 - {company}")

    missing_doc = _create_sales_invoice_for_submission(
        run_id=f"{run_id}-{phase_name}-QUEUE-RETRY",
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=item_tax_template,
        save_as_draft=True,
    )
    # Force a deterministic validation failure so retry/backoff is exercised.
    frappe.db.sql(
        """
        UPDATE `tabSales Invoice Item`
        SET custom_sale_type = %s
        WHERE parent = %s
        """,
        ("Unsupported Sale Type", missing_doc),
    )
    queue_result = fbr_queue.add_to_queue(
        doctype="Sales Invoice",
        docname=missing_doc,
        status="Pending",
        priority=5,
    )
    if not queue_result.get("success"):
        return {"ok": False, "error": queue_result.get("error")}

    queue_id = queue_result["queue_id"]
    attempts = []
    for _ in range(5):
        frappe.db.set_value(
            "FBR Queue",
            queue_id,
            {"status": "Pending", "next_retry_at": now_datetime(), "error_message": ""},
        )
        fbr_queue._process_single_queue_item(queue_id)
        row = frappe.db.get_value(
            "FBR Queue",
            queue_id,
            ["status", "retry_count", "max_retries", "next_retry_at", "error_message"],
            as_dict=True,
        )
        attempts.append(row)
        if row.status == "Failed":
            break

    retry_counts = [int(row.retry_count or 0) for row in attempts]
    expected_retry_counts = list(range(1, len(retry_counts) + 1))
    final_status = (attempts[-1].status if attempts else "") if attempts else ""
    retry_progression_ok = retry_counts == expected_retry_counts
    failed_as_expected = final_status == "Failed"

    return {
        "ok": True,
        "queue_id": queue_id,
        "attempts": attempts,
        "retry_counts": retry_counts,
        "retry_progression_ok": retry_progression_ok,
        "failed_as_expected": failed_as_expected,
        "delay_minutes_reference": [
            fbr_queue._get_retry_delay_minutes(i) for i in (1, 2, 3, 4, 5)
        ],
    }


def _run_warning_checks(run_id, phase_name):
    from fbr_e_invoicing.api.fbr_validation import get_fbr_warnings

    company = _get_or_create_company_for_test()
    customer = _ensure_test_customer(run_id)
    item_code = _ensure_test_item(run_id, "0101.2100", "Nos", STANDARD_SALE_TYPE)
    item_tax_template = _ensure_item_tax_template(company, f"FBR Test 18 - {company}")

    invoice_name = _create_sales_invoice_for_submission(
        run_id=f"{run_id}-{phase_name}-WARN",
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=item_tax_template,
        save_as_draft=True,
    )

    doc = frappe.get_doc("Sales Invoice", invoice_name)
    before_warnings = get_fbr_warnings(doc) or []
    before_has_duplicate = DUPLICATE_WARNING_TEXT in before_warnings

    test_invoice_number = f"{run_id}-{phase_name}-DUPLICATE-WARNING"
    frappe.db.set_value("Sales Invoice", invoice_name, "custom_fbr_invoice_number", test_invoice_number)
    doc.reload()
    after_warnings = get_fbr_warnings(doc) or []
    after_has_duplicate = DUPLICATE_WARNING_TEXT in after_warnings

    duplicate_warning_transition_ok = (not before_has_duplicate) and after_has_duplicate

    return {
        "invoice_name": invoice_name,
        "duplicate_warning_text": DUPLICATE_WARNING_TEXT,
        "before_warnings": before_warnings,
        "after_warnings": after_warnings,
        "before_has_duplicate_warning": before_has_duplicate,
        "after_has_duplicate_warning": after_has_duplicate,
        "duplicate_warning_transition_ok": duplicate_warning_transition_ok,
    }


def _run_queue_level_checks(run_id, phase_name):
    from fbr_e_invoicing.api import fbr_queue

    company = _get_or_create_company_for_test()
    customer = _ensure_test_customer(run_id)
    item_code = _ensure_test_item(run_id, "0101.2100", "Nos", STANDARD_SALE_TYPE)
    item_tax_template = _ensure_item_tax_template(company, f"FBR Test 18 - {company}")

    invoice_name = _create_sales_invoice_for_submission(
        run_id=f"{run_id}-{phase_name}-QUEUE-LEVEL",
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=item_tax_template,
        save_as_draft=True,
    )

    first_result = fbr_queue.add_to_queue(
        doctype="Sales Invoice",
        docname=invoice_name,
        status="Pending",
        priority=5,
    )
    if not first_result.get("success"):
        return {"ok": False, "invoice_name": invoice_name, "error": first_result.get("error")}

    first_queue_id = first_result.get("queue_id")
    second_result = fbr_queue.add_to_queue(
        doctype="Sales Invoice",
        docname=invoice_name,
        status="Pending",
        priority=5,
    )

    if first_queue_id:
        frappe.db.set_value(
            "FBR Queue",
            first_queue_id,
            {"status": "Processing", "next_retry_at": None, "error_message": ""},
        )

    third_result = fbr_queue.add_to_queue(
        doctype="Sales Invoice",
        docname=invoice_name,
        status="Pending",
        priority=5,
    )

    active_rows = frappe.get_all(
        "FBR Queue",
        filters={
            "document_type": "Sales Invoice",
            "document_name": invoice_name,
            "status": ["in", ["Pending", "Processing"]],
        },
        fields=["name", "status", "retry_count", "error_message"],
        order_by="creation asc",
        limit_page_length=10,
    )

    second_queue_id = second_result.get("queue_id")
    third_queue_id = third_result.get("queue_id")
    same_queue_id_pending = bool(first_queue_id) and second_queue_id == first_queue_id
    same_queue_id_processing = bool(first_queue_id) and third_queue_id == first_queue_id
    pending_dedupe_ok = second_result.get("state") == "already_pending" and same_queue_id_pending
    processing_guard_ok = (
        third_result.get("state") == "already_processing" and same_queue_id_processing
    )
    active_row_uniqueness_ok = len(active_rows) == 1
    ok = bool(first_result.get("success")) and pending_dedupe_ok and processing_guard_ok and active_row_uniqueness_ok

    return {
        "ok": ok,
        "invoice_name": invoice_name,
        "first_enqueue": first_result,
        "second_enqueue": second_result,
        "third_enqueue_after_processing": third_result,
        "pending_dedupe_ok": pending_dedupe_ok,
        "processing_guard_ok": processing_guard_ok,
        "active_row_uniqueness_ok": active_row_uniqueness_ok,
        "active_row_count": len(active_rows),
        "active_rows": active_rows,
    }


def _run_double_submit_checks(run_id, phase_name):
    from fbr_e_invoicing.api.fbr_submission import submit_single_invoice

    company = _get_or_create_company_for_test()
    customer = _ensure_test_customer(run_id)
    item_code = _ensure_test_item(run_id, "0101.2100", "Nos", STANDARD_SALE_TYPE)
    item_tax_template = _ensure_item_tax_template(company, f"FBR Test 18 - {company}")

    invoice_name = _create_sales_invoice_for_submission(
        run_id=f"{run_id}-{phase_name}-DOUBLE-SUBMIT",
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=item_tax_template,
        save_as_draft=True,
    )

    # Force deterministic payload failure to exercise auto-queue path without external API dependence.
    frappe.db.sql(
        """
        UPDATE `tabSales Invoice Item`
        SET custom_sale_type = %s
        WHERE parent = %s
        """,
        ("Unsupported Sale Type", invoice_name),
    )

    first_result = submit_single_invoice("Sales Invoice", invoice_name, is_retry=False)
    second_result = submit_single_invoice("Sales Invoice", invoice_name, is_retry=False)

    first_queue_id = first_result.get("queue_id")
    second_queue_id = second_result.get("queue_id")
    same_queue_id = bool(first_queue_id) and first_queue_id == second_queue_id

    active_rows = frappe.get_all(
        "FBR Queue",
        filters={
            "document_type": "Sales Invoice",
            "document_name": invoice_name,
            "status": ["in", ["Pending", "Processing"]],
        },
        fields=["name", "status", "retry_count", "error_message"],
        order_by="creation asc",
        limit_page_length=10,
    )

    first_structured = isinstance(first_result, dict) and all(
        key in first_result for key in ("success", "status", "message", "response", "queue_id")
    )
    second_structured = isinstance(second_result, dict) and all(
        key in second_result for key in ("success", "status", "message", "response", "queue_id")
    )
    active_row_uniqueness_ok = len(active_rows) == 1
    ok = first_structured and second_structured and same_queue_id and active_row_uniqueness_ok

    return {
        "ok": ok,
        "invoice_name": invoice_name,
        "first_result": first_result,
        "second_result": second_result,
        "first_structured": first_structured,
        "second_structured": second_structured,
        "same_queue_id": same_queue_id,
        "active_row_uniqueness_ok": active_row_uniqueness_ok,
        "active_row_count": len(active_rows),
        "active_rows": active_rows,
    }


def _run_recovery_checks(run_id, phase_name):
    from fbr_e_invoicing.api import fbr_queue

    company = _get_or_create_company_for_test()
    customer = _ensure_test_customer(run_id)
    item_code = _ensure_test_item(run_id, "0101.2100", "Nos", STANDARD_SALE_TYPE)
    item_tax_template = _ensure_item_tax_template(company, f"FBR Test 18 - {company}")

    stuck_name = _create_sales_invoice_for_submission(
        run_id=f"{run_id}-{phase_name}-STUCK",
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=item_tax_template,
        save_as_draft=True,
    )
    stuck_res = fbr_queue.add_to_queue("Sales Invoice", stuck_name, status="Processing", priority=5)
    stuck_queue = stuck_res.get("queue_id")

    if stuck_queue:
        stale_time = add_to_date(now_datetime(), minutes=-90)
        frappe.db.sql(
            """
            UPDATE `tabFBR Queue`
            SET status='Processing', modified=%s
            WHERE name=%s
            """,
            (stale_time, stuck_queue),
        )

    retryable_name = _create_sales_invoice_for_submission(
        run_id=f"{run_id}-{phase_name}-FAILED",
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=item_tax_template,
        save_as_draft=True,
    )
    retry_res = fbr_queue.add_to_queue("Sales Invoice", retryable_name, status="Pending", priority=5)
    retry_queue = retry_res.get("queue_id")
    if retry_queue:
        frappe.db.set_value(
            "FBR Queue",
            retry_queue,
            {
                "status": "Failed",
                "retry_count": 2,
                "max_retries": 5,
                "next_retry_at": None,
            },
        )

    recovery = fbr_queue.recover_stuck_and_retryable_items()

    return {
        "ok": True,
        "recovery_summary": recovery,
        "stuck_item": frappe.db.get_value(
            "FBR Queue",
            stuck_queue,
            ["status", "retry_count", "next_retry_at", "error_message"],
            as_dict=True,
        )
        if stuck_queue
        else None,
        "retryable_failed_item": frappe.db.get_value(
            "FBR Queue",
            retry_queue,
            ["status", "retry_count", "next_retry_at", "error_message"],
            as_dict=True,
        )
        if retry_queue
        else None,
    }


def _run_bulk_checks(run_id, phase_name):
    from fbr_e_invoicing.api.fbr_submission import bulk_submit_sales_invoices

    draft_name = _create_sales_invoice_for_submission(
        run_id=f"{run_id}-{phase_name}-DRAFT",
        company=_get_or_create_company_for_test(),
        customer=_ensure_test_customer(run_id),
        item_code=_ensure_test_item(run_id, "0101.2100", "Nos", STANDARD_SALE_TYPE),
        item_tax_template=_ensure_item_tax_template(_get_or_create_company_for_test(), "FBR Test 18 - BE"),
        save_as_draft=True,
    )

    submitted_name = frappe.db.get_value(
        "Sales Invoice",
        {"docstatus": 1},
        "name",
        order_by="modified desc",
    )

    docnames = [submitted_name, draft_name, "MISSING-SI-BULK", submitted_name]
    result = bulk_submit_sales_invoices(docnames)
    return {"input_docnames": docnames, "result": result}


def _set_pos_invoice_mode_for_test():
    current = frappe.db.get_single_value("POS Settings", "invoice_type") or "POS Invoice"
    if current != "POS Invoice":
        frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")
        frappe.db.commit()
    return current


def _restore_pos_invoice_mode(previous_invoice_type):
    previous = previous_invoice_type or "POS Invoice"
    current = frappe.db.get_single_value("POS Settings", "invoice_type") or "POS Invoice"
    if current != previous:
        frappe.db.set_single_value("POS Settings", "invoice_type", previous)
        frappe.db.commit()


def _attempt_pos_submission(run_id, label):
    from fbr_e_invoicing.api.fbr_submission import submit_single_invoice

    try:
        pos_name = _create_pos_invoice_for_submission(
            run_id=f"{run_id}-{label}",
            company=_get_or_create_company_for_test(),
            customer=_ensure_test_customer(run_id),
            item_code=_ensure_test_item(run_id, "0101.2100", "Nos", STANDARD_SALE_TYPE),
            item_tax_template=_ensure_item_tax_template(_get_or_create_company_for_test(), "FBR Test 18 - BE"),
        )
        result = submit_single_invoice("POS Invoice", pos_name, is_retry=False)
        return {"ok": bool(result.get("success")), "invoice": pos_name, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _submit_and_persist(doctype, docname):
    from fbr_e_invoicing.api.fbr_submission import _persist_fbr_response_fields, submit_single_invoice

    result = submit_single_invoice(doctype, docname, is_retry=False)
    response = result.get("response") or {}
    if isinstance(response, dict) and response:
        _persist_fbr_response_fields(doctype, docname, response)

    db_state = frappe.db.get_value(
        doctype,
        docname,
        ["custom_fbr_invoice_number", "custom_fbr_datetime", "custom_fbr_status"],
        as_dict=True,
    )
    return {"result": result, "db_state": db_state}


def _submit_pos_via_server_fallback(pos_invoice_name):
    from fbr_e_invoicing.api.fbr_submission import submit_pos_invoice_on_submit

    if "custom_submit_to_fbr" in set(frappe.db.get_table_columns("POS Invoice") or []):
        frappe.db.set_value("POS Invoice", pos_invoice_name, "custom_submit_to_fbr", 1)

    doc = frappe.get_doc("POS Invoice", pos_invoice_name)
    submit_pos_invoice_on_submit(doc, method="on_submit")

    response_field = (
        "custom_fbr_response"
        if "custom_fbr_response" in set(frappe.db.get_table_columns("POS Invoice") or [])
        else "custom_fbr_responce"
    )
    state = frappe.db.get_value(
        "POS Invoice",
        pos_invoice_name,
        ["custom_fbr_invoice_number", "custom_fbr_datetime", "custom_fbr_status", response_field],
        as_dict=True,
    )
    return {"db_state": state}


def _run_mode_payload_checks(run_id, sales_invoice_name=None, pos_invoice_name=None):
    from fbr_e_invoicing.api.fbr_submission import _build_payload

    company = _get_or_create_company_for_test()
    customer = _ensure_test_customer(run_id)
    item_code = _ensure_test_item(run_id, "0101.2100", "Numbers, pieces, units", STANDARD_SALE_TYPE)
    item_tax_template = _ensure_item_tax_template(company, f"FBR Test 18 - {company}")

    sales_invoice_name = sales_invoice_name or _create_sales_invoice_for_submission(
        run_id=f"{run_id}-MODE-SI",
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=item_tax_template,
        save_as_draft=True,
    )
    pos_invoice_name = pos_invoice_name or _create_pos_invoice_for_submission(
        run_id=f"{run_id}-MODE-POS",
        company=company,
        customer=customer,
        item_code=item_code,
        item_tax_template=item_tax_template,
    )

    previous_mode = frappe.db.get_single_value("FBR E-Inv Setup", "mode") or ""
    results = {
        "sales_invoice_name": sales_invoice_name,
        "pos_invoice_name": pos_invoice_name,
    }

    try:
        results["sandbox"] = _build_payload_mode_snapshot(
            sales_invoice_name=sales_invoice_name,
            pos_invoice_name=pos_invoice_name,
            mode_label=SANDBOX_MODE_LABEL,
            expected_has_scenario=True,
        )
        results["production"] = _build_payload_mode_snapshot(
            sales_invoice_name=sales_invoice_name,
            pos_invoice_name=pos_invoice_name,
            mode_label=PRODUCTION_MODE_LABEL,
            expected_has_scenario=False,
        )

        _set_fbr_mode_for_test(INVALID_MODE_LABEL)
        invalid_sales = _try_build_sales_payload(sales_invoice_name)
        invalid_pos = _try_build_pos_payload(pos_invoice_name)
        invalid_submission_probe = _build_payload("Sales Invoice", sales_invoice_name)
        expected_phrase = "Please set Mode to"
        results["invalid_mode"] = {
            "mode_label": INVALID_MODE_LABEL,
            "sales_build": invalid_sales,
            "pos_build": invalid_pos,
            "submission_build_probe": invalid_submission_probe,
            "sales_error_deterministic": (expected_phrase in (invalid_sales.get("error") or "")),
            "pos_error_deterministic": (expected_phrase in (invalid_pos.get("error") or "")),
            "submission_error_deterministic": (
                expected_phrase in (invalid_submission_probe.get("error") or "")
            ),
        }
    finally:
        _set_fbr_mode_for_test(previous_mode)

    return results


def _build_payload_mode_snapshot(sales_invoice_name, pos_invoice_name, mode_label, expected_has_scenario):
    _set_fbr_mode_for_test(mode_label)
    sales = _try_build_sales_payload(sales_invoice_name)
    pos = _try_build_pos_payload(pos_invoice_name)

    sales_has_scenario = bool(sales.get("payload") and "scenarioId" in sales["payload"])
    pos_has_scenario = bool(pos.get("payload") and "scenarioId" in pos["payload"])

    return {
        "mode_label": mode_label,
        "expected_has_scenario": expected_has_scenario,
        "sales_build": sales,
        "pos_build": pos,
        "sales_has_scenario": sales_has_scenario,
        "pos_has_scenario": pos_has_scenario,
        "sales_expectation_met": bool(sales.get("ok")) and (sales_has_scenario == expected_has_scenario),
        "pos_expectation_met": bool(pos.get("ok")) and (pos_has_scenario == expected_has_scenario),
    }


def _set_fbr_mode_for_test(mode_label):
    frappe.db.set_single_value("FBR E-Inv Setup", "mode", mode_label or "")
    frappe.db.commit()


def _try_build_sales_payload(sales_invoice_name):
    from fbr_e_invoicing.api.build_fbr_payload import build_fbr_payload

    try:
        payload = build_fbr_payload(sales_invoice_name)
        return {"ok": True, "payload": payload, "error": ""}
    except Exception as exc:
        return {"ok": False, "payload": {}, "error": str(exc)}


def _try_build_pos_payload(pos_invoice_name):
    from fbr_e_invoicing.api.build_fbr_payload import build_pos_fbr_payload

    try:
        payload = build_pos_fbr_payload(pos_invoice_name)
        return {"ok": True, "payload": payload, "error": ""}
    except Exception as exc:
        return {"ok": False, "payload": {}, "error": str(exc)}


def _post_probe(
    seller_tax_id,
    scenario_id,
    sale_type,
    buyer_registration_type,
    buyer_tax_id,
    uom,
    rate,
):
    setup_doc = frappe.get_single("FBR E-Inv Setup")
    endpoint = (setup_doc.api_endpoint or "").strip()
    token = (setup_doc.pral_authorization_token or "").strip()
    mode_label = (setup_doc.mode or "").strip()

    payload = {
        "invoiceType": "Sale Invoice",
        "invoiceDate": nowdate(),
        "sellerNTNCNIC": seller_tax_id,
        "sellerBusinessName": "FBR TEST SELLER",
        "sellerProvince": "Sindh",
        "sellerAddress": "Karachi",
        "buyerNTNCNIC": buyer_tax_id or "",
        "buyerBusinessName": "FBR TEST BUYER",
        "buyerProvince": "Sindh",
        "buyerAddress": "Karachi",
        "buyerRegistrationType": buyer_registration_type,
        "invoiceRefNo": "",
        "items": [
            {
                "hsCode": "0101.2100",
                "productDescription": "FBR test item",
                "rate": rate,
                "uoM": uom,
                "quantity": 1.0,
                "totalValues": 0.0,
                "valueSalesExcludingST": 1000.0,
                "fixedNotifiedValueOrRetailPrice": 0.0,
                "salesTaxApplicable": 180.0,
                "salesTaxWithheldAtSource": 0.0,
                "extraTax": 0.0,
                "furtherTax": 0.0,
                "sroScheduleNo": "",
                "fedPayable": 0.0,
                "discount": 0.0,
                "saleType": sale_type,
                "sroItemSerialNo": "",
            }
        ],
    }
    if mode_label.casefold() == SANDBOX_MODE_LABEL.casefold():
        payload["scenarioId"] = scenario_id

    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        data = {}
        try:
            data = response.json() if response.text else {}
        except Exception:
            data = {"raw": response.text[:1000]}
        return {
            "status_code": response.status_code,
            "response": data,
            "mode_label": mode_label,
            "payload_has_scenario": "scenarioId" in payload,
        }
    except Exception as exc:
        return {
            "status_code": None,
            "error": str(exc),
            "mode_label": mode_label,
            "payload_has_scenario": "scenarioId" in payload,
        }


def _get_or_create_company_for_test():
    company = frappe.db.get_value("Company", {"country": "Pakistan"}, "name")
    if company:
        return company
    return frappe.get_all("Company", pluck="name", limit_page_length=1)[0]


def _ensure_uom(uom_name):
    if frappe.db.exists("UOM", uom_name):
        return uom_name

    doc = frappe.new_doc("UOM")
    doc.uom_name = uom_name
    doc.enabled = 1
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_item_tax_template(company, title):
    existing = frappe.db.get_value("Item Tax Template", {"title": title, "company": company}, "name")
    if existing:
        return existing

    tax_account = (
        frappe.db.get_value(
            "Item Tax Template Detail",
            {"parent": ["is", "set"]},
            "tax_type",
            order_by="modified desc",
        )
        or frappe.db.get_value("Account", {"company": company, "account_type": "Tax"}, "name")
    )
    if not tax_account:
        frappe.throw("No tax account available to create Item Tax Template")

    doc = frappe.get_doc(
        {
            "doctype": "Item Tax Template",
            "title": title,
            "company": company,
            "taxes": [{"tax_type": tax_account, "tax_rate": 18.0}],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_pos_profile(company, run_id):
    active_profile = frappe.db.get_value(
        "POS Opening Entry",
        {"company": company, "status": "Open", "posting_date": nowdate()},
        "pos_profile",
    )
    if active_profile:
        return active_profile

    profile_name = f"FBR Test POS Profile - {run_id}"
    existing = frappe.db.get_value("POS Profile", profile_name, "name")
    if existing:
        return existing

    currency = frappe.db.get_value("Company", company, "default_currency") or "PKR"
    warehouse = (
        frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
        or frappe.db.get_value("Warehouse", {"is_group": 0}, "name")
    )
    write_off_cost_center = (
        frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
        or frappe.db.get_value("Cost Center", {"is_group": 0}, "name")
    )
    write_off_account = _get_write_off_account(company)
    mode_of_payment = _ensure_mode_of_payment_for_company(company)
    price_list = (
        frappe.db.get_value("Price List", {"selling": 1, "currency": currency, "enabled": 1}, "name")
        or frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")
    )

    if not warehouse:
        frappe.throw(f"No warehouse found to build POS Profile for company {company}")
    if not write_off_cost_center:
        frappe.throw(f"No cost center found to build POS Profile for company {company}")
    if not write_off_account:
        frappe.throw(f"No write-off account found to build POS Profile for company {company}")
    if not mode_of_payment:
        frappe.throw(f"No mode of payment found to build POS Profile for company {company}")

    doc = frappe.get_doc(
        {
            "doctype": "POS Profile",
            "name": profile_name,
            "company": company,
            "currency": currency,
            "warehouse": warehouse,
            "write_off_account": write_off_account,
            "write_off_cost_center": write_off_cost_center,
            "write_off_limit": 1000,
            "selling_price_list": price_list,
            "payments": [{"mode_of_payment": mode_of_payment, "default": 1}],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_open_pos_opening_entry(company, pos_profile):
    existing = frappe.db.get_value(
        "POS Opening Entry",
        {"company": company, "pos_profile": pos_profile, "status": "Open", "posting_date": nowdate()},
        "name",
    )
    if existing:
        return existing

    pos_profile_doc = frappe.get_doc("POS Profile", pos_profile)
    if not (pos_profile_doc.get("payments") or []):
        frappe.throw(f"POS Profile {pos_profile} has no payment methods")

    opening_user = _get_available_pos_cashier()
    if not opening_user:
        fallback = frappe.db.get_value(
            "POS Opening Entry",
            {"company": company, "status": "Open", "posting_date": nowdate()},
            "name",
        )
        if fallback:
            return fallback
        frappe.throw("No enabled user available for POS Opening Entry")

    opening_doc = frappe.get_doc(
        {
            "doctype": "POS Opening Entry",
            "period_start_date": now_datetime(),
            "posting_date": nowdate(),
            "company": company,
            "pos_profile": pos_profile,
            "user": opening_user,
            "balance_details": [
                {"mode_of_payment": row.mode_of_payment, "opening_amount": 0}
                for row in pos_profile_doc.get("payments")
            ],
        }
    )
    opening_doc.insert(ignore_permissions=True)
    opening_doc.submit()
    return opening_doc.name


def _get_available_pos_cashier():
    open_users = set(
        frappe.get_all(
            "POS Opening Entry",
            filters={"status": "Open"},
            pluck="user",
            limit_page_length=0,
        )
    )

    if frappe.db.get_value("User", "Administrator", "enabled") and "Administrator" not in open_users:
        return "Administrator"

    users = frappe.db.sql_list(
        """
        SELECT name
        FROM `tabUser`
        WHERE enabled = 1
          AND name NOT IN ('Guest')
        ORDER BY name
        """
    )
    for user in users:
        if user not in open_users:
            return user

    return None


def _get_write_off_account(company):
    default_cash = frappe.db.get_value("Company", company, "default_cash_account")
    if default_cash and frappe.db.exists("Account", default_cash):
        return default_cash

    cash_or_bank = _get_cash_or_bank_account(company)
    if cash_or_bank:
        return cash_or_bank

    return (
        frappe.db.get_value(
            "Account",
            {"company": company, "is_group": 0, "disabled": 0, "root_type": "Expense"},
            "name",
        )
        or frappe.db.get_value(
            "Account",
            {"company": company, "is_group": 0, "disabled": 0},
            "name",
        )
    )


def _get_cash_or_bank_account(company):
    account = frappe.db.get_value(
        "Account",
        {"company": company, "is_group": 0, "disabled": 0, "account_type": ["in", ["Cash", "Bank"]]},
        "name",
    )
    if account:
        return account

    default_cash = frappe.db.get_value("Company", company, "default_cash_account")
    if default_cash and frappe.db.exists("Account", default_cash):
        return default_cash

    return None


def _ensure_mode_of_payment_for_company(company):
    existing = frappe.db.get_value(
        "Mode of Payment Account",
        {"company": company, "default_account": ["is", "set"]},
        ["parent", "default_account"],
        as_dict=True,
    )
    if existing and existing.parent and existing.default_account:
        return existing.parent

    default_account = _get_cash_or_bank_account(company)
    if not default_account:
        frappe.throw(f"No cash/bank account found to build Mode of Payment for company {company}")

    mode_of_payment = frappe.db.get_value("Mode of Payment", {"mode_of_payment": "Cash"}, "name")
    if not mode_of_payment:
        mode_of_payment = frappe.db.get_value("Mode of Payment", {"enabled": 1}, "name")
    if not mode_of_payment:
        mode_name = f"FBR Test Cash - {company}"
        mode_doc = frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": mode_name, "enabled": 1})
        mode_doc.insert(ignore_permissions=True)
        mode_of_payment = mode_doc.name

    mode_doc = frappe.get_doc("Mode of Payment", mode_of_payment)
    has_company_row = False
    for row in mode_doc.get("accounts") or []:
        if row.company == company:
            has_company_row = True
            if not row.default_account:
                row.default_account = default_account
    if not has_company_row:
        mode_doc.append("accounts", {"company": company, "default_account": default_account})
    mode_doc.save(ignore_permissions=True)
    return mode_doc.name


def _ensure_test_customer(run_id):
    customer_name = f"{run_id}-FBR-CUSTOMER"
    existing = frappe.db.get_value("Customer", {"name": customer_name}, "name")
    if existing:
        return existing

    customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")

    doc = frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": customer_name,
            "customer_group": customer_group or "All Customer Groups",
            "territory": territory or "All Territories",
            "custom_province": "SINDH",
            "tax_category": "SINDH",
        }
    )
    doc.insert(ignore_permissions=True, ignore_mandatory=True)
    return doc.name


def _ensure_test_item(run_id, hs_code, stock_uom, sale_type):
    item_code = f"{run_id}-FBR-ITEM"
    existing = frappe.db.get_value("Item", {"item_code": item_code}, "name")
    if existing:
        return existing

    _ensure_uom(stock_uom)
    item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    doc = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": item_code,
            "item_name": item_code,
            "item_group": item_group,
            "stock_uom": stock_uom,
            "is_stock_item": 0,
            "custom_hs_code": hs_code,
            "custom_sale_type": sale_type,
        }
    )
    doc.insert(ignore_permissions=True, ignore_mandatory=True)
    return doc.name


def _create_sales_invoice_for_submission(
    run_id,
    company,
    customer,
    item_code,
    item_tax_template,
    save_as_draft=True,
):
    doc = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": customer,
            "company": company,
            "posting_date": nowdate(),
            "due_date": nowdate(),
            "currency": "PKR",
            "conversion_rate": 1,
            "custom_submit_to_fbr": 0,
            "custom_province": "SINDH",
            "tax_category": "SINDH",
            "items": [
                {
                    "item_code": item_code,
                    "qty": 1,
                    "rate": 100.0,
                    "amount": 100.0,
                    "custom_hs_code": "0101.2100",
                    "custom_sale_type": STANDARD_SALE_TYPE,
                    "item_tax_template": item_tax_template,
                }
            ],
        }
    )
    doc.insert(ignore_permissions=True, ignore_mandatory=True)
    if not save_as_draft:
        doc.submit()
    return doc.name


def _create_pos_invoice_for_submission(run_id, company, customer, item_code, item_tax_template):
    pos_profile = _ensure_pos_profile(company, run_id)
    _ensure_open_pos_opening_entry(company, pos_profile)
    pos_profile_doc = frappe.get_doc("POS Profile", pos_profile)
    pos_payments = []
    for idx, row in enumerate(pos_profile_doc.get("payments") or []):
        if not row.mode_of_payment:
            continue
        pos_payments.append(
            {
                "mode_of_payment": row.mode_of_payment,
                "amount": 100.0,
                "default": 1 if idx == 0 else 0,
            }
        )
    if not pos_payments:
        frappe.throw(f"POS Profile {pos_profile} has no usable payment rows")

    values = {
        "doctype": "POS Invoice",
        "customer": customer,
        "company": company,
        "pos_profile": pos_profile,
        "posting_date": nowdate(),
        "currency": "PKR",
        "conversion_rate": 1,
        "tax_category": "SINDH",
        "custom_submit_to_fbr": 0,
        "payments": pos_payments,
        "items": [
            {
                "item_code": item_code,
                "qty": 1,
                "rate": 100.0,
                "amount": 100.0,
                "custom_hs_code": "0101.2100",
                "custom_sale_type": STANDARD_SALE_TYPE,
                "item_tax_template": item_tax_template,
            }
        ],
    }
    if "custom_province" in set(frappe.db.get_table_columns("POS Invoice") or []):
        values["custom_province"] = "SINDH"

    doc = frappe.get_doc(values)
    doc.insert(ignore_permissions=True, ignore_mandatory=True)
    return doc.name


def _get_run_dir(run_id):
    run_dir = Path(frappe.get_site_path("private", "files", "fbr_test_runs", run_id))
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)


def _derive_go_no_go(results):
    postfix = results.get("postfix") or {}
    if not postfix:
        return {"decision": "NO-GO", "reason": "No postfix phase present"}

    schema_ok = _check_required_field_presence(postfix.get("setup_checks", {}))
    sales_ok = _status_valid(
        ((postfix.get("submission_checks") or {}).get("sales_submission") or {}).get("result")
    )
    pos_ok = (
        (((postfix.get("submission_checks") or {}).get("pos_submission") or {}).get("db_state") or {}).get(
            "custom_fbr_status"
        )
        == "Valid"
    )
    queue_checks = postfix.get("queue_retry_checks") or {}
    queue_ok = bool(queue_checks.get("retry_progression_ok")) and bool(
        queue_checks.get("failed_as_expected")
    )
    warning_checks = postfix.get("warning_checks") or {}
    warning_duplicate_check_ok = bool(warning_checks.get("duplicate_warning_transition_ok"))
    queue_level_checks = postfix.get("queue_level_checks") or {}
    queue_level_ok = bool(queue_level_checks.get("ok"))
    double_submit_checks = postfix.get("double_submit_checks") or {}
    double_submit_ok = bool(double_submit_checks.get("ok"))
    setup_ok = bool((postfix.get("setup_checks") or {}).get("workspace_exists"))
    mode_payload_checks = ((postfix.get("submission_checks") or {}).get("mode_payload_checks") or {})
    mode_payload_ok = all(
        [
            bool((mode_payload_checks.get("sandbox") or {}).get("sales_expectation_met")),
            bool((mode_payload_checks.get("sandbox") or {}).get("pos_expectation_met")),
            bool((mode_payload_checks.get("production") or {}).get("sales_expectation_met")),
            bool((mode_payload_checks.get("production") or {}).get("pos_expectation_met")),
            bool((mode_payload_checks.get("invalid_mode") or {}).get("sales_error_deterministic")),
            bool((mode_payload_checks.get("invalid_mode") or {}).get("pos_error_deterministic")),
            bool((mode_payload_checks.get("invalid_mode") or {}).get("submission_error_deterministic")),
        ]
    )

    decision = (
        "GO"
        if all([schema_ok, sales_ok, pos_ok, queue_ok, queue_level_ok, double_submit_ok, setup_ok, mode_payload_ok])
        else "NO-GO"
    )
    return {
        "decision": decision,
        "checks": {
            "schema_ok": schema_ok,
            "sales_valid_submission_ok": sales_ok,
            "pos_valid_submission_ok": pos_ok,
            "queue_retry_ok": queue_ok,
            "queue_level_dedupe_ok": queue_level_ok,
            "double_submit_integrity_ok": double_submit_ok,
            "setup_ok": setup_ok,
            "mode_payload_ok": mode_payload_ok,
            "warning_duplicate_check_ok_info": warning_duplicate_check_ok,
        },
    }


def _check_required_field_presence(setup_checks):
    presence = (setup_checks or {}).get("required_field_presence") or {}
    for dt_values in presence.values():
        if not all(dt_values.values()):
            return False
    return True


def _status_valid(result):
    if not isinstance(result, dict):
        return False
    status = (result.get("response") or {}).get("validationResponse", {}).get("status")
    return status == "Valid" and bool((result.get("response") or {}).get("invoiceNumber"))


def _to_markdown(results):
    go_no_go = results.get("go_no_go", {})
    lines = [
        f"# FBR Test Campaign Result ({results.get('run_id')})",
        "",
        f"- Site: `{results.get('site')}`",
        f"- Phase: `{results.get('phase')}`",
        f"- Decision: **{go_no_go.get('decision', 'NO-GO')}**",
        "",
        "## Decision Checks",
    ]
    for key, value in (go_no_go.get("checks") or {}).items():
        lines.append(f"- `{key}`: `{value}`")

    postfix = results.get("postfix") or {}
    warning_checks = postfix.get("warning_checks") or {}
    queue_level_checks = postfix.get("queue_level_checks") or {}
    double_submit_checks = postfix.get("double_submit_checks") or {}
    if warning_checks or queue_level_checks or double_submit_checks:
        lines.extend(
            [
                "",
                "## Additional Checks",
                f"- `warning_duplicate_transition_ok`: `{warning_checks.get('duplicate_warning_transition_ok')}`",
                f"- `queue_level_ok`: `{queue_level_checks.get('ok')}`",
                f"- `double_submit_ok`: `{double_submit_checks.get('ok')}`",
            ]
        )
    return "\n".join(lines) + "\n"
