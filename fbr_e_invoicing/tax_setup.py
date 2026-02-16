import frappe
from erpnext.setup.setup_wizard.operations.taxes_setup import get_or_create_tax_group


PROVINCES = (
    {"name": "PUNJAB", "code": "PUN", "services_rate": 16.0},
    {"name": "SINDH", "code": "SND", "services_rate": 13.0},
    {"name": "KYBER PAKHTUNKHWA", "code": "KPK", "services_rate": 15.0},
    {"name": "BALOCHISTAN", "code": "BAL", "services_rate": 15.0},
    {"name": "CAPITAL TERRITORY", "code": "ICT", "services_rate": 16.0},
    {"name": "GILGIT BALTISTAN", "code": "GB", "services_rate": 16.0},
    {"name": "AZAD JAMMU AND KASHMIR", "code": "AJK", "services_rate": 16.0},
)

DEFAULT_GOODS_ST_RATE = 18.0
DEFAULT_WHT_RATE = 0.0
DEFAULT_FURTHER_TAX_RATE = 0.0

PROVINCE_TO_TAX_CATEGORY = {
    "PUNJAB": "PUNJAB",
    "SINDH": "SINDH",
    "KYBER PAKHTUNKHWA": "KYBER PAKHTUNKHWA",
    "BALOCHISTAN": "BALOCHISTAN",
    "CAPITAL TERRITORY": "CAPITAL TERRITORY",
    "GILGIT BALTISTAN": "GILGIT BALTISTAN",
    "AZAD JAMMU AND KASHMIR": "AZAD JAMMU AND KASHMIR",
    "Punjab": "PUNJAB",
    "Sindh": "SINDH",
    "Kyber Pakhtunkhwa": "KYBER PAKHTUNKHWA",
    "Khyber Pakhtunkhwa": "KYBER PAKHTUNKHWA",
    "Balochistan": "BALOCHISTAN",
    "Capital Territory": "CAPITAL TERRITORY",
    "Islamabad": "CAPITAL TERRITORY",
    "Gilgit Baltistan": "GILGIT BALTISTAN",
    "Azad Jammu and Kashmir": "AZAD JAMMU AND KASHMIR",
    "AJK": "AZAD JAMMU AND KASHMIR",
}

PROVINCE_TAX_CATEGORIES = frozenset(PROVINCE_TO_TAX_CATEGORY.values())


def _get_company_abbr(company):
    return frappe.db.get_value("Company", company, "abbr") or ""


def _get_tax_group(company, root_type):
    try:
        return get_or_create_tax_group(company, root_type)
    except Exception as e:
        frappe.log_error(
            f"Unable to resolve tax group for company {company} ({root_type}): {e}",
            "FBR Tax Setup",
        )
        return None


def _create_tax_account(company, account_name, parent_account, root_type):
    abbr = _get_company_abbr(company)
    full_name = f"{account_name} - {abbr}"

    if frappe.db.exists("Account", full_name):
        return full_name, False

    try:
        doc = frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": account_name,
                "parent_account": parent_account,
                "company": company,
                "is_group": 0,
                "account_type": "Tax",
                "root_type": root_type,
                "report_type": "Balance Sheet",
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_root_company_validation = True
        doc.insert(ignore_if_duplicate=True, ignore_mandatory=True)
        return doc.name, True
    except Exception as e:
        frappe.log_error(
            f"Unable to create tax account {account_name} for {company}: {e}",
            "FBR Tax Setup",
        )
        return None, False


def _create_item_tax_template(company, title, tax_account, tax_rate):
    existing = frappe.db.get_value(
        "Item Tax Template",
        {"title": title, "company": company},
        "name",
    )
    if existing:
        return existing, False

    try:
        doc = frappe.get_doc(
            {
                "doctype": "Item Tax Template",
                "title": title,
                "company": company,
                "taxes": [
                    {
                        "tax_type": tax_account,
                        "tax_rate": tax_rate,
                    }
                ],
            }
        )
        doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
        return doc.name, True
    except Exception as e:
        frappe.log_error(
            f"Unable to create Item Tax Template {title} for {company}: {e}",
            "FBR Tax Setup",
        )
        return None, False


def _create_taxes_template(doctype, company, title, tax_category, rows):
    existing = frappe.db.get_value(
        doctype,
        {"title": title, "company": company},
        "name",
    )
    if existing:
        return existing, False

    cost_center = frappe.db.get_value("Company", company, "cost_center")
    template_rows = []
    for row in rows:
        template_row = {
            "charge_type": row.get("charge_type", "On Net Total"),
            "category": row.get("category", "Total"),
            "account_head": row["account_head"],
            "rate": row["rate"],
            "description": row.get("description"),
        }
        if doctype == "Purchase Taxes and Charges Template":
            template_row["add_deduct_tax"] = row.get("add_deduct_tax", "Add")
        if cost_center:
            template_row["cost_center"] = cost_center
        template_rows.append(template_row)

    try:
        doc = frappe.get_doc(
            {
                "doctype": doctype,
                "title": title,
                "company": company,
                "tax_category": tax_category,
                "taxes": template_rows,
            }
        )
        doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
        return doc.name, True
    except Exception as e:
        frappe.log_error(
            f"Unable to create template {title} ({doctype}) for {company}: {e}",
            "FBR Tax Setup",
        )
        return None, False


def _template_row(account_head, rate, description, add_deduct_tax=None):
    row = {
        "account_head": account_head,
        "rate": rate,
        "description": description,
        "charge_type": "On Net Total",
        "category": "Total",
    }
    if add_deduct_tax:
        row["add_deduct_tax"] = add_deduct_tax
    return row


def resolve_tax_category(province_name):
    if not province_name:
        return None

    province_name = province_name.strip()
    candidates = (
        PROVINCE_TO_TAX_CATEGORY.get(province_name),
        PROVINCE_TO_TAX_CATEGORY.get(province_name.upper()),
        province_name,
        province_name.upper(),
    )

    for tax_category in candidates:
        if tax_category and frappe.db.exists("Tax Category", tax_category):
            return tax_category

    return None


def _ensure_tax_category(province_name):
    tax_category = resolve_tax_category(province_name)
    if tax_category:
        return tax_category

    candidate = (
        PROVINCE_TO_TAX_CATEGORY.get((province_name or "").strip())
        or (province_name or "").strip().upper()
    )
    if not candidate:
        return None

    try:
        frappe.get_doc(
            {
                "doctype": "Tax Category",
                "title": candidate,
            }
        ).insert(ignore_permissions=True, ignore_if_duplicate=True)
        return candidate
    except Exception as e:
        frappe.log_error(
            f"Unable to create Tax Category {candidate}: {e}",
            "FBR Tax Setup",
        )
        return None


def set_customer_tax_category(doc, method=None):
    province = (doc.get("custom_province") or "").strip()
    if not province:
        return

    tax_category = resolve_tax_category(province)
    if not tax_category:
        return

    current_tax_category = (doc.get("tax_category") or "").strip()
    if not current_tax_category:
        doc.tax_category = tax_category
        return

    if (
        current_tax_category != tax_category
        and current_tax_category in PROVINCE_TAX_CATEGORIES
    ):
        doc.tax_category = tax_category


def _backfill_customer_tax_categories():
    updated = 0
    customers = frappe.get_all(
        "Customer",
        filters={"custom_province": ["is", "set"]},
        fields=["name", "custom_province", "tax_category"],
        limit_page_length=0,
    )
    for customer in customers:
        if customer.tax_category:
            continue
        tax_category = resolve_tax_category(customer.custom_province)
        if not tax_category:
            continue
        frappe.db.set_value(
            "Customer",
            customer.name,
            "tax_category",
            tax_category,
            update_modified=False,
        )
        updated += 1

    return updated


def _setup_company_tax_artifacts(company):
    stats = {
        "accounts_created": 0,
        "accounts_skipped": 0,
        "item_templates_created": 0,
        "item_templates_skipped": 0,
        "sales_templates_created": 0,
        "sales_templates_skipped": 0,
        "purchase_templates_created": 0,
        "purchase_templates_skipped": 0,
    }

    liability_group = _get_tax_group(company, "Liability")
    asset_group = _get_tax_group(company, "Asset")
    if not liability_group or not asset_group:
        return stats

    for province in PROVINCES:
        province_name = province["name"]
        province_code = province["code"]
        services_rate = province["services_rate"]

        account_specs = (
            ("sales_services", f"Sales Tax on Services - {province_code}", liability_group, "Liability"),
            ("sales_goods", f"Sales Tax on Goods - {province_code}", liability_group, "Liability"),
            ("wht", f"WHT - {province_code}", liability_group, "Liability"),
            ("further_tax", f"Further Tax - {province_code}", liability_group, "Liability"),
            ("purchase_services", f"Purchase Tax on Services - {province_code}", asset_group, "Asset"),
            ("purchase_goods", f"Purchase Tax on Goods - {province_code}", asset_group, "Asset"),
        )

        accounts = {}
        for key, account_name, parent_account, root_type in account_specs:
            account, created = _create_tax_account(
                company=company,
                account_name=account_name,
                parent_account=parent_account,
                root_type=root_type,
            )
            if not account:
                continue
            accounts[key] = account
            if created:
                stats["accounts_created"] += 1
            else:
                stats["accounts_skipped"] += 1

        item_templates = (
            (
                f"Item Tax Services - {province_code}",
                accounts.get("sales_services"),
                services_rate,
            ),
            (
                f"Item Tax Goods - {province_code}",
                accounts.get("sales_goods"),
                DEFAULT_GOODS_ST_RATE,
            ),
        )
        for title, account_head, rate in item_templates:
            if not account_head:
                continue
            _, created = _create_item_tax_template(company, title, account_head, rate)
            if created:
                stats["item_templates_created"] += 1
            else:
                stats["item_templates_skipped"] += 1

        tax_category = _ensure_tax_category(province_name)
        if not tax_category:
            frappe.log_error(
                f"Tax Category not found for province {province_name}. Skipping taxes templates.",
                "FBR Tax Setup",
            )
            continue

        sales_templates = (
            (
                f"Sales Tax on Services - {province_code}",
                [
                    _template_row(
                        accounts.get("sales_services"),
                        services_rate,
                        f"Sales Tax on Services - {province_name} @ {services_rate}%",
                    )
                ],
            ),
            (
                f"Sales Tax on Goods - {province_code}",
                [
                    _template_row(
                        accounts.get("sales_goods"),
                        DEFAULT_GOODS_ST_RATE,
                        f"Sales Tax on Goods (Federal) @ {DEFAULT_GOODS_ST_RATE}%",
                    )
                ],
            ),
            (
                f"SST + WHT + Further Tax - {province_code}",
                [
                    _template_row(
                        accounts.get("sales_services"),
                        services_rate,
                        f"Sales Tax on Services - {province_name} @ {services_rate}%",
                    ),
                    _template_row(
                        accounts.get("wht"),
                        DEFAULT_WHT_RATE,
                        f"WHT - {province_name}",
                    ),
                    _template_row(
                        accounts.get("further_tax"),
                        DEFAULT_FURTHER_TAX_RATE,
                        f"Further Tax - {province_name}",
                    ),
                ],
            ),
        )
        for title, rows in sales_templates:
            if any(not row["account_head"] for row in rows):
                continue
            _, created = _create_taxes_template(
                doctype="Sales Taxes and Charges Template",
                company=company,
                title=title,
                tax_category=tax_category,
                rows=rows,
            )
            if created:
                stats["sales_templates_created"] += 1
            else:
                stats["sales_templates_skipped"] += 1

        purchase_templates = (
            (
                f"Purchase Tax on Services - {province_code}",
                [
                    _template_row(
                        accounts.get("purchase_services"),
                        services_rate,
                        f"Purchase Tax on Services - {province_name} @ {services_rate}%",
                        add_deduct_tax="Add",
                    )
                ],
            ),
            (
                f"Purchase Tax on Goods - {province_code}",
                [
                    _template_row(
                        accounts.get("purchase_goods"),
                        DEFAULT_GOODS_ST_RATE,
                        f"Purchase Tax on Goods - {province_name} @ {DEFAULT_GOODS_ST_RATE}%",
                        add_deduct_tax="Add",
                    )
                ],
            ),
        )
        for title, rows in purchase_templates:
            if any(not row["account_head"] for row in rows):
                continue
            _, created = _create_taxes_template(
                doctype="Purchase Taxes and Charges Template",
                company=company,
                title=title,
                tax_category=tax_category,
                rows=rows,
            )
            if created:
                stats["purchase_templates_created"] += 1
            else:
                stats["purchase_templates_skipped"] += 1

    return stats


def setup_fbr_tax_artifacts():
    companies = frappe.get_all(
        "Company",
        filters={"country": "Pakistan"},
        pluck="name",
    )
    if not companies:
        return {"companies_processed": 0, "customers_updated": 0}

    aggregate = {
        "companies_processed": 0,
        "accounts_created": 0,
        "accounts_skipped": 0,
        "item_templates_created": 0,
        "item_templates_skipped": 0,
        "sales_templates_created": 0,
        "sales_templates_skipped": 0,
        "purchase_templates_created": 0,
        "purchase_templates_skipped": 0,
    }

    for company in companies:
        stats = _setup_company_tax_artifacts(company)
        aggregate["companies_processed"] += 1
        for key in (
            "accounts_created",
            "accounts_skipped",
            "item_templates_created",
            "item_templates_skipped",
            "sales_templates_created",
            "sales_templates_skipped",
            "purchase_templates_created",
            "purchase_templates_skipped",
        ):
            aggregate[key] += stats[key]

    aggregate["customers_updated"] = _backfill_customer_tax_categories()
    return aggregate
