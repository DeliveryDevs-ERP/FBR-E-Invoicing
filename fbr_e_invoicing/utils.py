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



def sync_provinces():
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
    url = "https://gw.fbr.gov.pk/pdi/v1/provinces"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
    }
    try:
        # 1. Make the GET request
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Raises error if status is 401, 500, etc.
        data = response.json()
        for item in data:
            province_name = item.get("stateProvinceDesc")
            if province_name:  
                frappe.get_doc(
                    {
                        "doctype": "Province",
                        "name": province_name,
                    }
                ).insert(ignore_permissions=True)

        frappe.db.commit()
        print(f"Successfully synced {len(data)} Province.")
    except requests.exceptions.RequestException as e:
        # Log connection/API errors
        frappe.log_error(f"FBR API Error: {str(e)}", "Province Sync Failed")
        print(f"API Error: {str(e)}")

    except Exception as e:
        # Log other python errors
        frappe.log_error(f"Sync Logic Error: {str(e)}", "Province Sync Failed")
        print(f"Logic Error: {str(e)}")


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
    
    frappe.db.commit()
    frappe.logger().info("FBR Sale Types populated successfully")

    