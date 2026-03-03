from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, now_datetime, random_string

from fbr_e_invoicing.coa_setup.overrides.company import (
    ensure_pakistan_tax_accounts,
    on_company_update_setup_pakistan,
    setup_pakistan_for_existing_companies,
    setup_pakistan_tax_accounts_and_item_templates,
)


class TestPakistanCompanyFixtures(IntegrationTestCase):
    @staticmethod
    def _province_template_defaults():
        return frappe.get_file_json(
            frappe.get_app_path(
                "fbr_e_invoicing", "data", "province_tax_charge_template_defaults.json"
            )
        ).get("province_tax_charge_templates", [])

    @staticmethod
    def _current_year() -> int:
        return now_datetime().year

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.db.savepoint("before_pakistan_company_fixtures")
        cls.company = cls._create_company(country="Pakistan")

    @classmethod
    def tearDownClass(cls):
        frappe.db.rollback(save_point="before_pakistan_company_fixtures")
        super().tearDownClass()

    @classmethod
    def _create_company(cls, country: str):
        suffix = random_string(6).upper()
        company = frappe.new_doc("Company")
        company.update(
            {
                "abbr": f"PF{suffix[:2]}",
                "company_name": f"_Pakistan Fixture {suffix}",
                "country": country,
                "default_currency": "PKR",
                "domain": "Manufacturing",
                "chart_of_accounts": "Standard",
                "enable_perpetual_inventory": 0,
            }
        )
        company.insert()
        return company

    def test_setup_creates_tax_hierarchy_and_item_templates(self):
        setup_pakistan_tax_accounts_and_item_templates(self.company.name)
        current_year = self._current_year()
        province_rows = self._province_template_defaults()

        self.assertTrue(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Sindh",
                    "is_group": 1,
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Sales Tax Services 17%",
                    "is_group": 0,
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Item Tax Template",
                {
                    "company": self.company.name,
                    "title": "Sales Tax Services 17%",
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Sales Taxes and Charges Template",
                {
                    "company": self.company.name,
                    "title": f"Sindh Sales Tax {current_year}",
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Purchase Taxes and Charges Template",
                {
                    "company": self.company.name,
                    "title": f"Sindh Purchase Tax {current_year}",
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Gilgit Baltistan",
                    "is_group": 1,
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Standard Purchases Service Tax Gilgit Baltistan 0%",
                    "is_group": 0,
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Standard Sales Service Tax Gilgit Baltistan 0%",
                    "is_group": 0,
                },
            )
        )
        self.assertFalse(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Khyber Paktunkhwa",
                    "is_group": 1,
                },
            )
        )
        self.assertFalse(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Khyber Paktunkha",
                    "is_group": 1,
                },
            )
        )
        for row in province_rows:
            sales_title = f"{row['province_label']} Sales Tax {current_year}"
            purchase_title = f"{row['province_label']} Purchase Tax {current_year}"

            sales_name = frappe.db.get_value(
                "Sales Taxes and Charges Template",
                {
                    "company": self.company.name,
                    "title": sales_title,
                    "tax_category": row["tax_category"],
                },
                "name",
            )
            self.assertTrue(sales_name, f"Missing sales template: {sales_title}")
            sales_doc = frappe.get_doc("Sales Taxes and Charges Template", sales_name)
            self.assertEqual(len(sales_doc.taxes), 1)
            expected_sales_account = frappe.db.get_value(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": row["sales_account_name"],
                },
                "name",
            )
            self.assertEqual(sales_doc.taxes[0].account_head, expected_sales_account)
            self.assertEqual(flt(sales_doc.taxes[0].rate), flt(row["sales_rate"]))

            purchase_name = frappe.db.get_value(
                "Purchase Taxes and Charges Template",
                {
                    "company": self.company.name,
                    "title": purchase_title,
                    "tax_category": row["tax_category"],
                },
                "name",
            )
            self.assertTrue(purchase_name, f"Missing purchase template: {purchase_title}")
            purchase_doc = frappe.get_doc(
                "Purchase Taxes and Charges Template", purchase_name
            )
            self.assertEqual(len(purchase_doc.taxes), 1)
            expected_purchase_account = frappe.db.get_value(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": row["purchase_account_name"],
                },
                "name",
            )
            self.assertEqual(
                purchase_doc.taxes[0].account_head,
                expected_purchase_account,
            )
            self.assertEqual(
                flt(purchase_doc.taxes[0].rate),
                flt(row["purchase_rate"]),
            )

        gilgit_sales_name = frappe.db.get_value(
            "Sales Taxes and Charges Template",
            {
                "company": self.company.name,
                "title": f"Gilgit Baltistan Sales Tax {current_year}",
                "tax_category": "GILGIT BALTISTAN",
            },
            "name",
        )
        gilgit_purchase_name = frappe.db.get_value(
            "Purchase Taxes and Charges Template",
            {
                "company": self.company.name,
                "title": f"Gilgit Baltistan Purchase Tax {current_year}",
                "tax_category": "GILGIT BALTISTAN",
            },
            "name",
        )
        self.assertTrue(gilgit_sales_name)
        self.assertTrue(gilgit_purchase_name)

        gilgit_sales = frappe.get_doc("Sales Taxes and Charges Template", gilgit_sales_name)
        gilgit_purchase = frappe.get_doc(
            "Purchase Taxes and Charges Template", gilgit_purchase_name
        )
        self.assertEqual(flt(gilgit_sales.taxes[0].rate), 0.0)
        self.assertEqual(flt(gilgit_purchase.taxes[0].rate), 0.0)

        generic_sales_zero = frappe.db.get_value(
            "Account",
            {"company": self.company.name, "account_name": "Sales Tax Services 0%"},
            "name",
        )
        generic_purchase_zero = frappe.db.get_value(
            "Account",
            {"company": self.company.name, "account_name": "Purchases Tax Services 0%"},
            "name",
        )
        if generic_sales_zero:
            self.assertNotEqual(gilgit_sales.taxes[0].account_head, generic_sales_zero)
        if generic_purchase_zero:
            self.assertNotEqual(gilgit_purchase.taxes[0].account_head, generic_purchase_zero)

    def test_idempotent_rerun(self):
        setup_pakistan_tax_accounts_and_item_templates(self.company.name)
        second = setup_pakistan_tax_accounts_and_item_templates(self.company.name)

        self.assertEqual(second["accounts_created"], 0)
        self.assertEqual(second["item_templates_created"], 0)
        self.assertEqual(second["sales_templates_created"], 0)
        self.assertEqual(second["purchase_templates_created"], 0)
        self.assertEqual(second["sales_template_conflicts"], 0)
        self.assertEqual(second["purchase_template_conflicts"], 0)
        self.assertFalse(second["errors"])

    def test_non_pakistan_company_update_is_ignored(self):
        doc = frappe._dict({"country": "India", "name": self.company.name})

        with patch(
            "fbr_e_invoicing.coa_setup.overrides.company.setup_pakistan_tax_accounts_and_item_templates"
        ) as mocked:
            on_company_update_setup_pakistan(doc)
            mocked.assert_not_called()

    def test_custom_sales_template_conflict_is_not_auto_disabled(self):
        conflict_company = self._create_company(country="Pakistan")
        ensure_pakistan_tax_accounts(conflict_company.name)

        sindh_sales_account = frappe.db.get_value(
            "Account",
            {
                "company": conflict_company.name,
                "account_name": "Standard Sales Tax Services 13% Sindh",
            },
            "name",
        )
        self.assertTrue(sindh_sales_account)

        custom_title = "Custom Sindh Sales Template"
        custom_doc = frappe.get_doc(
            {
                "doctype": "Sales Taxes and Charges Template",
                "company": conflict_company.name,
                "title": custom_title,
                "tax_category": "SINDH",
                "taxes": [
                    {
                        "charge_type": "On Net Total",
                        "account_head": sindh_sales_account,
                        "description": "Custom Sindh Sales Tax",
                        "rate": 13.0,
                    }
                ],
            }
        )
        custom_doc.insert(ignore_permissions=True)

        result = setup_pakistan_tax_accounts_and_item_templates(conflict_company.name)
        current_year = self._current_year()
        self.assertGreaterEqual(result["sales_template_conflicts"], 1)
        self.assertTrue(
            frappe.db.exists(
                "Sales Taxes and Charges Template",
                {
                    "company": conflict_company.name,
                    "title": custom_title,
                    "disabled": 0,
                },
            )
        )
        self.assertFalse(
            frappe.db.exists(
                "Sales Taxes and Charges Template",
                {
                    "company": conflict_company.name,
                    "title": f"Sindh Sales Tax {current_year}",
                },
            )
        )

    def test_managed_old_year_sales_template_is_disabled_on_rollover(self):
        rollover_company = self._create_company(country="Pakistan")
        ensure_pakistan_tax_accounts(rollover_company.name)

        current_year = self._current_year()
        previous_year = current_year - 1

        sindh_sales_account = frappe.db.get_value(
            "Account",
            {
                "company": rollover_company.name,
                "account_name": "Standard Sales Tax Services 13% Sindh",
            },
            "name",
        )
        self.assertTrue(sindh_sales_account)

        old_title = f"Sindh Sales Tax {previous_year}"
        old_doc = frappe.get_doc(
            {
                "doctype": "Sales Taxes and Charges Template",
                "company": rollover_company.name,
                "title": old_title,
                "tax_category": "SINDH",
                "taxes": [
                    {
                        "charge_type": "On Net Total",
                        "account_head": sindh_sales_account,
                        "description": "Sindh Sales Tax Previous Year",
                        "rate": 13.0,
                    }
                ],
            }
        )
        old_doc.insert(ignore_permissions=True)

        result = setup_pakistan_tax_accounts_and_item_templates(rollover_company.name)
        self.assertGreaterEqual(result["sales_templates_disabled"], 1)

        old_state = frappe.db.get_value(
            "Sales Taxes and Charges Template",
            {"company": rollover_company.name, "title": old_title},
            "disabled",
        )
        self.assertEqual(int(old_state or 0), 1)
        self.assertTrue(
            frappe.db.exists(
                "Sales Taxes and Charges Template",
                {
                    "company": rollover_company.name,
                    "title": f"Sindh Sales Tax {current_year}",
                    "tax_category": "SINDH",
                    "disabled": 0,
                },
            )
        )

    def test_existing_companies_setup_summary(self):
        summary = setup_pakistan_for_existing_companies(ignore_permissions=True)

        self.assertGreaterEqual(summary["companies_processed"], 1)
        self.assertIn("accounts_created", summary)
        self.assertIn("item_templates_created", summary)
        self.assertIn("sales_templates_created", summary)
        self.assertIn("purchase_templates_created", summary)
