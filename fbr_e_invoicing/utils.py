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


def sync_hs_codes():
    auth_token = None
    # 1. Try DocType
    if frappe.db.exists("DocType", "FBR E-Inv Setup"):
        auth_token = frappe.db.get_single_value(
            "FBR E-Inv Setup", "pral_authorization_token"
        )
    if not auth_token:
        auth_token = frappe.conf.get("PRAL_AUTHORIZATION_TOKEN")
    # 3. Fail Gracefully
    if not auth_token:
        # print("WARNING: PRAL Access token not found. Skipping Sync.")
        return {
            "success": False,
            "status_code": None,
            "response": "PRAL Access token not found. Skipping Sync.",
        }

    # --- Proceed with Sync ---
    url = "https://gw.fbr.gov.pk/pdi/v1/itemdesccode"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    try:
        # 1. Make the GET request
        response = requests.get(url, headers=headers)
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
        # 2. Iterate through the list
        for item in data:
            # Extract fields using the EXACT keys from the image
            hs_code = item.get("hS_CODE")
            description = item.get("description")
            # 3. Insert into Frappe (Idempotent check)
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

        if frappe.db.exists("DocType", "FBR E-Inv Setup"):
            frappe.db.set_single_value("FBR E-Inv Setup", "hs_codes_retrieved", 1)

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
        frappe.log_error(f"Province prepopulation failed: {str(e)}", "Province Population Failed")
        return {
            "success": False,
            "status_code": None,
            "response": str(e),
        }


# Province sync from FBR API is intentionally disabled.
# Provinces are now populated through `populate_provinces()`.


@frappe.whitelist()
def run_master_data_sync():
    """Run HS Code sync and report failures in UI popup."""
    hs_result = sync_hs_codes()

    results = {
        "sync_hs_codes": hs_result or {},
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

    if failed:
        message_parts = []
        for item in failed:
            status_code = item["status_code"] if item["status_code"] is not None else "N/A"
            response = frappe.utils.escape_html((item["response"] or "No response")[:1000])
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
        msg="HS Codes synced successfully.",
        title="FBR Sync",
        indicator="green",
    )
    return {"success": True, "results": results}


def run_post_migrate_sync():
    """Post-migration sync for static Province data and HS Codes."""
    populate_provinces()
    sync_hs_codes()
    from fbr_e_invoicing.tax_setup import setup_fbr_tax_artifacts

    setup_fbr_tax_artifacts()


def is_api_key_valid():
    auth_token = None
    if frappe.db.exists("DocType", "FBR E-Inv Setup"):
        auth_token = frappe.db.get_single_value(
            "FBR E-Inv Setup", "pral_authorization_token"
        )

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
    hs_codes_retrieved = frappe.db.get_single_value(
        "FBR E-Inv Setup", "hs_codes_retrieved"
    )

    endpoint_missing = not api_endpoint
    token_missing = not token
    try:
        hs_codes_missing = int(hs_codes_retrieved or 0) != 1
    except (TypeError, ValueError):
        hs_codes_missing = True

    return {
        "endpoint_missing": endpoint_missing,
        "token_missing": token_missing,
        "hs_codes_missing": hs_codes_missing,
        "show_instructions": endpoint_missing or token_missing or hs_codes_missing,
    }


def create_fbr_sale_types():
    """Create default FBR Sale Type records based on FBR scenarios."""
    
    sale_types = [
        {"scenario_id": "SN001", "name": "Goods at Standard Rate (default)"},
        {"scenario_id": "SN002", "name": "Goods at Standard Rate (default)"},
        {"scenario_id": "SN003", "name": "Steel Melting and re-rolling"},
        {"scenario_id": "SN004", "name": "Ship breaking"},
        {"scenario_id": "SN005", "name": "Goods at Reduced Rate"},
        {"scenario_id": "SN006", "name": "Exempt Goods"},
        {"scenario_id": "SN007", "name": "Goods at zero-rate"},
        {"scenario_id": "SN008", "name": "3rd Schedule Goods"},
        {"scenario_id": "SN009", "name": "Cotton Ginners"},
        {"scenario_id": "SN010", "name": "Telecommunication services"},
        {"scenario_id": "SN011", "name": "Toll Manufacturing"},
        {"scenario_id": "SN012", "name": "Petroleum Products"},
        {"scenario_id": "SN013", "name": "Electricity Supply to Retailers"},
        {"scenario_id": "SN014", "name": "Gas to CNG stations"},
        {"scenario_id": "SN015", "name": "Mobile Phones"},
        {"scenario_id": "SN016", "name": "Processing/ Conversion of Goods"},
        {"scenario_id": "SN017", "name": "Goods (FED in ST Mode)"},
        {"scenario_id": "SN018", "name": "Services (FED in ST Mode)"},
        {"scenario_id": "SN019", "name": "Services"},
        {"scenario_id": "SN020", "name": "Electric Vehicle"},
        {"scenario_id": "SN021", "name": "Cement /Concrete Block"},
        {"scenario_id": "SN022", "name": "Potassium Chlorate"},
        {"scenario_id": "SN023", "name": "CNG Sales"},
        {"scenario_id": "SN024", "name": "Goods as per SRO.297(|)/2023"},
        {"scenario_id": "SN025", "name": "Non-Adjustable Supplies"},
        {"scenario_id": "SN026", "name": "Goods at Standard Rate (default)"},
        {"scenario_id": "SN027", "name": "3rd Schedule Goods"},
        {"scenario_id": "SN028", "name": "Goods at Reduced Rate"},
    ]
    
    for sale_type in sale_types:
        try:
            if not frappe.db.exists("FBR Sale Type", sale_type["name"]):
                doc = frappe.get_doc({
                    "doctype": "FBR Sale Type",
                    "name": sale_type["name"],
                    "scenario_id": sale_type["scenario_id"]
                })
                doc.insert(ignore_permissions=True)
                frappe.logger().info(f"Created FBR Sale Type: {sale_type['name']}")
            else:
                # Update scenario_id if record exists but scenario might be different
                frappe.db.set_value(
                    "FBR Sale Type", 
                    sale_type["name"], 
                    "scenario_id", 
                    sale_type["scenario_id"]
                )
        except Exception as e:
            frappe.logger().error(f"Error creating FBR Sale Type {sale_type['name']}: {str(e)}")
    
    frappe.logger().info("FBR Sale Types populated successfully")
