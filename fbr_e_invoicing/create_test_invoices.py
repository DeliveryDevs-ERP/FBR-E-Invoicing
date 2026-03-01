"""
FBR E-Invoicing Test Data Creator
=================================

This module builds a deterministic test dataset for FBR E-Invoicing:
- Selects exactly 15 expected-valid + 5 expected-invalid candidates for each doctype.
- Enforces at least 2 SN002 no-ID buyer candidates in Sales and POS sets.
- Inserts draft Sales Invoice and POS Invoice documents only (no submit, no queue).

Usage:
  ./bench --site test.local execute create_test_invoices.run
  ./bench --site test.local execute create_test_invoices.cleanup
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import frappe
import requests
from frappe.utils import flt, nowdate, now_datetime


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DEFAULT_COMPANY = "Falcon"
DEFAULT_COMPANY_TAX_ID = "2868087"
DEFAULT_COUNTRY = "Pakistan"
DEFAULT_PRICE_LIST = "Standard Selling"
DEFAULT_COMPANY_BUYER_NTN = "8646347"
DEFAULT_INDIVIDUAL_BUYER_CNIC = "3130395896547"
DEFAULT_TARGET_VALID = 15
DEFAULT_TARGET_INVALID = 5
DEFAULT_MIN_SN002_NO_ID = 2
DEFAULT_SALES_TAX_RATE = 18.0
TEST_MARKER = "[FBRTEST]"
TEST_ITEM_PREFIX = "FBRTEST-"

STANDARD_SCENARIO_IDS = {"SN001", "SN002", "SN026"}
REDUCED_SCENARIO_IDS = {"SN005", "SN028"}
THIRD_SCENARIO_IDS = {"SN008", "SN027"}


# -----------------------------------------------------------------------------
# Master Data Definitions
# -----------------------------------------------------------------------------

CUSTOMERS = [
    {
        "key": "alpha",
        "customer_name": "Alpha Trading Co",
        "customer_type": "Company",
        "customer_group": "Commercial",
        "territory": "Pakistan",
        "tax_id": DEFAULT_COMPANY_BUYER_NTN,
        "ntn": DEFAULT_COMPANY_BUYER_NTN,
        "nic": "",
        "custom_province": "PUNJAB",
        "address": {
            "address_line1": "123 Mall Road",
            "city": "Lahore",
            "state": "Punjab",
            "pincode": "54000",
            "country": DEFAULT_COUNTRY,
        },
    },
    {
        "key": "beta",
        "customer_name": "Beta Industries Ltd",
        "customer_type": "Company",
        "customer_group": "Commercial",
        "territory": "Pakistan",
        "tax_id": DEFAULT_COMPANY_BUYER_NTN,
        "ntn": DEFAULT_COMPANY_BUYER_NTN,
        "nic": "",
        "custom_province": "SINDH",
        "address": {
            "address_line1": "45 I.I. Chundrigar Road",
            "city": "Karachi",
            "state": "Sindh",
            "pincode": "74000",
            "country": DEFAULT_COUNTRY,
        },
    },
    {
        "key": "imran",
        "customer_name": "Imran Ahmed",
        "customer_type": "Individual",
        "customer_group": "Individual",
        "territory": "Pakistan",
        "tax_id": "",
        "ntn": "",
        "nic": DEFAULT_INDIVIDUAL_BUYER_CNIC,
        "custom_province": "CAPITAL TERRITORY",
        "address": {
            "address_line1": "House 12 Street 5 F-8/3",
            "city": "Islamabad",
            "state": "Capital Territory",
            "pincode": "44000",
            "country": DEFAULT_COUNTRY,
        },
    },
    {
        "key": "walkin",
        "customer_name": "Walk-in Customer",
        "customer_type": "Individual",
        "customer_group": "Individual",
        "territory": "Pakistan",
        "tax_id": "",
        "ntn": "",
        "nic": "",
        "custom_province": "SINDH",
        "address": {
            "address_line1": "POS Counter",
            "city": "Karachi",
            "state": "Sindh",
            "pincode": "75500",
            "country": DEFAULT_COUNTRY,
        },
    },
    {
        "key": "no_id",
        "customer_name": "No ID Buyer",
        "customer_type": "Individual",
        "customer_group": "Individual",
        "territory": "Pakistan",
        "tax_id": "",
        "ntn": "",
        "nic": "",
        "custom_province": "SINDH",
        "address": {
            "address_line1": "Shop 7 Tariq Road",
            "city": "Karachi",
            "state": "Sindh",
            "pincode": "75400",
            "country": DEFAULT_COUNTRY,
        },
    },
]

CUSTOMER_BY_KEY = {row["key"]: row for row in CUSTOMERS}


ITEMS = [
    {
        "item_code": "FBRTEST-LAPTOP-NPU",
        "item_name": "Laptop Computer",
        "item_group": "Products",
        "stock_uom": "Numbers, pieces, units",
        "custom_hs_code": "8471.3010",
        "default_sale_type": "Goods at standard rate (default) - Registered Buyer",
        "value_excl_st": 150000.0,
    },
    {
        "item_code": "FBRTEST-SHIRT-NPU",
        "item_name": "Cotton Shirt",
        "item_group": "Products",
        "stock_uom": "Numbers, pieces, units",
        "custom_hs_code": "6105.1000",
        "default_sale_type": "Goods at standard rate (default) - Registered Buyer",
        "value_excl_st": 2500.0,
    },
    {
        "item_code": "FBRTEST-PHONE-NPU",
        "item_name": "Smartphone",
        "item_group": "Products",
        "stock_uom": "Numbers, pieces, units",
        "custom_hs_code": "8517.1390",
        "default_sale_type": "Goods at standard rate (default) - Registered Buyer",
        "value_excl_st": 85000.0,
    },
    {
        "item_code": "FBRTEST-RICE-KG",
        "item_name": "Basmati Rice",
        "item_group": "Products",
        "stock_uom": "KG",
        "custom_hs_code": "1006.3010",
        "default_sale_type": "Goods at standard rate (default) - Registered Buyer",
        "value_excl_st": 500.0,
    },
    {
        "item_code": "FBRTEST-SOAP-KG",
        "item_name": "Toilet Soap",
        "item_group": "Products",
        "stock_uom": "KG",
        "custom_hs_code": "3401.1100",
        "default_sale_type": "Goods at standard rate (default) - Registered Buyer",
        "value_excl_st": 150.0,
    },
    {
        "item_code": "FBRTEST-TYRE-NPU",
        "item_name": "Car Tyre",
        "item_group": "Products",
        "stock_uom": "Numbers, pieces, units",
        "custom_hs_code": "4011.1000",
        "default_sale_type": "3rd Schedule Goods",
        "value_excl_st": 12000.0,
    },
    {
        "item_code": "FBRTEST-PETROL-LTR",
        "item_name": "Motor Spirit Petrol (Liter)",
        "item_group": "Products",
        "stock_uom": "Liter",
        "custom_hs_code": "2710.1210",
        "default_sale_type": "Petroleum Products",
        "value_excl_st": 280.0,
    },
    {
        "item_code": "FBRTEST-PETROL-MT",
        "item_name": "Motor Spirit Petrol (MT)",
        "item_group": "Products",
        "stock_uom": "MT",
        "custom_hs_code": "2710.1210",
        "default_sale_type": "Petroleum Products",
        "value_excl_st": 280000.0,
    },
    {
        "item_code": "FBRTEST-COTTON-KG",
        "item_name": "Cotton Yarn",
        "item_group": "Products",
        "stock_uom": "KG",
        "custom_hs_code": "5205.1100",
        "default_sale_type": "Cotton ginners",
        "value_excl_st": 450.0,
    },
    {
        "item_code": "FBRTEST-MEDICINE-NPU",
        "item_name": "Amoxicillin Capsules",
        "item_group": "Products",
        "stock_uom": "Numbers, pieces, units",
        "custom_hs_code": "3004.1010",
        "default_sale_type": "Goods at Reduced Rate",
        "value_excl_st": 350.0,
    },
    {
        "item_code": "FBRTEST-MEDICINE-KG",
        "item_name": "Medicine Bulk",
        "item_group": "Products",
        "stock_uom": "KG",
        "custom_hs_code": "3004.1010",
        "default_sale_type": "Goods at Reduced Rate",
        "value_excl_st": 350.0,
    },
    {
        "item_code": "FBRTEST-CEMENT-BAG",
        "item_name": "Portland Cement Bag",
        "item_group": "Products",
        "stock_uom": "Bag",
        "custom_hs_code": "2523.2900",
        "default_sale_type": "Cement /Concrete Block",
        "value_excl_st": 1250.0,
    },
    {
        "item_code": "FBRTEST-EV-NPU",
        "item_name": "Electric Vehicle",
        "item_group": "Products",
        "stock_uom": "Numbers, pieces, units",
        "custom_hs_code": "8703.8030",
        "default_sale_type": "Electric Vehicle",
        "value_excl_st": 650000.0,
    },
    {
        "item_code": "FBRTEST-CNG-KG",
        "item_name": "CNG Sales (KG)",
        "item_group": "Products",
        "stock_uom": "KG",
        "custom_hs_code": "2711.2100",
        "default_sale_type": "CNG Sales",
        "value_excl_st": 210.0,
    },
    {
        "item_code": "FBRTEST-CNG-MMBTU",
        "item_name": "CNG Sales (MMBTU)",
        "item_group": "Products",
        "stock_uom": "MMBTU",
        "custom_hs_code": "2711.2100",
        "default_sale_type": "CNG Sales",
        "value_excl_st": 210.0,
    },
]

ITEM_BY_CODE = {row["item_code"]: row for row in ITEMS}


# -----------------------------------------------------------------------------
# Candidate Matrix
# -----------------------------------------------------------------------------


def _candidate(
    candidate_id: str,
    scenario_id: str,
    sale_type_doctype: str,
    item_code: str,
    customer_key: str,
    *,
    rate: str = "18%",
    quantity: float = 1.0,
    value_excl_st: float | None = None,
    fixed_price: float = 0.0,
    notes: str = "",
    is_expected_valid: bool = True,
    requires_no_id_buyer: bool = False,
    doctype_targets: tuple[str, ...] = ("Sales", "POS"),
) -> Dict[str, Any]:
    item = ITEM_BY_CODE[item_code]
    return {
        "id": candidate_id,
        "scenario_id": scenario_id,
        "sale_type_doctype": sale_type_doctype,
        "item_code": item_code,
        "customer_key": customer_key,
        "rate": rate,
        "quantity": flt(quantity),
        "value_excl_st": flt(value_excl_st if value_excl_st is not None else item["value_excl_st"]),
        "fixed_price": flt(fixed_price),
        "notes": notes,
        "is_expected_valid": bool(is_expected_valid),
        "requires_no_id_buyer": bool(requires_no_id_buyer),
        "doctype_targets": tuple(doctype_targets),
    }


VALID_CANDIDATES = [
    # SN001 (Registered)
    _candidate("V001-SN001-LAPTOP", "SN001", "Goods at standard rate (default) - Registered Buyer", "FBRTEST-LAPTOP-NPU", "beta", notes="User matrix"),
    _candidate("V002-SN001-SHIRT", "SN001", "Goods at standard rate (default) - Registered Buyer", "FBRTEST-SHIRT-NPU", "beta", notes="User matrix"),
    _candidate("V003-SN001-PHONE", "SN001", "Goods at standard rate (default) - Registered Buyer", "FBRTEST-PHONE-NPU", "beta", notes="User matrix"),
    _candidate("V004-SN001-RICE", "SN001", "Goods at standard rate (default) - Registered Buyer", "FBRTEST-RICE-KG", "beta", notes="User matrix"),
    _candidate("V005-SN001-SOAP", "SN001", "Goods at standard rate (default) - Registered Buyer", "FBRTEST-SOAP-KG", "beta", notes="User matrix"),

    # SN002 (Unregistered) with NO ID buyer explicitly required
    _candidate("V006-SN002-LAPTOP-NOID", "SN002", "Goods at standard rate (default) - Unregistered Buyer", "FBRTEST-LAPTOP-NPU", "no_id", requires_no_id_buyer=True, notes="SN002 explicit no NTN/CNIC"),
    _candidate("V007-SN002-SHIRT-NOID", "SN002", "Goods at standard rate (default) - Unregistered Buyer", "FBRTEST-SHIRT-NPU", "no_id", requires_no_id_buyer=True, notes="SN002 explicit no NTN/CNIC"),
    _candidate("V008-SN002-SOAP-NOID", "SN002", "Goods at standard rate (default) - Unregistered Buyer", "FBRTEST-SOAP-KG", "no_id", requires_no_id_buyer=True, notes="SN002 explicit no NTN/CNIC"),
    _candidate("V009-SN002-LAPTOP-WALKIN", "SN002", "Goods at standard rate (default) - Unregistered Buyer", "FBRTEST-LAPTOP-NPU", "walkin", requires_no_id_buyer=True),
    _candidate("V010-SN002-SHIRT-WALKIN", "SN002", "Goods at standard rate (default) - Unregistered Buyer", "FBRTEST-SHIRT-NPU", "walkin", requires_no_id_buyer=True),

    # Additional known valid scenarios
    _candidate("V011-SN008-TYRE", "SN008", "3rd Schedule Goods", "FBRTEST-TYRE-NPU", "beta", fixed_price=12000.0, notes="fixedPrice=12000"),
    _candidate("V012-SN012-PETROL-LITER", "SN012", "Petroleum Products", "FBRTEST-PETROL-LTR", "beta", notes="User matrix"),
    _candidate("V013-SN012-PETROL-MT", "SN012", "Petroleum Products", "FBRTEST-PETROL-MT", "beta", notes="User matrix"),
    _candidate("V014-SN009-COTTON", "SN009", "Cotton ginners", "FBRTEST-COTTON-KG", "beta", notes="User matrix"),
    _candidate("V015-SN027-TYRE-CONSUMER", "SN027", "3rd Schedule Goods - End Consumer", "FBRTEST-TYRE-NPU", "walkin", fixed_price=12000.0, notes="Consumer + fixedPrice=12000"),
    _candidate("V016-SN026-SHIRT", "SN026", "Goods at standard rate (default) - End Consumer", "FBRTEST-SHIRT-NPU", "walkin"),
    _candidate("V017-SN026-RICE", "SN026", "Goods at standard rate (default) - End Consumer", "FBRTEST-RICE-KG", "walkin"),
    _candidate("V018-SN026-SOAP", "SN026", "Goods at standard rate (default) - End Consumer", "FBRTEST-SOAP-KG", "walkin"),

    # Extra valid fallback
    _candidate("V019-SN001-LAPTOP-BETA", "SN001", "Goods at standard rate (default) - Registered Buyer", "FBRTEST-LAPTOP-NPU", "beta"),
    _candidate("V020-SN001-SHIRT-ALPHA", "SN001", "Goods at standard rate (default) - Registered Buyer", "FBRTEST-SHIRT-NPU", "beta"),
]


INVALID_CANDIDATES = [
    # Known-invalid patterns already observed in your quick probe history
    _candidate("I001-SN005-MED-NPU-1", "SN005", "Goods at Reduced Rate", "FBRTEST-MEDICINE-NPU", "beta", rate="1%", is_expected_valid=False, notes="Known invalid: UOM/rate/SRO"),
    _candidate("I002-SN005-MED-KG-1", "SN005", "Goods at Reduced Rate", "FBRTEST-MEDICINE-KG", "beta", rate="1%", is_expected_valid=False, notes="Known invalid: SRO required"),
    _candidate("I003-SN021-CEMENT-18", "SN021", "Cement /Concrete Block", "FBRTEST-CEMENT-BAG", "beta", rate="18%", is_expected_valid=False, notes="Known invalid: rate not allowed"),
    _candidate("I004-SN020-EV-18", "SN020", "Electric Vehicle", "FBRTEST-EV-NPU", "beta", rate="18%", is_expected_valid=False, notes="Known invalid: rate/SRO mismatch"),
    _candidate("I005-SN023-CNG-KG-18", "SN023", "CNG Sales", "FBRTEST-CNG-KG", "alpha", rate="18%", is_expected_valid=False, notes="Known invalid: rate not allowed"),
    _candidate("I006-SN023-CNG-MMBTU-18", "SN023", "CNG Sales", "FBRTEST-CNG-MMBTU", "alpha", rate="18%", is_expected_valid=False, notes="Known invalid: rate not allowed"),
]


# -----------------------------------------------------------------------------
# Utility
# -----------------------------------------------------------------------------


def normalise_cnic(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", str(value)).strip()


def parse_percent(rate: str) -> float:
    text = str(rate or "").strip()
    if not text.endswith("%"):
        return 0.0
    try:
        return flt(text[:-1])
    except Exception:
        return 0.0


def canonical_payload_sale_type(scenario_id: str, sale_type_doctype: str) -> str:
    if scenario_id in STANDARD_SCENARIO_IDS:
        return "Goods at standard rate (default)"
    if scenario_id in REDUCED_SCENARIO_IDS:
        return "Goods at Reduced Rate"
    if scenario_id in THIRD_SCENARIO_IDS:
        return "3rd Schedule Goods"
    return sale_type_doctype


def resolve_buyer_tax_id(customer: Dict[str, Any], force_empty=False) -> str:
    if force_empty:
        return ""
    tax_id = (customer.get("tax_id") or "").strip()
    if tax_id:
        return tax_id
    nic = (customer.get("nic") or "").strip()
    if nic:
        return normalise_cnic(nic)
    ntn = (customer.get("ntn") or "").strip()
    if ntn:
        return ntn
    return ""


def _format_address_from_parts(parts: List[str]) -> str:
    return ", ".join([p for p in parts if p])


def _format_customer_address(customer: Dict[str, Any]) -> str:
    addr = customer.get("address") or {}
    return _format_address_from_parts(
        [
            addr.get("address_line1", ""),
            addr.get("city", ""),
            addr.get("state", ""),
            addr.get("pincode", ""),
        ]
    )


def _set_if_field_exists(doc, fieldname: str, value: Any):
    if doc.meta.has_field(fieldname):
        setattr(doc, fieldname, value)


def _resolve_company(default_company=DEFAULT_COMPANY) -> str:
    if frappe.db.exists("Company", default_company):
        return default_company
    fallback = frappe.db.get_value("Company", {}, "name")
    if not fallback:
        frappe.throw("No Company found. Please create a Company first.")
    return fallback


def _resolve_account(company: str, preferred: str, account_type: str | None, root_type: str | None):
    if preferred and frappe.db.exists("Account", preferred):
        return preferred

    filters = {"company": company, "is_group": 0}
    if account_type:
        filters["account_type"] = account_type
    if root_type:
        filters["root_type"] = root_type

    account = frappe.db.get_value("Account", filters, "name")
    if account:
        return account

    account = frappe.db.get_value("Account", {"company": company, "is_group": 0}, "name")
    if account:
        return account

    frappe.throw(f"No account found for company {company}")


def _resolve_warehouse(company: str, preferred: str):
    if preferred and frappe.db.exists("Warehouse", preferred):
        return preferred
    wh = frappe.db.get_value("Warehouse", {"company": company}, "name")
    if not wh:
        frappe.throw(f"No warehouse found for company {company}")
    return wh


def _resolve_cost_center(company: str, preferred: str):
    if preferred and frappe.db.exists("Cost Center", preferred):
        return preferred
    cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
    if not cc:
        cc = frappe.db.get_value("Cost Center", {"is_group": 0}, "name")
    if not cc:
        frappe.throw("No Cost Center found")
    return cc


def _resolve_tax_category(province: str) -> str:
    province_name = (province or "").strip().upper()
    if province_name and frappe.db.exists("Tax Category", province_name):
        return province_name
    first = frappe.db.get_value("Tax Category", {}, "name")
    return first or province_name


def _resolve_payment_mode(preferred="Cash") -> str:
    if preferred and frappe.db.exists("Mode of Payment", preferred):
        return preferred
    mode = frappe.db.get_value("Mode of Payment", {}, "name")
    if not mode:
        frappe.throw("No Mode of Payment found")
    return mode


def _ensure_price_list(price_list_name=DEFAULT_PRICE_LIST) -> str:
    if frappe.db.exists("Price List", price_list_name):
        return price_list_name
    doc = frappe.new_doc("Price List")
    doc.price_list_name = price_list_name
    doc.selling = 1
    doc.currency = "PKR"
    doc.insert(ignore_permissions=True)
    return doc.name


def _get_party_primary_address(link_doctype: str, link_name: str) -> Dict[str, Any]:
    if not (link_doctype and link_name):
        return {}

    address_names = frappe.get_all(
        "Dynamic Link",
        filters={
            "parenttype": "Address",
            "link_doctype": link_doctype,
            "link_name": link_name,
        },
        pluck="parent",
        limit=20,
    )
    if not address_names:
        return {}

    addresses = frappe.get_all(
        "Address",
        filters={"name": ["in", address_names], "disabled": 0},
        fields=[
            "name",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "pincode",
            "is_primary_address",
            "creation",
        ],
        order_by="is_primary_address desc, creation desc",
        limit=1,
    )
    return addresses[0] if addresses else {}


# -----------------------------------------------------------------------------
# Seller + Runtime Config
# -----------------------------------------------------------------------------


def ensure_company_address(company: str):
    existing = frappe.get_all(
        "Dynamic Link",
        filters={"parenttype": "Address", "link_doctype": "Company", "link_name": company},
        pluck="parent",
    )
    if existing:
        return

    addr = frappe.new_doc("Address")
    addr.address_title = company
    addr.address_type = "Office"
    addr.address_line1 = "Suite 500 Shahrah-e-Faisal"
    addr.city = "Karachi"
    addr.state = "Sindh"
    addr.pincode = "75400"
    addr.country = DEFAULT_COUNTRY
    addr.is_primary_address = 1
    addr.append("links", {"link_doctype": "Company", "link_name": company})
    addr.insert(ignore_permissions=True)


def get_seller_context(company: str) -> Dict[str, Any]:
    ensure_company_address(company)
    company_doc = frappe.get_doc("Company", company)
    address = _get_party_primary_address("Company", company)

    seller_address = _format_address_from_parts(
        [
            address.get("address_line1", ""),
            address.get("address_line2", ""),
            address.get("city", ""),
            address.get("state", ""),
            address.get("pincode", ""),
        ]
    )

    seller_province = (
        (address.get("state") or "").strip().upper()
        or "SINDH"
    )

    return {
        "company": company,
        "sellerNTNCNIC": (company_doc.tax_id or "").strip(),
        "sellerBusinessName": company,
        "sellerAddress": seller_address,
        "sellerProvince": seller_province,
    }


def ensure_item_tax_template_for_rate(company: str, tax_account: str, rate_percent: float) -> str:
    template_title = f"FBR Test Tax {rate_percent}%"
    existing = frappe.db.get_value(
        "Item Tax Template",
        {"company": company, "title": template_title},
        "name",
    )
    if existing:
        doc = frappe.get_doc("Item Tax Template", existing)
    else:
        doc = frappe.new_doc("Item Tax Template")

    doc.title = template_title
    doc.company = company

    if getattr(doc, "taxes", None):
        doc.taxes = []

    doc.append(
        "taxes",
        {
            "tax_type": tax_account,
            "tax_rate": flt(rate_percent),
        },
    )

    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)

    return doc.name


def ensure_sales_tax_template_18(company: str, cost_center: str, tax_account: str) -> str:
    template_title = "FBR Test Tax 18"
    existing = frappe.db.get_value(
        "Sales Taxes and Charges Template",
        {"company": company, "title": template_title},
        "name",
    )
    if existing:
        doc = frappe.get_doc("Sales Taxes and Charges Template", existing)
    else:
        doc = frappe.new_doc("Sales Taxes and Charges Template")

    doc.company = company
    doc.title = template_title

    if getattr(doc, "taxes", None):
        doc.taxes = []

    doc.append(
        "taxes",
        {
            "charge_type": "On Net Total",
            "account_head": tax_account,
            "description": "GST @ 18.0",
            "rate": 18.0,
            "cost_center": cost_center,
        },
    )

    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)

    return doc.name


def resolve_runtime_config() -> Dict[str, Any]:
    company = _resolve_company()
    if company == DEFAULT_COMPANY:
        current_tax_id = (frappe.db.get_value("Company", company, "tax_id") or "").strip()
        if current_tax_id != DEFAULT_COMPANY_TAX_ID:
            frappe.db.set_value("Company", company, "tax_id", DEFAULT_COMPANY_TAX_ID)
            frappe.db.commit()  # nosemgrep: frappe-manual-commit

    abbr = frappe.db.get_value("Company", company, "abbr") or "CO"
    currency = frappe.db.get_value("Company", company, "default_currency") or "PKR"

    warehouse = _resolve_warehouse(company, f"Stores - {abbr}")
    cost_center = _resolve_cost_center(company, f"Main - {abbr}")
    debit_account = _resolve_account(company, f"Debtors - {abbr}", "Receivable", None)
    income_account = _resolve_account(company, f"Sales - {abbr}", None, "Income")
    tax_account = _resolve_account(company, f"GST - {abbr}", "Tax", None)

    price_list = _ensure_price_list(DEFAULT_PRICE_LIST)
    item_tax_template_18 = ensure_item_tax_template_for_rate(company, tax_account, 18.0)
    sales_tax_template_18 = ensure_sales_tax_template_18(company, cost_center, tax_account)

    seller_context = get_seller_context(company)

    return {
        "company": company,
        "company_abbr": abbr,
        "currency": currency,
        "warehouse": warehouse,
        "cost_center": cost_center,
        "debit_account": debit_account,
        "income_account": income_account,
        "tax_account": tax_account,
        "price_list": price_list,
        "item_tax_template_18": item_tax_template_18,
        "sales_tax_template_18": sales_tax_template_18,
        "seller_context": seller_context,
        "default_payment_mode": _resolve_payment_mode("Cash"),
    }


# -----------------------------------------------------------------------------
# Setup Helpers
# -----------------------------------------------------------------------------


def ensure_customer_group(group_name: str):
    if frappe.db.exists("Customer Group", group_name):
        return

    parent = "All Customer Groups"
    if not frappe.db.exists("Customer Group", parent):
        alt = frappe.get_all("Customer Group", filters={"is_group": 1}, limit=1)
        parent = alt[0].name if alt else None

    if not parent:
        return

    doc = frappe.new_doc("Customer Group")
    doc.customer_group_name = group_name
    doc.parent_customer_group = parent
    doc.insert(ignore_permissions=True)


def ensure_item_group(group_name: str):
    if frappe.db.exists("Item Group", group_name):
        return

    parent = "All Item Groups"
    if not frappe.db.exists("Item Group", parent):
        alt = frappe.get_all("Item Group", filters={"is_group": 1}, limit=1)
        parent = alt[0].name if alt else None

    if not parent:
        return

    doc = frappe.new_doc("Item Group")
    doc.item_group_name = group_name
    doc.parent_item_group = parent
    doc.insert(ignore_permissions=True)


def ensure_territory(territory_name: str):
    if frappe.db.exists("Territory", territory_name):
        return

    parent = "All Territories"
    if not frappe.db.exists("Territory", parent):
        alt = frappe.get_all("Territory", filters={"is_group": 1}, limit=1)
        parent = alt[0].name if alt else None

    if not parent:
        return

    doc = frappe.new_doc("Territory")
    doc.territory_name = territory_name
    doc.parent_territory = parent
    doc.insert(ignore_permissions=True)


def create_customers():
    print("\n=== Creating/Updating Customers ===")
    for cust_data in CUSTOMERS:
        ensure_customer_group(cust_data.get("customer_group", "Commercial"))
        ensure_territory(cust_data.get("territory", "Pakistan"))

        existing_name = frappe.db.get_value("Customer", {"customer_name": cust_data["customer_name"]}, "name")
        if existing_name:
            cust = frappe.get_doc("Customer", existing_name)
            action = "Updated"
        else:
            cust = frappe.new_doc("Customer")
            cust.customer_name = cust_data["customer_name"]
            action = "Created"

        cust.customer_type = cust_data["customer_type"]
        cust.customer_group = cust_data["customer_group"]
        cust.territory = cust_data.get("territory", "Pakistan")
        cust.tax_id = cust_data.get("tax_id", "")

        _set_if_field_exists(cust, "nic", cust_data.get("nic", ""))
        _set_if_field_exists(cust, "ntn", cust_data.get("ntn", ""))
        _set_if_field_exists(cust, "custom_province", cust_data.get("custom_province", ""))

        if cust.is_new():
            cust.insert(ignore_permissions=True)
        else:
            cust.save(ignore_permissions=True)

        addr_data = cust_data.get("address") or {}
        existing_addr_links = frappe.get_all(
            "Dynamic Link",
            filters={"parenttype": "Address", "link_doctype": "Customer", "link_name": cust.name},
            pluck="parent",
            limit=1,
        )

        if existing_addr_links:
            addr = frappe.get_doc("Address", existing_addr_links[0])
        else:
            addr = frappe.new_doc("Address")
            addr.address_title = cust_data["customer_name"]
            addr.address_type = "Billing"
            addr.is_primary_address = 1
            addr.append("links", {"link_doctype": "Customer", "link_name": cust.name})

        addr.address_line1 = addr_data.get("address_line1", "")
        addr.city = addr_data.get("city", "")
        addr.state = addr_data.get("state", "")
        addr.pincode = addr_data.get("pincode", "")
        addr.country = addr_data.get("country", DEFAULT_COUNTRY)

        if addr.is_new():
            addr.insert(ignore_permissions=True)
        else:
            addr.save(ignore_permissions=True)

        print(f"  {action} customer: {cust.customer_name} ({cust.name})")

    frappe.db.commit()  # nosemgrep: frappe-manual-commit


def create_items(runtime: Dict[str, Any]):
    print("\n=== Creating/Updating Items ===")

    for item_data in ITEMS:
        ensure_item_group(item_data.get("item_group", "Products"))
        code = item_data["item_code"]

        if frappe.db.exists("Item", code):
            item = frappe.get_doc("Item", code)
            action = "Updated"
        else:
            item = frappe.new_doc("Item")
            item.item_code = code
            action = "Created"

        item.item_name = item_data["item_name"]
        item.item_group = item_data.get("item_group", "Products")
        item.stock_uom = item_data["stock_uom"]
        item.is_stock_item = 1
        item.valuation_rate = flt(item_data["value_excl_st"])

        _set_if_field_exists(item, "custom_hs_code", item_data["custom_hs_code"])
        _set_if_field_exists(item, "custom_sale_type", item_data["default_sale_type"])

        if getattr(item, "taxes", None):
            item.taxes = []
        item.append("taxes", {"item_tax_template": runtime["item_tax_template_18"]})

        if item.is_new():
            item.insert(ignore_permissions=True)
        else:
            item.save(ignore_permissions=True)

        print(f"  {action} item: {code} ({item_data['stock_uom']}, HS {item_data['custom_hs_code']})")

    frappe.db.commit()  # nosemgrep: frappe-manual-commit


def ensure_price_list_entries(runtime: Dict[str, Any]):
    print("\n=== Ensuring Item Prices ===")
    for item in ITEMS:
        existing = frappe.db.exists(
            "Item Price",
            {
                "item_code": item["item_code"],
                "price_list": runtime["price_list"],
                "selling": 1,
            },
        )
        if existing:
            price_doc = frappe.get_doc("Item Price", existing)
            price_doc.price_list_rate = flt(item["value_excl_st"])
            price_doc.save(ignore_permissions=True)
            continue

        ip = frappe.new_doc("Item Price")
        ip.item_code = item["item_code"]
        ip.price_list = runtime["price_list"]
        ip.selling = 1
        ip.currency = runtime["currency"]
        ip.price_list_rate = flt(item["value_excl_st"])
        ip.insert(ignore_permissions=True)

    frappe.db.commit()  # nosemgrep: frappe-manual-commit


def ensure_stock(runtime: Dict[str, Any]):
    print("\n=== Ensuring Stock ===")
    from erpnext.stock.utils import get_stock_balance

    warehouse = runtime["warehouse"]

    for item in ITEMS:
        qty = flt(get_stock_balance(item["item_code"], warehouse) or 0)
        if qty > 0:
            continue

        se = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Material Receipt"
        se.company = runtime["company"]
        se.append(
            "items",
            {
                "item_code": item["item_code"],
                "t_warehouse": warehouse,
                "qty": 5000,
                "basic_rate": flt(item["value_excl_st"]),
            },
        )
        se.insert(ignore_permissions=True)
        se.submit()
        print(f"  Stock created: {item['item_code']}")

    frappe.db.commit()  # nosemgrep: frappe-manual-commit


def ensure_pos_profile(runtime: Dict[str, Any]) -> str:
    print("\n=== Ensuring POS Profile ===")

    profile_name = f"FBR Test POS Profile - {runtime['company_abbr']}"
    if frappe.db.exists("POS Profile", profile_name):
        return profile_name

    existing = frappe.db.get_value("POS Profile", {"company": runtime["company"]}, "name")
    if existing:
        return existing

    pos = frappe.new_doc("POS Profile")
    pos.name = profile_name
    pos.company = runtime["company"]
    pos.warehouse = runtime["warehouse"]
    pos.cost_center = runtime["cost_center"]
    pos.selling_price_list = runtime["price_list"]
    pos.currency = runtime["currency"]
    pos.write_off_account = _resolve_account(runtime["company"], "", "", "Expense")
    pos.write_off_cost_center = runtime["cost_center"]

    pos.append("payments", {"mode_of_payment": runtime["default_payment_mode"], "default": 1})
    pos.append("item_groups", {"item_group": "All Item Groups"})
    _set_if_field_exists(pos, "taxes_and_charges", runtime["sales_tax_template_18"])

    pos.insert(ignore_permissions=True)
    frappe.db.commit()  # nosemgrep: frappe-manual-commit
    return pos.name


def ensure_pos_opening_entry(runtime: Dict[str, Any], pos_profile: str, user: str = "Administrator") -> str:
    existing_open = frappe.db.get_value(
        "POS Opening Entry",
        {
            "pos_profile": pos_profile,
            "user": user,
            "docstatus": 1,
            "status": "Open",
        },
        "name",
    )
    if existing_open:
        return existing_open

    entry = frappe.new_doc("POS Opening Entry")
    entry.period_start_date = now_datetime()
    entry.posting_date = nowdate()
    entry.company = runtime["company"]
    entry.pos_profile = pos_profile
    entry.user = user
    entry.append(
        "balance_details",
        {
            "mode_of_payment": runtime["default_payment_mode"],
            "opening_amount": 0,
        },
    )
    entry.insert(ignore_permissions=True)
    entry.submit()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit
    return entry.name


def ensure_pos_settings_for_pos_invoice() -> str:
    current_mode = (frappe.db.get_single_value("POS Settings", "invoice_type") or "").strip()
    if current_mode != "POS Invoice":
        frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")
        frappe.db.commit()  # nosemgrep: frappe-manual-commit
        print(f"  POS Settings invoice_type switched to POS Invoice (was '{current_mode or 'blank'}').")
    return current_mode


def restore_pos_settings_invoice_type(previous_mode: str):
    previous = (previous_mode or "").strip()
    if not previous:
        return
    current_mode = (frappe.db.get_single_value("POS Settings", "invoice_type") or "").strip()
    if current_mode == previous:
        return
    frappe.db.set_single_value("POS Settings", "invoice_type", previous)
    frappe.db.commit()  # nosemgrep: frappe-manual-commit
    print(f"  POS Settings invoice_type restored to '{previous}'.")


# -----------------------------------------------------------------------------
# Candidate Payload Build + Probe
# -----------------------------------------------------------------------------


def build_candidate_payload(candidate: Dict[str, Any], invoice_type="Sale Invoice") -> Dict[str, Any]:
    seller = get_seller_context(_resolve_company())
    customer = CUSTOMER_BY_KEY[candidate["customer_key"]]
    item = ITEM_BY_CODE[candidate["item_code"]]

    buyer_tax_id = resolve_buyer_tax_id(customer, force_empty=candidate.get("requires_no_id_buyer", False))
    buyer_registration_type = "Registered" if buyer_tax_id else "Unregistered"

    rate = str(candidate.get("rate") or "18%")
    tax_percent = parse_percent(rate)
    value_excl_st = flt(candidate.get("value_excl_st") or item["value_excl_st"])
    sales_tax_applicable = round((tax_percent * value_excl_st) / 100.0, 2)

    payload_sale_type = canonical_payload_sale_type(candidate["scenario_id"], candidate["sale_type_doctype"])

    payload = {
        "invoiceType": invoice_type,
        "invoiceDate": nowdate(),
        "sellerNTNCNIC": seller["sellerNTNCNIC"],
        "sellerBusinessName": seller["sellerBusinessName"],
        "sellerProvince": seller["sellerProvince"],
        "sellerAddress": seller["sellerAddress"],
        "buyerNTNCNIC": buyer_tax_id,
        "buyerBusinessName": customer["customer_name"],
        "buyerProvince": (customer.get("custom_province") or "").upper(),
        "buyerAddress": _format_customer_address(customer),
        "buyerRegistrationType": buyer_registration_type,
        "invoiceRefNo": "",
        "scenarioId": candidate["scenario_id"],
        "items": [
            {
                "hsCode": item["custom_hs_code"],
                "productDescription": item["item_name"],
                "rate": rate,
                "uoM": item["stock_uom"],
                "quantity": flt(candidate.get("quantity") or 1),
                "totalValues": 0.00,
                "valueSalesExcludingST": value_excl_st,
                "fixedNotifiedValueOrRetailPrice": flt(candidate.get("fixed_price") or 0.0),
                "salesTaxApplicable": sales_tax_applicable,
                "salesTaxWithheldAtSource": 0.00,
                "extraTax": 0.00,
                "furtherTax": 0.00,
                "sroScheduleNo": "",
                "fedPayable": 0.00,
                "discount": 0.00,
                "saleType": payload_sale_type,
                "sroItemSerialNo": "",
            }
        ],
    }

    return payload


def _extract_error_text(response_data: Dict[str, Any]) -> str:
    validation = (response_data or {}).get("validationResponse", {})
    if validation.get("error"):
        return str(validation.get("error"))

    statuses = validation.get("invoiceStatuses")
    if isinstance(statuses, list) and statuses:
        first = statuses[0] if isinstance(statuses[0], dict) else {}
        if first.get("error"):
            return str(first.get("error"))

    return ""


def test_payload_against_api(payload: Dict[str, Any], label="") -> Dict[str, Any]:
    fbr_settings = frappe.get_single("FBR E-Inv Setup")
    api_endpoint = (fbr_settings.api_endpoint or "").strip()
    token = (fbr_settings.pral_authorization_token or "").strip()

    if not api_endpoint or not token:
        return {
            "success": False,
            "label": label,
            "http_status": None,
            "fbr_status": "Error",
            "error": "FBR API endpoint/token is not configured.",
            "data": {},
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    verify_ssl = bool(getattr(fbr_settings, "verify_ssl", True))
    connect_timeout = float(getattr(fbr_settings, "connect_timeout", 10.0))
    read_timeout = float(getattr(fbr_settings, "read_timeout", 30.0))

    try:
        resp = requests.post(
            api_endpoint,
            json=payload,
            headers=headers,
            timeout=(connect_timeout, read_timeout),
            verify=verify_ssl,
        )
        data = resp.json() if (resp.text or "").strip() else {}
    except Exception as e:
        return {
            "success": False,
            "label": label,
            "http_status": None,
            "fbr_status": "Error",
            "error": str(e),
            "data": {},
        }

    fbr_status = (data or {}).get("validationResponse", {}).get("status", "Unknown")
    return {
        "success": fbr_status == "Valid",
        "label": label,
        "http_status": resp.status_code,
        "fbr_status": fbr_status,
        "error": _extract_error_text(data),
        "data": data,
    }


def probe_candidate(candidate: Dict[str, Any], invoice_type="Sale Invoice") -> Dict[str, Any]:
    payload = build_candidate_payload(candidate, invoice_type=invoice_type)
    probe = test_payload_against_api(payload, label=candidate["id"])

    response_data = probe.get("data") or {}
    invoice_number = (response_data or {}).get("invoiceNumber", "")

    return {
        "candidate": candidate,
        "payload": payload,
        "actual_valid": bool(probe.get("success")),
        "expected_valid": bool(candidate.get("is_expected_valid")),
        "http_status": probe.get("http_status"),
        "fbr_status": probe.get("fbr_status"),
        "error": probe.get("error", ""),
        "invoice_number": invoice_number,
        "response": response_data,
    }


def _candidate_targets(candidate: Dict[str, Any]) -> tuple[str, ...]:
    targets = candidate.get("doctype_targets") or ("Sales", "POS")
    return tuple(targets)


def materialize_candidates() -> List[Dict[str, Any]]:
    all_candidates = []
    order = 1
    for row in VALID_CANDIDATES + INVALID_CANDIDATES:
        copy_row = dict(row)
        copy_row["order"] = order
        order += 1
        all_candidates.append(copy_row)
    return all_candidates


def _probe_for_doctype(candidates: List[Dict[str, Any]], doctype_label: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    print(f"\n--- Probing {doctype_label} candidates ({len(candidates)}) ---")

    for idx, candidate in enumerate(candidates, 1):
        res = probe_candidate(candidate, invoice_type="Sale Invoice")
        results.append(res)
        status = "VALID" if res["actual_valid"] else "INVALID"
        print(
            f"  {idx:02d}. {candidate['id']}: {status} "
            f"(expected={'VALID' if candidate['is_expected_valid'] else 'INVALID'}, "
            f"scenario={candidate['scenario_id']}, buyer={candidate['customer_key']})"
        )
        if not res["actual_valid"] and res.get("error"):
            print(f"      -> {res['error']}")

    return results


def select_mixed_dataset(
    probe_results: List[Dict[str, Any]],
    valid_target: int,
    invalid_target: int,
    min_sn002_no_id: int,
) -> List[Dict[str, Any]]:
    ordered = sorted(probe_results, key=lambda r: r["candidate"].get("order", 0))

    valid_pool = [
        row
        for row in ordered
        if row["actual_valid"] and row["candidate"].get("is_expected_valid")
    ]

    invalid_pool = [
        row
        for row in ordered
        if (not row["actual_valid"]) and (not row["candidate"].get("is_expected_valid"))
    ]

    sn002_no_id_valid_pool = [
        row
        for row in valid_pool
        if row["candidate"].get("scenario_id") == "SN002"
        and row["candidate"].get("requires_no_id_buyer")
    ]

    if len(sn002_no_id_valid_pool) < min_sn002_no_id:
        frappe.throw(
            f"Need at least {min_sn002_no_id} valid SN002 no-ID candidates; "
            f"found {len(sn002_no_id_valid_pool)}"
        )

    selected_valid: List[Dict[str, Any]] = sn002_no_id_valid_pool[:min_sn002_no_id]

    for row in valid_pool:
        if len(selected_valid) >= valid_target:
            break
        if row in selected_valid:
            continue
        selected_valid.append(row)

    if len(selected_valid) < valid_target:
        frappe.throw(
            f"Could not select {valid_target} valid candidates. "
            f"Only {len(selected_valid)} available."
        )

    selected_invalid = invalid_pool[:invalid_target]
    if len(selected_invalid) < invalid_target:
        frappe.throw(
            f"Could not select {invalid_target} controlled-invalid candidates. "
            f"Only {len(selected_invalid)} available."
        )

    return selected_valid + selected_invalid


def prepare_candidates(
    valid_target=DEFAULT_TARGET_VALID,
    invalid_target=DEFAULT_TARGET_INVALID,
    min_sn002_no_id=DEFAULT_MIN_SN002_NO_ID,
) -> Dict[str, Any]:
    candidates = materialize_candidates()

    sales_candidates = [c for c in candidates if "Sales" in _candidate_targets(c)]
    pos_candidates = [c for c in candidates if "POS" in _candidate_targets(c)]

    sales_probe = _probe_for_doctype(sales_candidates, "Sales Invoice")
    pos_probe = _probe_for_doctype(pos_candidates, "POS Invoice")

    selected_sales = select_mixed_dataset(
        sales_probe,
        valid_target=valid_target,
        invalid_target=invalid_target,
        min_sn002_no_id=min_sn002_no_id,
    )
    selected_pos = select_mixed_dataset(
        pos_probe,
        valid_target=valid_target,
        invalid_target=invalid_target,
        min_sn002_no_id=min_sn002_no_id,
    )

    return {
        "sales_probe": sales_probe,
        "pos_probe": pos_probe,
        "selected_sales": selected_sales,
        "selected_pos": selected_pos,
    }


def prepare_candidates_without_prevalidation(
    valid_target=DEFAULT_TARGET_VALID,
    invalid_target=DEFAULT_TARGET_INVALID,
    min_sn002_no_id=DEFAULT_MIN_SN002_NO_ID,
) -> Dict[str, Any]:
    def _expected_probe_row(candidate: Dict[str, Any]) -> Dict[str, Any]:
        is_valid = bool(candidate.get("is_expected_valid"))
        return {
            "candidate": candidate,
            "payload": {},
            "actual_valid": is_valid,
            "expected_valid": is_valid,
            "http_status": 200 if is_valid else 400,
            "fbr_status": "Valid" if is_valid else "Invalid",
            "error": "" if is_valid else "Controlled invalid (no prevalidation)",
            "invoice_number": "",
            "response": {},
        }

    candidates = materialize_candidates()
    sales_probe = [
        _expected_probe_row(c)
        for c in candidates
        if "Sales" in _candidate_targets(c)
    ]
    pos_probe = [
        _expected_probe_row(c)
        for c in candidates
        if "POS" in _candidate_targets(c)
    ]

    selected_sales = select_mixed_dataset(
        sales_probe,
        valid_target=valid_target,
        invalid_target=invalid_target,
        min_sn002_no_id=min_sn002_no_id,
    )
    selected_pos = select_mixed_dataset(
        pos_probe,
        valid_target=valid_target,
        invalid_target=invalid_target,
        min_sn002_no_id=min_sn002_no_id,
    )

    return {
        "sales_probe": sales_probe,
        "pos_probe": pos_probe,
        "selected_sales": selected_sales,
        "selected_pos": selected_pos,
    }


def _print_probe_summary(title: str, probe_results: List[Dict[str, Any]], selected: List[Dict[str, Any]] | None = None):
    total = len(probe_results)
    valid = len([r for r in probe_results if r["actual_valid"]])
    invalid = total - valid

    expected_valid_actual_valid = len(
        [r for r in probe_results if r["actual_valid"] and r["candidate"].get("is_expected_valid")]
    )
    expected_invalid_actual_invalid = len(
        [r for r in probe_results if (not r["actual_valid"]) and (not r["candidate"].get("is_expected_valid"))]
    )

    print(f"\n{title}")
    print(f"  Total probed: {total}")
    print(f"  Actual valid: {valid}")
    print(f"  Actual invalid: {invalid}")
    print(f"  Controlled-valid pool: {expected_valid_actual_valid}")
    print(f"  Controlled-invalid pool: {expected_invalid_actual_invalid}")

    if selected is not None:
        selected_valid = len([r for r in selected if r["candidate"].get("is_expected_valid")])
        selected_invalid = len(selected) - selected_valid
        sn002_no_id = len(
            [
                r
                for r in selected
                if r["candidate"].get("scenario_id") == "SN002"
                and r["candidate"].get("requires_no_id_buyer")
                and r["candidate"].get("is_expected_valid")
            ]
        )
        print(f"  Selected: {len(selected)} ({selected_valid} expected-valid, {selected_invalid} expected-invalid)")
        print(f"  Selected SN002 no-ID valid: {sn002_no_id}")


# -----------------------------------------------------------------------------
# Document Creation
# -----------------------------------------------------------------------------


def _item_tax_template_for_candidate(runtime: Dict[str, Any], candidate: Dict[str, Any]) -> str:
    rate_percent = parse_percent(candidate.get("rate") or "18%")
    if rate_percent <= 0:
        rate_percent = 18.0
    return ensure_item_tax_template_for_rate(runtime["company"], runtime["tax_account"], rate_percent)


def _build_invoice_remark(candidate: Dict[str, Any], probe_row: Dict[str, Any], doctype: str) -> str:
    return (
        f"{TEST_MARKER} doctype={doctype}; candidate={candidate['id']}; "
        f"scenario={candidate['scenario_id']}; expected_valid={candidate['is_expected_valid']}; "
        f"probe_valid={probe_row['actual_valid']}; http={probe_row.get('http_status')}; "
        f"fbr_status={probe_row.get('fbr_status')}; requires_no_id_buyer={candidate.get('requires_no_id_buyer')}"
    )


def create_sales_invoices(
    selected: List[Dict[str, Any]],
    runtime: Dict[str, Any],
    as_draft=True,
) -> List[str]:
    created = []
    print("\n=== Creating Sales Invoice Drafts ===")

    for idx, row in enumerate(selected, 1):
        candidate = row["candidate"]
        customer = CUSTOMER_BY_KEY[candidate["customer_key"]]
        item = ITEM_BY_CODE[candidate["item_code"]]

        si = frappe.new_doc("Sales Invoice")
        si.company = runtime["company"]
        si.customer = customer["customer_name"]
        si.posting_date = nowdate()
        si.set_posting_time = 1
        si.due_date = nowdate()
        si.currency = runtime["currency"]
        si.selling_price_list = runtime["price_list"]
        si.debit_to = runtime["debit_account"]
        si.cost_center = runtime["cost_center"]
        si.update_stock = 1

        _set_if_field_exists(si, "custom_submit_to_fbr", 1)
        _set_if_field_exists(si, "custom_province", runtime["seller_context"]["sellerProvince"])
        _set_if_field_exists(si, "tax_category", _resolve_tax_category(customer.get("custom_province") or ""))

        si.append(
            "items",
            {
                "item_code": item["item_code"],
                "item_name": item["item_name"],
                "description": item["item_name"],
                "qty": flt(candidate.get("quantity") or 1),
                "rate": flt(candidate.get("value_excl_st") or item["value_excl_st"]),
                "uom": item["stock_uom"],
                "stock_uom": item["stock_uom"],
                "conversion_factor": 1,
                "warehouse": runtime["warehouse"],
                "income_account": runtime["income_account"],
                "cost_center": runtime["cost_center"],
                "item_tax_template": _item_tax_template_for_candidate(runtime, candidate),
                "custom_hs_code": item["custom_hs_code"],
                "custom_sale_type": candidate["sale_type_doctype"],
                "discount_amount": 0,
            },
        )

        si.append(
            "taxes",
            {
                "charge_type": "On Net Total",
                "account_head": runtime["tax_account"],
                "description": f"GST @ {DEFAULT_SALES_TAX_RATE}",
                "rate": DEFAULT_SALES_TAX_RATE,
                "cost_center": runtime["cost_center"],
            },
        )

        _set_if_field_exists(si, "remarks", _build_invoice_remark(candidate, row, "Sales Invoice"))

        si.insert(ignore_permissions=True)
        if not as_draft:
            si.submit()

        created.append(si.name)
        print(
            f"  SI-{idx:02d}: {si.name} | candidate={candidate['id']} | "
            f"expected={'VALID' if candidate['is_expected_valid'] else 'INVALID'}"
        )

    frappe.db.commit()  # nosemgrep: frappe-manual-commit
    return created


def create_pos_invoices(
    selected: List[Dict[str, Any]],
    runtime: Dict[str, Any],
    pos_profile_name: str,
    as_draft=True,
) -> List[str]:
    created = []
    print("\n=== Creating POS Invoice Drafts ===")

    for idx, row in enumerate(selected, 1):
        candidate = row["candidate"]
        customer = CUSTOMER_BY_KEY[candidate["customer_key"]]
        item = ITEM_BY_CODE[candidate["item_code"]]

        pos = frappe.new_doc("POS Invoice")
        pos.company = runtime["company"]
        pos.customer = customer["customer_name"]
        pos.posting_date = nowdate()
        pos.set_posting_time = 1
        pos.due_date = nowdate()
        pos.currency = runtime["currency"]
        pos.selling_price_list = runtime["price_list"]
        pos.debit_to = runtime["debit_account"]
        pos.cost_center = runtime["cost_center"]
        pos.pos_profile = pos_profile_name
        pos.is_pos = 1
        pos.update_stock = 1

        _set_if_field_exists(pos, "custom_submit_to_fbr", 1)
        _set_if_field_exists(pos, "custom_province", runtime["seller_context"]["sellerProvince"])
        _set_if_field_exists(pos, "tax_category", _resolve_tax_category(customer.get("custom_province") or ""))

        qty = flt(candidate.get("quantity") or 1)
        rate = flt(candidate.get("value_excl_st") or item["value_excl_st"])

        pos.append(
            "items",
            {
                "item_code": item["item_code"],
                "item_name": item["item_name"],
                "description": item["item_name"],
                "qty": qty,
                "rate": rate,
                "uom": item["stock_uom"],
                "stock_uom": item["stock_uom"],
                "conversion_factor": 1,
                "warehouse": runtime["warehouse"],
                "income_account": runtime["income_account"],
                "cost_center": runtime["cost_center"],
                "item_tax_template": _item_tax_template_for_candidate(runtime, candidate),
                "custom_hs_code": item["custom_hs_code"],
                "custom_sale_type": candidate["sale_type_doctype"],
            },
        )

        pos.append(
            "taxes",
            {
                "charge_type": "On Net Total",
                "account_head": runtime["tax_account"],
                "description": f"GST @ {DEFAULT_SALES_TAX_RATE}",
                "rate": DEFAULT_SALES_TAX_RATE,
                "cost_center": runtime["cost_center"],
            },
        )

        grand_total = (qty * rate) * (1 + (DEFAULT_SALES_TAX_RATE / 100.0))
        pos.append(
            "payments",
            {
                "mode_of_payment": runtime["default_payment_mode"],
                "amount": grand_total,
                "default": 1,
            },
        )

        _set_if_field_exists(pos, "remarks", _build_invoice_remark(candidate, row, "POS Invoice"))

        pos.insert(ignore_permissions=True)
        if not as_draft:
            pos.submit()

        created.append(pos.name)
        print(
            f"  POS-{idx:02d}: {pos.name} | candidate={candidate['id']} | "
            f"expected={'VALID' if candidate['is_expected_valid'] else 'INVALID'}"
        )

    frappe.db.commit()  # nosemgrep: frappe-manual-commit
    return created


# -----------------------------------------------------------------------------
# Main Entry Points
# -----------------------------------------------------------------------------


def test_payloads_only():
    print("\n" + "=" * 78)
    print("FBR PAYLOAD PREVALIDATION ONLY")
    print("=" * 78)

    results = prepare_candidates(
        valid_target=DEFAULT_TARGET_VALID,
        invalid_target=DEFAULT_TARGET_INVALID,
        min_sn002_no_id=DEFAULT_MIN_SN002_NO_ID,
    )

    _print_probe_summary("Sales Probe Summary", results["sales_probe"], results["selected_sales"])
    _print_probe_summary("POS Probe Summary", results["pos_probe"], results["selected_pos"])

    print("\nSelected Sales Candidates:")
    for row in results["selected_sales"]:
        c = row["candidate"]
        print(f"  {c['id']} | expected={'VALID' if c['is_expected_valid'] else 'INVALID'}")

    print("\nSelected POS Candidates:")
    for row in results["selected_pos"]:
        c = row["candidate"]
        print(f"  {c['id']} | expected={'VALID' if c['is_expected_valid'] else 'INVALID'}")

    return {
        "sales_selected": [r["candidate"]["id"] for r in results["selected_sales"]],
        "pos_selected": [r["candidate"]["id"] for r in results["selected_pos"]],
    }


def run():
    """
    Full flow:
    1) Ensure master/setup data
    2) Select deterministic 15 expected-valid + 5 expected-invalid for Sales and POS
    3) Create draft invoices only (no submit, no queue)
    """
    print("\n" + "=" * 78)
    print("FBR TEST DATA CREATOR - DRAFT MODE (NO PREVALIDATION)")
    print("=" * 78)

    runtime = resolve_runtime_config()

    create_customers()
    create_items(runtime)
    ensure_price_list_entries(runtime)
    ensure_stock(runtime)
    pos_profile = ensure_pos_profile(runtime)
    pos_opening_entry = ensure_pos_opening_entry(runtime, pos_profile, user="Administrator")
    print(f"  Using POS Opening Entry: {pos_opening_entry}")

    results = prepare_candidates_without_prevalidation(
        valid_target=DEFAULT_TARGET_VALID,
        invalid_target=DEFAULT_TARGET_INVALID,
        min_sn002_no_id=DEFAULT_MIN_SN002_NO_ID,
    )

    _print_probe_summary("Sales Probe Summary", results["sales_probe"], results["selected_sales"])
    _print_probe_summary("POS Probe Summary", results["pos_probe"], results["selected_pos"])

    sales_names = create_sales_invoices(results["selected_sales"], runtime, as_draft=True)

    previous_pos_mode = ensure_pos_settings_for_pos_invoice()
    try:
        pos_names = create_pos_invoices(results["selected_pos"], runtime, pos_profile, as_draft=True)
    finally:
        restore_pos_settings_invoice_type(previous_pos_mode)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  Sales Invoices created (draft): {len(sales_names)}")
    print(f"  POS Invoices created (draft): {len(pos_names)}")
    print(f"  Sales names: {sales_names}")
    print(f"  POS names: {pos_names}")
    print("\n  Notes:")
    print("    - No invoices were submitted.")
    print("    - No queue jobs were triggered.")
    print("    - Each doctype includes 15 expected-valid + 5 expected-invalid candidates.")
    print("=" * 78)

    return {
        "sales_invoices": sales_names,
        "pos_invoices": pos_names,
    }


def run_offline():
    """Compatibility alias; run now always skips prevalidation."""
    return run()


# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------


def cleanup():
    print("\n" + "=" * 78)
    print("CLEANUP FBR TEST DATA")
    print("=" * 78)

    # Delete POS Invoices tagged by remark marker
    pos_names = frappe.get_all(
        "POS Invoice",
        filters={"remarks": ["like", f"%{TEST_MARKER}%"]},
        pluck="name",
    )
    for name in pos_names:
        try:
            doc = frappe.get_doc("POS Invoice", name)
            if doc.docstatus == 1:
                doc.cancel()
            frappe.delete_doc("POS Invoice", name, force=True, ignore_permissions=True)
            print(f"  Deleted POS Invoice: {name}")
        except Exception as e:
            print(f"  Failed POS delete {name}: {e}")

    # Delete Sales Invoices tagged by remark marker
    si_names = frappe.get_all(
        "Sales Invoice",
        filters={"remarks": ["like", f"%{TEST_MARKER}%"]},
        pluck="name",
    )
    for name in si_names:
        try:
            doc = frappe.get_doc("Sales Invoice", name)
            if doc.docstatus == 1:
                doc.cancel()
            frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True)
            print(f"  Deleted Sales Invoice: {name}")
        except Exception as e:
            print(f"  Failed SI delete {name}: {e}")

    # Delete stock entries created for FBRTEST items
    stock_entries = frappe.get_all(
        "Stock Entry",
        filters={"stock_entry_type": "Material Receipt"},
        pluck="name",
        order_by="creation desc",
    )
    for name in stock_entries:
        try:
            doc = frappe.get_doc("Stock Entry", name)
            is_test = any((row.item_code or "").startswith(TEST_ITEM_PREFIX) for row in doc.items)
            if not is_test:
                continue
            if doc.docstatus == 1:
                doc.cancel()
            frappe.delete_doc("Stock Entry", name, force=True, ignore_permissions=True)
            print(f"  Deleted Stock Entry: {name}")
        except Exception as e:
            print(f"  Failed Stock Entry delete {name}: {e}")

    # Delete item prices for test items
    for item in ITEMS:
        price_names = frappe.get_all(
            "Item Price",
            filters={"item_code": item["item_code"]},
            pluck="name",
        )
        for price_name in price_names:
            try:
                frappe.delete_doc("Item Price", price_name, force=True, ignore_permissions=True)
            except Exception:
                pass

    # Delete items
    for item in ITEMS:
        if not frappe.db.exists("Item", item["item_code"]):
            continue
        try:
            frappe.delete_doc("Item", item["item_code"], force=True, ignore_permissions=True)
            print(f"  Deleted Item: {item['item_code']}")
        except Exception as e:
            print(f"  Failed item delete {item['item_code']}: {e}")

    # Delete customers + linked addresses
    for customer in CUSTOMERS:
        cust_name = frappe.db.get_value("Customer", {"customer_name": customer["customer_name"]}, "name")
        if not cust_name:
            continue

        addr_names = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Customer", "link_name": cust_name, "parenttype": "Address"},
            pluck="parent",
        )
        for addr_name in addr_names:
            try:
                frappe.delete_doc("Address", addr_name, force=True, ignore_permissions=True)
            except Exception:
                pass

        try:
            frappe.delete_doc("Customer", cust_name, force=True, ignore_permissions=True)
            print(f"  Deleted Customer: {cust_name}")
        except Exception as e:
            print(f"  Failed customer delete {cust_name}: {e}")

    frappe.db.commit()  # nosemgrep: frappe-manual-commit
    print("\nCleanup complete.")


if __name__ == "__main__":
    run()
