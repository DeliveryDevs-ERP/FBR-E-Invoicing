import frappe
import requests


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


def _get_pral_token():
    auth_token = None
    if frappe.db.exists("DocType", "FBR E-Inv Setup"):
        auth_token = frappe.db.get_single_value(
            "FBR E-Inv Setup", "pral_authorization_token"
        )
    if not auth_token:
        auth_token = frappe.conf.get("PRAL_AUTHORIZATION_TOKEN")
    return (auth_token or "").strip()


def _has_setup_field(fieldname):
    if not frappe.db.exists("DocType", "FBR E-Inv Setup"):
        return False
    try:
        return bool(frappe.get_meta("FBR E-Inv Setup").has_field(fieldname))
    except Exception:
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
            "FBR E-Inv Setup",
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
                f"{item['function']} | Error Code: {item['status_code'] if item['status_code'] is not None else 'N/A'} | "
                f"Response: {(item['response'] or 'No response')[:2000]}"
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
    """Post-migration sync for static Province, HS Code, and UOM master data."""
    populate_provinces()
    hs_result = sync_hs_codes() or {}
    uom_result = sync_uoms() or {}
    master_ok = (
        hs_result.get("status_code") == 200 and uom_result.get("status_code") == 200
    )
    if _has_setup_field("master_data_retrieved"):
        frappe.db.set_single_value(
            "FBR E-Inv Setup", "master_data_retrieved", 1 if master_ok else 0
        )
    from fbr_e_invoicing.tax_setup import setup_fbr_tax_artifacts

    setup_fbr_tax_artifacts()


def is_api_key_valid():
    auth_token = _get_pral_token()

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
            frappe.db.set_single_value("FBR E-Inv Setup", "is_api_token_valid", 1)
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
        frappe.throw("Not permitted", frappe.PermissionError)

    api_endpoint = (
        frappe.db.get_single_value("FBR E-Inv Setup", "api_endpoint") or ""
    ).strip()
    token = (
        frappe.db.get_single_value("FBR E-Inv Setup", "pral_authorization_token") or ""
    ).strip()
    master_data_retrieved = (
        frappe.db.get_single_value("FBR E-Inv Setup", "master_data_retrieved")
        if _has_setup_field("master_data_retrieved")
        else 0
    )

    endpoint_missing = not api_endpoint
    token_missing = not token
    try:
        master_data_missing = int(master_data_retrieved or 0) != 1
    except (TypeError, ValueError):
        master_data_missing = True

    return {
        "endpoint_missing": endpoint_missing,
        "token_missing": token_missing,
        "master_data_missing": master_data_missing,
        "show_instructions": endpoint_missing or token_missing or master_data_missing,
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
        {
            "scenario_id": "SN026",
            "name": "Goods at Standard Rate (default) - End Consumer",
        },
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
