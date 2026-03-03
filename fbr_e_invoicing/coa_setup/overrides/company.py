from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

APP_NAME = "fbr_e_invoicing"
ALLOWED_SETUP_ROLES = {"System Manager", "Accounts Manager"}


def get_data_file_path(file_name: str) -> str:
    return frappe.get_app_path(APP_NAME, "data", file_name)


def _get_tax_account_defaults() -> dict[str, Any]:
    return frappe.get_file_json(get_data_file_path("pakistan_tax_accounts.json"))


def _get_item_tax_template_defaults() -> dict[str, Any]:
    return frappe.get_file_json(get_data_file_path("item_tax_template_defaults.json"))


def _get_province_tax_charge_template_defaults() -> dict[str, Any]:
    return frappe.get_file_json(
        get_data_file_path("province_tax_charge_template_defaults.json")
    )


def _get_tax_withholding_defaults() -> dict[str, Any]:
    return frappe.get_file_json(get_data_file_path("tax_withholding_defaults.json"))


def _current_template_year() -> int:
    return now_datetime().year


def _build_province_template_title(
    province_label: str, template_suffix: str, year: int
) -> str:
    return f"{province_label} {template_suffix} {year}"


def _resolve_company_tax_account(
    company: str, account_name: str, expected_rate: float
) -> tuple[str | None, str | None]:
    accounts = frappe.get_all(
        "Account",
        filters={"company": company, "account_name": account_name},
        fields=["name", "account_type", "is_group", "tax_rate"],
    )

    if len(accounts) != 1:
        return (
            None,
            _("Expected exactly one account named {0} in company {1}. Found: {2}").format(
                frappe.bold(account_name), frappe.bold(company), len(accounts)
            ),
        )

    account = accounts[0]
    if (account.get("account_type") or "").strip() != "Tax":
        return (
            None,
            _("Account {0} must have Account Type = Tax").format(
                frappe.bold(account.get("name"))
            ),
        )

    if int(account.get("is_group") or 0) != 0:
        return (
            None,
            _("Account {0} must be a ledger account (is_group = 0)").format(
                frappe.bold(account.get("name"))
            ),
        )

    actual_rate = flt(account.get("tax_rate"))
    if abs(actual_rate - flt(expected_rate)) > 0.0001:
        return (
            None,
            _("Account {0} tax rate mismatch. Expected {1}, found {2}").format(
                frappe.bold(account.get("name")), flt(expected_rate), actual_rate
            ),
        )

    return account.get("name"), None


def _resolve_company_account_by_name(
    company: str, account_name: str, preferred_root_type: str | None = None
) -> tuple[str | None, str | None]:
    accounts = frappe.get_all(
        "Account",
        filters={"company": company, "account_name": account_name},
        fields=["name", "is_group", "root_type"],
    )

    accounts = [account for account in accounts if int(account.get("is_group") or 0) == 0]

    if preferred_root_type and len(accounts) > 1:
        preferred_accounts = [
            account
            for account in accounts
            if (account.get("root_type") or "").strip() == preferred_root_type
        ]
        if len(preferred_accounts) == 1:
            return preferred_accounts[0].get("name"), None

        if len(preferred_accounts) > 1:
            accounts = preferred_accounts

    if len(accounts) != 1:
        return (
            None,
            _("Expected exactly one account named {0} in company {1}. Found: {2}").format(
                frappe.bold(account_name), frappe.bold(company), len(accounts)
            ),
        )

    return accounts[0].get("name"), None


def _ensure_province_tax_charge_template(
    company: str,
    row: dict[str, Any],
    summary: dict[str, Any],
    current_year: int,
    doctype: str,
    template_suffix: str,
    account_name_field: str,
    rate_field: str,
    created_key: str,
    skipped_key: str,
):
    tax_category = (row.get("tax_category") or "").strip()
    province_label = (row.get("province_label") or "").strip()
    account_name = (row.get(account_name_field) or "").strip()
    expected_rate = flt(row.get(rate_field))

    if not tax_category or not province_label or not account_name:
        summary["errors"].append(
            _("Incomplete province template config for {0}").format(
                frappe.bold(row.get("tax_category") or row.get("province_label") or "Unknown")
            )
        )
        return

    title = _build_province_template_title(province_label, template_suffix, current_year)

    if frappe.db.get_value(doctype, {"company": company, "title": title}, "name"):
        summary[skipped_key] += 1
        return

    account, account_error = _resolve_company_tax_account(
        company=company,
        account_name=account_name,
        expected_rate=expected_rate,
    )
    if account_error:
        summary["errors"].append(
            _("{0}: {1}").format(frappe.bold(title), account_error)
        )
        return

    try:
        doc = frappe.get_doc(
            {
                "doctype": doctype,
                "title": title,
                "company": company,
                "tax_category": tax_category,
                "taxes": [
                    {
                        "charge_type": "On Net Total",
                        "account_head": account,
                        "description": f"{province_label} {template_suffix}",
                        "rate": expected_rate,
                    }
                ],
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert(ignore_if_duplicate=True)
        summary[created_key] += 1
    except frappe.DuplicateEntryError:
        summary[skipped_key] += 1
    except Exception:
        summary["errors"].append(
            _("Failed to create {0} {1}").format(doctype, title)
        )
        frappe.log_error(
            frappe.get_traceback(),
            "Pakistan Tax Setup: Province Tax Charge Template Creation",
        )


def _is_pakistan_company(company: str) -> bool:
    country = frappe.db.get_value("Company", company, "country")
    return _normalize_country(country) == "pakistan"


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


def _normalize_country(value: str | None) -> str:
    return (value or "").strip().lower()


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


def ensure_pakistan_province_tax_charge_templates(company: str) -> dict[str, Any]:
    summary = {
        "sales_templates_created": 0,
        "sales_templates_skipped": 0,
        "purchase_templates_created": 0,
        "purchase_templates_skipped": 0,
        "tax_categories_created": 0,
        "tax_categories_skipped": 0,
        "errors": [],
    }

    if not _is_pakistan_company(company):
        return summary

    defaults = _get_province_tax_charge_template_defaults()
    province_rows = defaults.get("province_tax_charge_templates", [])
    current_year = _current_template_year()

    for category_name in {
        (row.get("tax_category") or "").strip() for row in province_rows
    }:
        if not category_name:
            continue

        if frappe.db.exists("Tax Category", category_name):
            summary["tax_categories_skipped"] += 1
            continue

        try:
            category_doc = frappe.get_doc(
                {
                    "doctype": "Tax Category",
                    "name": category_name,
                    "title": category_name,
                }
            )
            category_doc.flags.ignore_permissions = True
            category_doc.insert(ignore_if_duplicate=True)
            summary["tax_categories_created"] += 1
        except frappe.DuplicateEntryError:
            summary["tax_categories_skipped"] += 1
        except Exception:
            summary["errors"].append(
                _("Failed to create Tax Category {0}").format(category_name)
            )
            frappe.log_error(
                frappe.get_traceback(),
                "Pakistan Tax Setup: Tax Category Creation",
            )

    for row in province_rows:
        _ensure_province_tax_charge_template(
            company=company,
            row=row,
            summary=summary,
            current_year=current_year,
            doctype="Sales Taxes and Charges Template",
            template_suffix="Sales Tax",
            account_name_field="sales_account_name",
            rate_field="sales_rate",
            created_key="sales_templates_created",
            skipped_key="sales_templates_skipped",
        )
        _ensure_province_tax_charge_template(
            company=company,
            row=row,
            summary=summary,
            current_year=current_year,
            doctype="Purchase Taxes and Charges Template",
            template_suffix="Purchase Tax",
            account_name_field="purchase_account_name",
            rate_field="purchase_rate",
            created_key="purchase_templates_created",
            skipped_key="purchase_templates_skipped",
        )

    return summary


def ensure_tax_withholding_groups() -> dict[str, Any]:
    summary = {
        "withholding_groups_created": 0,
        "withholding_groups_skipped": 0,
        "errors": [],
    }

    defaults = _get_tax_withholding_defaults()
    groups = defaults.get("tax_withholding_groups", [])

    for row in groups:
        group_name = (row or "").strip()
        if not group_name:
            continue

        if frappe.db.get_value(
            "Tax Withholding Group", {"group_name": group_name}, "name"
        ):
            summary["withholding_groups_skipped"] += 1
            continue

        try:
            doc = frappe.get_doc(
                {
                    "doctype": "Tax Withholding Group",
                    "group_name": group_name,
                }
            )
            doc.flags.ignore_permissions = True
            doc.insert(ignore_if_duplicate=True)
            summary["withholding_groups_created"] += 1
        except frappe.DuplicateEntryError:
            summary["withholding_groups_skipped"] += 1
        except Exception:
            summary["errors"].append(
                _("Failed to create Tax Withholding Group {0}").format(group_name)
            )
            frappe.log_error(
                frappe.get_traceback(),
                "Pakistan Tax Setup: Tax Withholding Group Creation",
            )

    return summary


def _has_matching_withholding_rate(doc, rate: dict[str, Any]) -> bool:
    for existing in doc.get("rates") or []:
        if (
            (existing.tax_withholding_group or "").strip()
            == (rate.get("tax_withholding_group") or "").strip()
            and str(existing.from_date or "") == str(rate.get("from_date") or "")
            and str(existing.to_date or "") == str(rate.get("to_date") or "")
            and abs(flt(existing.tax_withholding_rate) - flt(rate.get("tax_withholding_rate")))
            < 0.0001
            and abs(flt(existing.cumulative_threshold) - flt(rate.get("cumulative_threshold")))
            < 0.0001
            and abs(flt(existing.single_threshold) - flt(rate.get("single_threshold")))
            < 0.0001
        ):
            return True
    return False


def _find_withholding_account_for_company(
    company: str, account_name: str, category_name: str
) -> tuple[str | None, str | None]:
    preferred_root_type = None
    normalized_category_name = (category_name or "").strip().lower()
    if "(purchases)" in normalized_category_name:
        preferred_root_type = "Liability"
    elif "(sales)" in normalized_category_name:
        preferred_root_type = "Asset"

    account, error = _resolve_company_account_by_name(
        company, account_name, preferred_root_type
    )
    if error:
        return (
            None,
            _("Tax Withholding Category {0}: {1}").format(
                frappe.bold(category_name), error
            ),
        )

    return account, None


def ensure_tax_withholding_categories(company: str) -> dict[str, Any]:
    summary = {
        "withholding_categories_created": 0,
        "withholding_categories_skipped": 0,
        "withholding_category_accounts_linked": 0,
        "withholding_category_accounts_skipped": 0,
        "withholding_category_rates_added": 0,
        "errors": [],
    }

    if not _is_pakistan_company(company):
        return summary

    defaults = _get_tax_withholding_defaults()
    categories = defaults.get("tax_withholding_categories", [])

    for row in categories:
        category_name = (row.get("name") or "").strip()
        account_name = (row.get("account_name") or "").strip()
        rate = row.get("rate") or {}

        if not category_name or not account_name:
            summary["errors"].append(
                _("Incomplete Tax Withholding Category config for {0}").format(
                    frappe.bold(category_name or "Unknown")
                )
            )
            continue

        account, account_error = _find_withholding_account_for_company(
            company=company,
            account_name=account_name,
            category_name=category_name,
        )
        if account_error:
            summary["errors"].append(account_error)
            continue

        existing_name = frappe.db.get_value(
            "Tax Withholding Category", {"name": category_name}, "name"
        )

        if not existing_name:
            try:
                doc = frappe.get_doc(
                    {
                        "doctype": "Tax Withholding Category",
                        "name": category_name,
                        "category_name": row.get("category_name") or "",
                        "tax_deduction_basis": row.get("tax_deduction_basis")
                        or "Net Total",
                        "round_off_tax_amount": int(row.get("round_off_tax_amount") or 0),
                        "tax_on_excess_amount": int(row.get("tax_on_excess_amount") or 0),
                        "disable_cumulative_threshold": int(
                            row.get("disable_cumulative_threshold") or 0
                        ),
                        "disable_transaction_threshold": int(
                            row.get("disable_transaction_threshold") or 0
                        ),
                        "rates": [rate],
                        "accounts": [{"company": company, "account": account}],
                    }
                )
                doc.flags.ignore_permissions = True
                doc.insert(ignore_if_duplicate=True)
                summary["withholding_categories_created"] += 1
                continue
            except frappe.DuplicateEntryError:
                existing_name = category_name
            except Exception:
                summary["errors"].append(
                    _("Failed to create Tax Withholding Category {0}").format(category_name)
                )
                frappe.log_error(
                    frappe.get_traceback(),
                    "Pakistan Tax Setup: Tax Withholding Category Creation",
                )
                continue

        summary["withholding_categories_skipped"] += 1

        try:
            doc = frappe.get_doc("Tax Withholding Category", existing_name)
            changed = False

            if rate and not _has_matching_withholding_rate(doc, rate):
                doc.append("rates", rate)
                summary["withholding_category_rates_added"] += 1
                changed = True

            company_rows = [
                row_ for row_ in (doc.get("accounts") or []) if row_.company == company
            ]
            if not company_rows:
                doc.append("accounts", {"company": company, "account": account})
                summary["withholding_category_accounts_linked"] += 1
                changed = True
            elif any((row_.account or "").strip() == account for row_ in company_rows):
                summary["withholding_category_accounts_skipped"] += 1
            else:
                summary["errors"].append(
                    _(
                        "Tax Withholding Category {0} already has a different account for company {1}"
                    ).format(frappe.bold(category_name), frappe.bold(company))
                )

            if changed:
                doc.flags.ignore_permissions = True
                doc.save(ignore_permissions=True)
        except Exception:
            summary["errors"].append(
                _("Failed to update Tax Withholding Category {0}").format(category_name)
            )
            frappe.log_error(
                frappe.get_traceback(),
                "Pakistan Tax Setup: Tax Withholding Category Update",
            )

    return summary


def ensure_tax_withholding_masters(company: str) -> dict[str, Any]:
    summary = {
        "withholding_groups_created": 0,
        "withholding_groups_skipped": 0,
        "withholding_categories_created": 0,
        "withholding_categories_skipped": 0,
        "withholding_category_accounts_linked": 0,
        "withholding_category_accounts_skipped": 0,
        "withholding_category_rates_added": 0,
        "errors": [],
    }

    if not _is_pakistan_company(company):
        return summary

    group_summary = ensure_tax_withholding_groups()
    category_summary = ensure_tax_withholding_categories(company)

    summary["withholding_groups_created"] = group_summary["withholding_groups_created"]
    summary["withholding_groups_skipped"] = group_summary["withholding_groups_skipped"]
    summary["withholding_categories_created"] = category_summary[
        "withholding_categories_created"
    ]
    summary["withholding_categories_skipped"] = category_summary[
        "withholding_categories_skipped"
    ]
    summary["withholding_category_accounts_linked"] = category_summary[
        "withholding_category_accounts_linked"
    ]
    summary["withholding_category_accounts_skipped"] = category_summary[
        "withholding_category_accounts_skipped"
    ]
    summary["withholding_category_rates_added"] = category_summary[
        "withholding_category_rates_added"
    ]
    summary["errors"] = group_summary["errors"] + category_summary["errors"]
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
        "sales_templates_created": 0,
        "sales_templates_skipped": 0,
        "purchase_templates_created": 0,
        "purchase_templates_skipped": 0,
        "withholding_groups_created": 0,
        "withholding_groups_skipped": 0,
        "withholding_categories_created": 0,
        "withholding_categories_skipped": 0,
        "withholding_category_accounts_linked": 0,
        "withholding_category_accounts_skipped": 0,
        "withholding_category_rates_added": 0,
        "errors": [],
    }

    if not _is_pakistan_company(company):
        return result

    account_summary = ensure_pakistan_tax_accounts(company)
    template_summary = ensure_pakistan_item_tax_templates(company)
    province_template_summary = ensure_pakistan_province_tax_charge_templates(company)
    withholding_summary = ensure_tax_withholding_masters(company)

    result["accounts_created"] = account_summary["accounts_created"]
    result["accounts_skipped"] = account_summary["accounts_skipped"]
    result["item_templates_created"] = template_summary["item_templates_created"]
    result["item_templates_skipped"] = template_summary["item_templates_skipped"]
    result["sales_templates_created"] = province_template_summary["sales_templates_created"]
    result["sales_templates_skipped"] = province_template_summary["sales_templates_skipped"]
    result["purchase_templates_created"] = province_template_summary[
        "purchase_templates_created"
    ]
    result["purchase_templates_skipped"] = province_template_summary[
        "purchase_templates_skipped"
    ]
    result["withholding_groups_created"] = withholding_summary[
        "withholding_groups_created"
    ]
    result["withholding_groups_skipped"] = withholding_summary[
        "withholding_groups_skipped"
    ]
    result["withholding_categories_created"] = withholding_summary[
        "withholding_categories_created"
    ]
    result["withholding_categories_skipped"] = withholding_summary[
        "withholding_categories_skipped"
    ]
    result["withholding_category_accounts_linked"] = withholding_summary[
        "withholding_category_accounts_linked"
    ]
    result["withholding_category_accounts_skipped"] = withholding_summary[
        "withholding_category_accounts_skipped"
    ]
    result["withholding_category_rates_added"] = withholding_summary[
        "withholding_category_rates_added"
    ]
    result["errors"] = (
        account_summary["errors"]
        + template_summary["errors"]
        + province_template_summary["errors"]
        + withholding_summary["errors"]
    )

    return result


@frappe.whitelist()
def setup_pakistan_for_existing_companies(ignore_permissions: bool = False) -> dict[str, Any]:
    if not ignore_permissions:
        _ensure_setup_permissions()

    companies = frappe.db.sql(
        """
        select name
        from `tabCompany`
        where lower(trim(ifnull(country, ''))) = 'pakistan'
        order by lft asc
        """,
        as_list=True,
    )
    companies = [row[0] for row in companies]

    summary = {
        "companies_processed": len(companies),
        "accounts_created": 0,
        "accounts_skipped": 0,
        "item_templates_created": 0,
        "item_templates_skipped": 0,
        "sales_templates_created": 0,
        "sales_templates_skipped": 0,
        "purchase_templates_created": 0,
        "purchase_templates_skipped": 0,
        "withholding_groups_created": 0,
        "withholding_groups_skipped": 0,
        "withholding_categories_created": 0,
        "withholding_categories_skipped": 0,
        "withholding_category_accounts_linked": 0,
        "withholding_category_accounts_skipped": 0,
        "withholding_category_rates_added": 0,
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
        summary["sales_templates_created"] += company_result["sales_templates_created"]
        summary["sales_templates_skipped"] += company_result["sales_templates_skipped"]
        summary["purchase_templates_created"] += company_result["purchase_templates_created"]
        summary["purchase_templates_skipped"] += company_result["purchase_templates_skipped"]
        summary["withholding_groups_created"] += company_result[
            "withholding_groups_created"
        ]
        summary["withholding_groups_skipped"] += company_result[
            "withholding_groups_skipped"
        ]
        summary["withholding_categories_created"] += company_result[
            "withholding_categories_created"
        ]
        summary["withholding_categories_skipped"] += company_result[
            "withholding_categories_skipped"
        ]
        summary["withholding_category_accounts_linked"] += company_result[
            "withholding_category_accounts_linked"
        ]
        summary["withholding_category_accounts_skipped"] += company_result[
            "withholding_category_accounts_skipped"
        ]
        summary["withholding_category_rates_added"] += company_result[
            "withholding_category_rates_added"
        ]
        summary["errors"].extend(company_result["errors"])

    summary["success"] = len(summary["errors"]) == 0
    return summary


def on_company_update_setup_pakistan(doc, method=None):
    if _normalize_country(getattr(doc, "country", None)) != "pakistan":
        return

    if not getattr(doc, "name", None):
        return

    setup_pakistan_tax_accounts_and_item_templates(doc.name, ignore_permissions=True)


def on_company_after_insert_setup_pakistan(doc, method=None):
    if _normalize_country(getattr(doc, "country", None)) != "pakistan":
        return

    if not getattr(doc, "name", None):
        return

    setup_pakistan_tax_accounts_and_item_templates(doc.name, ignore_permissions=True)
