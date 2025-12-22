import frappe
import requests
import os


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
        print("WARNING: PRAL Access token not found. Skipping Sync.")
        return

    # --- Proceed with Sync ---
    url = "https://gw.fbr.gov.pk/pdi/v1/itemdesccode"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    try:
        # 1. Make the GET request
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Raises error if status is 401, 500, etc.

        data = response.json()
        # 2. Iterate through the list
        for item in data:
            # Extract fields using the EXACT keys from the image
            hs_code = item.get("hS_CODE")
            description = item.get("description")
            # 3. Insert into Frappe (Idempotent check)
            if hs_code:  # and not frappe.db.exists("HS Code", {"code": hs_code}):
                frappe.get_doc(
                    {
                        "doctype": "HS Code",
                        "code_number": hs_code,
                        "description": description,
                    }
                ).insert(ignore_permissions=True)

        frappe.db.commit()
        print(f"Successfully synced {len(data)} HS Codes.")
    except requests.exceptions.RequestException as e:
        # Log connection/API errors
        frappe.log_error(f"FBR API Error: {str(e)}", "HS Code Sync Failed")
        print(f"API Error: {str(e)}")

    except Exception as e:
        # Log other python errors
        frappe.log_error(f"Sync Logic Error: {str(e)}", "HS Code Sync Failed")
        print(f"Logic Error: {str(e)}")
