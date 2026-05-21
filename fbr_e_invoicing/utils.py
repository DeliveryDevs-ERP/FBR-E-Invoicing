import frappe
import requests
from frappe import _


HARDCODED_PROVINCES = [
    "PUNJAB",
    "SINDH",
    "KYBER PAKHTUNKHWA",
    "BALOCHISTAN",
    "CAPITAL TERRITORY",
    "GILGIT BALTISTAN",
    "AZAD JAMMU AND KASHMIR",
]

FBR_HS_CODE_URL = "https://gw.fbr.gov.pk/pdi/v1/itemdesccode"
FBR_UOM_URL = "https://gw.fbr.gov.pk/pdi/v1/uom"


def _resolve_company(company=None):
    """Pick the company to read FBR config from.

    When `company` is supplied (the common path for invoice posting), use it.
    Otherwise fall back to the Default Company from Global Defaults — used by
    global flows such as master-data sync.
    """
    return company or frappe.defaults.get_global_default("company")


def _get_company_fbr_field(company, fieldname):
    """Read a custom FBR field off the resolved Company, safely."""
    target = _resolve_company(company)
    if not target:
        return None
    if not frappe.db.has_column("Company", fieldname):
        return None
    try:
        return frappe.db.get_value("Company", target, fieldname)
    except Exception:
        return None


def _get_pral_token(company=None):
    """Return the PRAL Authorization Token for the resolved Company.

    Resolution: named Company → Default Company → `frappe.conf.PRAL_AUTHORIZATION_TOKEN`.
    """
    token = _get_company_fbr_field(company, "custom_fbr_authorization_token")
    if not token:
        token = frappe.conf.get("PRAL_AUTHORIZATION_TOKEN")
    return (token or "").strip()


def _get_fbr_endpoint(company=None):
    """Return the FBR API endpoint for the resolved Company."""
    endpoint = _get_company_fbr_field(company, "custom_fbr_api_endpoint")
    return (endpoint or "").strip()


def _get_fbr_mode(company=None):
    """Return the FBR Mode (Sandbox Testing / Production) for the resolved Company."""
    mode = _get_company_fbr_field(company, "custom_fbr_mode")
    return (mode or "").strip()


def _has_setup_field(fieldname):
    if not frappe.db.exists("DocType", "FBR E-Invoicing Setup"):
        return False
    try:
        return bool(frappe.get_meta("FBR E-Invoicing Setup").has_field(fieldname))
    except Exception:
        return False


def is_fbr_enabled(company=None):
    """Return True only if FBR is enabled on the resolved Company.

    Used as the kill switch in validation entry points. When off, all FBR
    validations for that company are skipped.
    """
    value = _get_company_fbr_field(company, "custom_fbr_enabled")
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return False


def sync_hs_codes():
    auth_token = _get_pral_token()
    if not auth_token:
        return {
            "success": False,
            "status_code": None,
            "response": "PRAL Access token not found. Skipping Sync.",
        }

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.get(FBR_HS_CODE_URL, headers=headers, timeout=30)
        if response.status_code != 200:
            frappe.log_error(
                f"FBR API Error [{response.status_code}]: {response.text}",
                "HS Code Sync Failed",
            )
            return {
                "success": False,
                "status_code": response.status_code,
                "response": response.text,
            }

        data = response.json()
        inserted_count = 0
        updated_count = 0
        for item in data:
            hs_code = item.get("hS_CODE")
            description = item.get("description")
            if not hs_code:
                continue

            if frappe.db.exists("HS Code", hs_code):
                frappe.db.set_value(
                    "HS Code",
                    hs_code,
                    "description",
                    description,
                    update_modified=False,
                )
                updated_count += 1
            else:
                frappe.get_doc(
                    {
                        "doctype": "HS Code",
                        "code_number": hs_code,
                        "description": description,
                    }
                ).insert(ignore_permissions=True)
                inserted_count += 1

        print(
            f"Successfully synced {len(data)} HS Codes. "
            f"Inserted: {inserted_count}, Updated: {updated_count}."
        )
        return {
            "success": True,
            "status_code": response.status_code,
            "response": (
                f"Successfully synced {len(data)} HS Codes. "
                f"Inserted: {inserted_count}, Updated: {updated_count}."
            ),
        }
    except requests.exceptions.RequestException as e:
        # Log connection/API errors
        frappe.log_error(f"FBR API Error: {str(e)}", "HS Code Sync Failed")
        print(f"API Error: {str(e)}")
        return {
            "success": False,
            "status_code": None,
            "response": str(e),
        }

    except Exception as e:
        # Log other python errors
        frappe.log_error(f"Sync Logic Error: {str(e)}", "HS Code Sync Failed")
        print(f"Logic Error: {str(e)}")
        return {
            "success": False,
            "status_code": None,
            "response": str(e),
        }


def sync_uoms(uom_payload=None):
    """Sync FBR-approved UOMs into ERPNext UOM as add/enable-only."""
    response = None
    if uom_payload is None:
        auth_token = _get_pral_token()
        if not auth_token:
            return {
                "success": False,
                "status_code": None,
                "response": "PRAL Access token not found. Skipping Sync.",
            }

        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.get(FBR_UOM_URL, headers=headers, timeout=30)
            if response.status_code != 200:
                frappe.log_error(
                    f"FBR API Error [{response.status_code}]: {response.text}",
                    "UOM Sync Failed",
                )
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "response": response.text,
                }
            data = response.json()
        except requests.exceptions.RequestException as e:
            frappe.log_error(f"FBR API Error: {str(e)}", "UOM Sync Failed")
            return {
                "success": False,
                "status_code": None,
                "response": str(e),
            }
    else:
        data = uom_payload

    try:
        if not isinstance(data, list):
            return {
                "success": False,
                "status_code": (response.status_code if response else None),
                "response": "Invalid UOM payload format: expected a list.",
            }

        fetched_count = len(data)
        inserted_count = 0
        enabled_count = 0
        skipped_count = 0

        seen = set()
        unique_uoms = []
        for item in data:
            if not isinstance(item, dict):
                skipped_count += 1
                continue

            description = (
                item.get("description")
                or item.get("uom")
                or item.get("uoM")
                or item.get("uom_name")
                or ""
            )
            description = str(description).strip()
            if not description:
                skipped_count += 1
                continue

            normalized = description.casefold()
            if normalized in seen:
                skipped_count += 1
                continue

            seen.add(normalized)
            unique_uoms.append(description)

        for description in unique_uoms:
            existing = frappe.db.get_value(
                "UOM",
                {"uom_name": description},
                ["name", "enabled"],
                as_dict=True,
            )
            if existing:
                if int(existing.enabled or 0) != 1:
                    frappe.db.set_value(
                        "UOM",
                        existing.name,
                        "enabled",
                        1,
                        update_modified=False,
                    )
                    enabled_count += 1
                else:
                    skipped_count += 1
                continue

            frappe.get_doc(
                {
                    "doctype": "UOM",
                    "uom_name": description,
                    "enabled": 1,
                }
            ).insert(ignore_permissions=True)
            inserted_count += 1

        summary = (
            f"Successfully synced UOMs. Fetched: {fetched_count}, "
            f"Unique: {len(unique_uoms)}, Inserted: {inserted_count}, "
            f"Enabled: {enabled_count}, Skipped: {skipped_count}."
        )
        print(summary)
        return {
            "success": True,
            "status_code": (response.status_code if response else 200),
            "response": summary,
            "fetched_count": fetched_count,
            "unique_count": len(unique_uoms),
            "inserted_count": inserted_count,
            "enabled_count": enabled_count,
            "skipped_count": skipped_count,
        }
    except Exception as e:
        frappe.log_error(f"Sync Logic Error: {str(e)}", "UOM Sync Failed")
        return {
            "success": False,
            "status_code": (response.status_code if response else None),
            "response": str(e),
        }


def populate_provinces():
    created = 0
    skipped = 0
    try:
        for province_name in HARDCODED_PROVINCES:
            if frappe.db.exists("Province", province_name):
                skipped += 1
                continue

            frappe.get_doc(
                {
                    "doctype": "Province",
                    "name": province_name,
                }
            ).insert(ignore_permissions=True)
            created += 1

        return {
            "success": True,
            "status_code": 200,
            "response": (
                f"Provinces ensured successfully. "
                f"Created: {created}, Existing: {skipped}."
            ),
        }
    except Exception as e:
        frappe.log_error(
            f"Province prepopulation failed: {str(e)}", "Province Population Failed"
        )
        return {
            "success": False,
            "status_code": None,
            "response": str(e),
        }


# Province sync from FBR API is intentionally disabled.
# Provinces are now populated through `populate_provinces()`.


@frappe.whitelist()
def run_master_data_sync():
    """Run HS Code + UOM sync and report failures in UI popup."""
    hs_result = sync_hs_codes()
    uom_result = sync_uoms()

    results = {
        "sync_hs_codes": hs_result or {},
        "sync_uoms": uom_result or {},
    }

    failed = []
    for function_name, result in results.items():
        status_code = result.get("status_code")
        if status_code != 200:
            failed.append(
                {
                    "function": function_name,
                    "status_code": status_code,
                    "response": result.get("response"),
                }
            )

    if _has_setup_field("master_data_retrieved"):
        frappe.db.set_single_value(
            "FBR E-Invoicing Setup",
            "master_data_retrieved",
            0 if failed else 1,
        )

    if failed:
        message_parts = []
        for item in failed:
            status_code = (
                item["status_code"] if item["status_code"] is not None else "N/A"
            )
            response = frappe.utils.escape_html(
                (item["response"] or "No response")[:1000]
            )
            message_parts.append(
                f"<b>{item['function']}</b><br>"
                f"Error Code: {status_code}<br>"
                f"Response: {response}"
            )

        popup_message = "<br><br>".join(message_parts)
        log_message = "\n\n".join(
            [
                f"{item['function']} | Error Code: {item['status_code'] if item['status_code'] is not None else 'N/A'} | Response: {(item['response'] or 'No response')[:2000]}"
                for item in failed
            ]
        )
        frappe.log_error(log_message, "FBR Master Data Sync Failed")
        frappe.msgprint(
            msg=popup_message,
            title="FBR Sync Error",
            indicator="red",
        )
        return {"success": False, "results": results}

    frappe.msgprint(
        msg="Master Data synced successfully (HS Codes + UOMs).",
        title="FBR Sync",
        indicator="green",
    )
    return {"success": True, "results": results}


def run_post_migrate_sync():
    """Post-migration sync for static Province and Pakistan tax setup."""
    populate_provinces()
    _ensure_pakistan_tax_accounts_and_templates()
    _migrate_legacy_fbr_setup_fields_to_default_company()


# Map legacy FBR E-Invoicing Setup Singles field -> Company custom field column
_LEGACY_FBR_SETUP_FIELD_MIGRATIONS = {
    "pral_authorization_token": "custom_fbr_authorization_token",
    "api_endpoint": "custom_fbr_api_endpoint",
    "mode": "custom_fbr_mode",
    "enabled": "custom_fbr_enabled",
}


def _migrate_legacy_fbr_setup_fields_to_default_company():
    """Move legacy Setup-level FBR config onto the Default Company.

    The following FBR E-Invoicing Setup Singles values are moved onto the
    Default Company's matching custom field, then deleted:

      pral_authorization_token -> Company.custom_fbr_authorization_token
      api_endpoint             -> Company.custom_fbr_api_endpoint
      mode                     -> Company.custom_fbr_mode
      enabled                  -> Company.custom_fbr_enabled

    Idempotent: only copies into a Company field that is currently empty/zero,
    and removes the Singles row regardless so subsequent migrates do nothing.
    Runs from the `after_migrate` hook so Custom Field columns are guaranteed
    to exist by the time we write to them.
    """
    default_company = frappe.defaults.get_global_default("company")
    if not default_company or not frappe.db.exists("Company", default_company):
        return

    for legacy_field, company_field in _LEGACY_FBR_SETUP_FIELD_MIGRATIONS.items():
        try:
            rows = frappe.db.sql(
                "SELECT `value` FROM `tabSingles` "
                "WHERE `doctype` = 'FBR E-Invoicing Setup' "
                "AND `field` = %s",
                (legacy_field,),
                as_list=True,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"FBR setup migration: read of legacy Singles row '{legacy_field}' failed",
            )
            continue

        if not rows:
            continue

        legacy_value = (rows[0][0] or "").strip()

        if legacy_value and frappe.db.has_column("Company", company_field):
            existing = frappe.db.get_value("Company", default_company, company_field)
            existing_str = (str(existing).strip() if existing is not None else "")
            # Don't clobber a value already set on the Company.
            if not existing_str or existing_str == "0":
                frappe.db.set_value(
                    "Company", default_company, company_field, legacy_value
                )

        try:
            frappe.db.sql(
                "DELETE FROM `tabSingles` "
                "WHERE `doctype` = 'FBR E-Invoicing Setup' "
                "AND `field` = %s",
                (legacy_field,),
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"FBR setup migration: cleanup of legacy Singles row '{legacy_field}' failed",
            )

    frappe.clear_cache(doctype="FBR E-Invoicing Setup")
    frappe.clear_cache(doctype="Company")


def _ensure_pakistan_tax_accounts_and_templates():
    """
    Create-only: ensure Pakistan tax accounts and templates exist for all Pakistan companies.
    Existing records are never modified, overwritten, or deleted.
    """
    try:
        from fbr_e_invoicing.coa_setup.overrides.company import (
            setup_pakistan_for_existing_companies,
        )

        result = setup_pakistan_for_existing_companies(ignore_permissions=True)
        created = result.get("accounts_created", 0) + result.get("item_templates_created", 0)
        skipped = result.get("accounts_skipped", 0) + result.get("item_templates_skipped", 0)
        if created or skipped:
            frappe.logger().info(
                f"Pakistan tax setup: {created} created, {skipped} skipped (existing left untouched)."
            )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Pakistan Tax Setup: Post-migrate auto-creation failed",
        )


def is_api_key_valid(company=None):
    auth_token = _get_pral_token(company)

    if not auth_token:
        return False

    url = "https://gw.fbr.gov.pk/pdi/v1/provinces"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            if _has_setup_field("is_api_token_valid"):
                frappe.db.set_single_value(
                    "FBR E-Invoicing Setup", "is_api_token_valid", 1
                )
            return True
        if response.status_code == 401:
            return False
        if response.status_code == 500:
            return "Internal Server Error"
        return False
    except requests.exceptions.RequestException:
        return False


@frappe.whitelist()
def get_fbr_setup_status():
    """Return non-sensitive setup completion flags for workspace guidance UI."""
    allowed_roles = {"System Manager", "Accounts Manager"}
    user_roles = set(frappe.get_roles(frappe.session.user))
    if not allowed_roles.intersection(user_roles):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    master_data_retrieved = (
        frappe.db.get_single_value("FBR E-Invoicing Setup", "master_data_retrieved")
        if _has_setup_field("master_data_retrieved")
        else 0
    )

    try:
        master_data_missing = int(master_data_retrieved or 0) != 1
    except (TypeError, ValueError):
        master_data_missing = True

    companies_missing_province = []
    if frappe.db.has_column("Company", "custom_province"):
        companies_missing_province = frappe.get_all(
            "Company",
            filters={"custom_province": ["in", ["", None]]},
            pluck="name",
        )
    province_missing = bool(companies_missing_province)

    default_company = frappe.defaults.get_global_default("company") or ""

    endpoint_missing = not _get_fbr_endpoint(default_company) if default_company else True
    token_missing = not _get_pral_token(default_company) if default_company else True
    enabled_missing = not is_fbr_enabled(default_company) if default_company else True

    return {
        "default_company": default_company,
        "endpoint_missing": endpoint_missing,
        "token_missing": token_missing,
        "enabled_missing": enabled_missing,
        "master_data_missing": master_data_missing,
        "province_missing": province_missing,
        "companies_missing_province": companies_missing_province,
        "show_instructions": (
            endpoint_missing
            or token_missing
            or enabled_missing
            or master_data_missing
            or province_missing
        ),
    }


@frappe.whitelist()
def get_fbr_valid_invoice_count_last_week():
    """Combined Valid invoice count across SI + POS submitted in the last 7 days.

    Used by the `Valid Invoices (Last Week)` Number Card on the Pak Compliance
    workspace.
    """
    return _count_invoices_by_fbr_status("Valid", days=7)


@frappe.whitelist()
def get_fbr_invalid_invoice_count_last_week():
    """Combined Invalid invoice count across SI + POS submitted in the last 7 days.

    Used by the `Invalid Invoices (Last Week)` Number Card on the Pak Compliance
    workspace.
    """
    return _count_invoices_by_fbr_status("Invalid", days=7)


def _count_invoices_by_fbr_status(status, days=None):
    """Sum of Sales Invoice + POS Invoice rows with the given FBR status.

    If `days` is given, only invoices with `posting_date` within the last that
    many days are counted.
    """
    filters = {"custom_fbr_status": status, "docstatus": 1}
    if days:
        cutoff = frappe.utils.add_days(frappe.utils.nowdate(), -int(days))
        filters["posting_date"] = [">=", cutoff]

    si = frappe.db.count("Sales Invoice", filters=filters)
    pos = frappe.db.count("POS Invoice", filters=filters)
    return (si or 0) + (pos or 0)


@frappe.whitelist()
def get_company_fbr_stats(company: str):
    """Return FBR-posted invoice counts for a Company, split by doctype.

    Counts only successfully-submitted invoices (`custom_fbr_status = "Valid"`).
    Used by the Company form's FBR Stats HTML field.
    """
    if not company:
        return {"sales_invoice": 0, "pos_invoice": 0, "total": 0}

    sales_invoice = frappe.db.count(
        "Sales Invoice",
        filters={
            "company": company,
            "custom_fbr_status": "Valid",
            "docstatus": 1,
        },
    )
    pos_invoice = frappe.db.count(
        "POS Invoice",
        filters={
            "company": company,
            "custom_fbr_status": "Valid",
            "docstatus": 1,
        },
    )
    return {
        "company": company,
        "sales_invoice": sales_invoice,
        "pos_invoice": pos_invoice,
        "total": sales_invoice + pos_invoice,
    }


def create_fbr_sale_types():
    """Create default FBR Sale Type records based on FBR scenarios."""

    sale_types = [
        {
            "scenario_id": "SN001",
            "name": "Goods at standard rate (default) - Registered Buyer",
        },
        {
            "scenario_id": "SN002",
            "name": "Goods at standard rate (default) - Unregistered Buyer",
        },
        {"scenario_id": "SN003", "name": "Steel melting and re-rolling"},
        {"scenario_id": "SN004", "name": "Ship breaking"},
        {"scenario_id": "SN005", "name": "Goods at Reduced Rate"},
        {"scenario_id": "SN006", "name": "Exempt goods"},
        {"scenario_id": "SN007", "name": "Goods at zero-rate"},
        {"scenario_id": "SN008", "name": "3rd Schedule Goods"},
        {"scenario_id": "SN009", "name": "Cotton ginners"},
        {"scenario_id": "SN010", "name": "Telecommunication services"},
        {"scenario_id": "SN011", "name": "Toll Manufacturing"},
        {"scenario_id": "SN012", "name": "Petroleum Products"},
        {"scenario_id": "SN013", "name": "Electricity Supply to Retailers"},
        {"scenario_id": "SN014", "name": "Gas to CNG stations"},
        {"scenario_id": "SN015", "name": "Mobile Phones"},
        {"scenario_id": "SN016", "name": "Processing/Conversion of Goods"},
        {"scenario_id": "SN017", "name": "Goods (FED in ST Mode)"},
        {"scenario_id": "SN018", "name": "Services (FED in ST Mode)"},
        {"scenario_id": "SN019", "name": "Services"},
        {"scenario_id": "SN020", "name": "Electric Vehicle"},
        {"scenario_id": "SN021", "name": "Cement /Concrete Block"},
        {"scenario_id": "SN022", "name": "Potassium Chlorate"},
        {"scenario_id": "SN023", "name": "CNG Sales"},
        {"scenario_id": "SN024", "name": "Goods as per SRO.297(I)/2023"},
        {"scenario_id": "SN025", "name": "Non-Adjustable Supplies"},
        {"scenario_id": "SN026","name": "Goods at standard rate (default) - End Consumer",},
        {"scenario_id": "SN027", "name": "3rd Schedule Goods - End Consumer"},
        {"scenario_id": "SN028", "name": "Goods at Reduced Rate - End Consumer"},
    ]

    for sale_type in sale_types:
        try:
            if not frappe.db.exists("FBR Sale Type", sale_type["name"]):
                doc = frappe.get_doc(
                    {
                        "doctype": "FBR Sale Type",
                        "name": sale_type["name"],
                        "scenario_id": sale_type["scenario_id"],
                    }
                )
                doc.insert(ignore_permissions=True)
                frappe.logger().info(f"Created FBR Sale Type: {sale_type['name']}")
            else:
                # Update scenario_id if record exists but scenario might be different
                frappe.db.set_value(
                    "FBR Sale Type",
                    sale_type["name"],
                    "scenario_id",
                    sale_type["scenario_id"],
                )
        except Exception as e:
            frappe.logger().error(
                f"Error creating FBR Sale Type {sale_type['name']}: {str(e)}"
            )

    frappe.logger().info("FBR Sale Types populated successfully")



