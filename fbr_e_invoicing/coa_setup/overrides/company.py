from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt

APP_NAME = "fbr_e_invoicing"
ALLOWED_SETUP_ROLES = {"System Manager", "Accounts Manager"}


def get_data_file_path(file_name: str) -> str:
    return frappe.get_app_path(APP_NAME, "data", file_name)


def _get_tax_account_defaults() -> dict[str, Any]:
    return frappe.get_file_json(get_data_file_path("pakistan_tax_accounts.json"))


def _get_item_tax_template_defaults() -> dict[str, Any]:
    return frappe.get_file_json(get_data_file_path("item_tax_template_defaults.json"))


def _is_pakistan_company(company: str) -> bool:
    country = frappe.db.get_value("Company", company, "country")
    return (country or "").strip().lower() == "pakistan"


def _report_type(root_type: str | None) -> str:
    if root_type in ("Asset", "Liability", "Equity"):
        return "Balance Sheet"
    return "Profit and Loss"


def _find_existing_account(
    company: str,
    account_name: str,
    root_type: str | None,
    is_group: int,
    parent_account: str | None = None,
) -> str | None:
    base_filters = {
        "company": company,
        "account_name": account_name,
        "is_group": int(is_group),
    }
    if root_type:
        base_filters["root_type"] = root_type

    if parent_account:
        filters = dict(base_filters)
        filters["parent_account"] = parent_account
        if existing := frappe.db.get_value("Account", filters, "name"):
            return existing

    return frappe.db.get_value("Account", base_filters, "name")


def _ensure_setup_permissions():
    user_roles = set(frappe.get_roles(frappe.session.user))
    if not user_roles.intersection(ALLOWED_SETUP_ROLES):
        frappe.throw(_("Not permitted"), frappe.PermissionError)


def ensure_pakistan_tax_accounts(company: str) -> dict[str, Any]:
    summary = {
        "accounts_created": 0,
        "accounts_skipped": 0,
        "errors": [],
        "account_by_key": {},
    }

    if not _is_pakistan_company(company):
        return summary

    defaults = _get_tax_account_defaults()
    accounts = defaults.get("accounts", [])
    account_by_key = summary["account_by_key"]
    account_def_by_key = {row.get("key"): row for row in accounts}
    company_currency = frappe.db.get_value("Company", company, "default_currency")

    for row in accounts:
        key = row.get("key")
        parent_key = row.get("parent_key")
        parent_account = account_by_key.get(parent_key) if parent_key else None

        if parent_key and not parent_account:
            parent_def = account_def_by_key.get(parent_key)
            if parent_def:
                parent_account = _find_existing_account(
                    company=company,
                    account_name=parent_def.get("account_name"),
                    root_type=parent_def.get("root_type"),
                    is_group=parent_def.get("is_group", 0),
                )
                if parent_account:
                    account_by_key[parent_key] = parent_account

        if parent_key and not parent_account:
            summary["errors"].append(
                _("Parent account missing for {0}").format(row.get("account_name"))
            )
            continue

        existing = _find_existing_account(
            company=company,
            account_name=row.get("account_name"),
            root_type=row.get("root_type"),
            is_group=row.get("is_group", 0),
            parent_account=parent_account,
        )
        if existing:
            account_by_key[key] = existing
            summary["accounts_skipped"] += 1
            continue

        doc_data = {
            "doctype": "Account",
            "company": company,
            "account_name": row.get("account_name"),
            "is_group": int(row.get("is_group") or 0),
            "root_type": row.get("root_type"),
            "report_type": row.get("report_type") or _report_type(row.get("root_type")),
            "account_currency": company_currency,
        }
        if parent_account:
            doc_data["parent_account"] = parent_account
        if row.get("account_type"):
            doc_data["account_type"] = row.get("account_type")
        if row.get("account_number"):
            doc_data["account_number"] = row.get("account_number")
        if row.get("account_type") == "Tax":
            doc_data["tax_rate"] = flt(row.get("tax_rate"))

        try:
            doc = frappe.get_doc(doc_data)
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.flags.ignore_root_company_validation = True
            doc.insert(ignore_if_duplicate=True)
            account_by_key[key] = doc.name
            summary["accounts_created"] += 1
        except frappe.DuplicateEntryError:
            existing = _find_existing_account(
                company=company,
                account_name=row.get("account_name"),
                root_type=row.get("root_type"),
                is_group=row.get("is_group", 0),
                parent_account=parent_account,
            )
            if existing:
                account_by_key[key] = existing
                summary["accounts_skipped"] += 1
            else:
                summary["errors"].append(
                    _("Duplicate account conflict for {0}").format(row.get("account_name"))
                )
        except Exception:
            summary["errors"].append(
                _("Failed to create account {0}").format(row.get("account_name"))
            )
            frappe.log_error(
                frappe.get_traceback(),
                "Pakistan Tax Setup: Account Creation",
            )

    return summary


def ensure_pakistan_item_tax_templates(company: str) -> dict[str, Any]:
    summary = {
        "item_templates_created": 0,
        "item_templates_skipped": 0,
        "errors": [],
    }

    if not _is_pakistan_company(company):
        return summary

    defaults = _get_item_tax_template_defaults()
    templates = defaults.get("item_tax_templates", [])

    for row in templates:
        title = row.get("title")
        if not title:
            continue

        if frappe.db.get_value(
            "Item Tax Template", {"title": title, "company": company}, "name"
        ):
            summary["item_templates_skipped"] += 1
            continue

        account_name = row.get("account_name")
        account = frappe.db.get_value(
            "Account",
            {
                "company": company,
                "account_name": account_name,
            },
            "name",
        )
        if not account:
            summary["errors"].append(
                _("Account not found for Item Tax Template {0}").format(title)
            )
            continue

        try:
            doc = frappe.get_doc(
                {
                    "doctype": "Item Tax Template",
                    "title": title,
                    "company": company,
                    "taxes": [
                        {
                            "tax_type": account,
                            "tax_rate": flt(row.get("tax_rate")),
                        }
                    ],
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_links = True
            doc.flags.ignore_validate = True
            doc.insert(ignore_if_duplicate=True)
            summary["item_templates_created"] += 1
        except frappe.DuplicateEntryError:
            summary["item_templates_skipped"] += 1
        except Exception:
            summary["errors"].append(
                _("Failed to create Item Tax Template {0}").format(title)
            )
            frappe.log_error(
                frappe.get_traceback(),
                "Pakistan Tax Setup: Item Tax Template Creation",
            )

    return summary


@frappe.whitelist()
def setup_pakistan_tax_accounts_and_item_templates(
    company: str, ignore_permissions: bool = False
) -> dict[str, Any]:
    if not frappe.db.exists("Company", company):
        frappe.throw(_("Company {0} not found").format(company))
    if not ignore_permissions:
        frappe.has_permission("Company", ptype="write", doc=company, throw=True)

    result = {
        "company": company,
        "accounts_created": 0,
        "accounts_skipped": 0,
        "item_templates_created": 0,
        "item_templates_skipped": 0,
        "errors": [],
    }

    if not _is_pakistan_company(company):
        return result

    account_summary = ensure_pakistan_tax_accounts(company)
    template_summary = ensure_pakistan_item_tax_templates(company)

    result["accounts_created"] = account_summary["accounts_created"]
    result["accounts_skipped"] = account_summary["accounts_skipped"]
    result["item_templates_created"] = template_summary["item_templates_created"]
    result["item_templates_skipped"] = template_summary["item_templates_skipped"]
    result["errors"] = account_summary["errors"] + template_summary["errors"]

    return result


@frappe.whitelist()
def setup_pakistan_for_existing_companies(ignore_permissions: bool = False) -> dict[str, Any]:
    if not ignore_permissions:
        _ensure_setup_permissions()

    companies = frappe.get_all(
        "Company",
        filters={"country": "Pakistan"},
        pluck="name",
        order_by="lft asc",
    )

    summary = {
        "companies_processed": len(companies),
        "accounts_created": 0,
        "accounts_skipped": 0,
        "item_templates_created": 0,
        "item_templates_skipped": 0,
        "errors": [],
        "company_summaries": [],
    }

    for company in companies:
        company_result = setup_pakistan_tax_accounts_and_item_templates(
            company, ignore_permissions=True
        )
        summary["company_summaries"].append(company_result)
        summary["accounts_created"] += company_result["accounts_created"]
        summary["accounts_skipped"] += company_result["accounts_skipped"]
        summary["item_templates_created"] += company_result["item_templates_created"]
        summary["item_templates_skipped"] += company_result["item_templates_skipped"]
        summary["errors"].extend(company_result["errors"])

    summary["success"] = len(summary["errors"]) == 0
    return summary


def on_company_update_setup_pakistan(doc, method=None):
    if getattr(doc, "country", None) != "Pakistan":
        return

    if not getattr(doc, "name", None):
        return

    setup_pakistan_tax_accounts_and_item_templates(doc.name, ignore_permissions=True)
