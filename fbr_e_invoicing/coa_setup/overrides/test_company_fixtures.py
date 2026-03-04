from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, now_datetime, random_string

from fbr_e_invoicing.coa_setup.overrides.company import (
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
    def _withholding_defaults():
        return frappe.get_file_json(
            frappe.get_app_path(
                "fbr_e_invoicing", "data", "tax_withholding_defaults.json"
            )
        )

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

    def test_setup_creates_tax_accounts_templates_and_withholding(self):
        setup_pakistan_tax_accounts_and_item_templates(self.company.name)
        current_year = self._current_year()
        province_rows = self._province_template_defaults()
        withholding_defaults = self._withholding_defaults()

        self.assertTrue(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Khyber Pakhtunkhwa",
                    "is_group": 1,
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Khyber Paktunkhwa",
                    "is_group": 1,
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": "Sales Service Tax 17%",
                    "is_group": 0,
                },
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Item Tax Template",
                {
                    "company": self.company.name,
                    "title": "Sales Service Tax 17% Sindh",
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

        first_group = withholding_defaults["tax_withholding_groups"][0]
        self.assertTrue(
            frappe.db.exists(
                "Tax Withholding Group",
                {
                    "group_name": first_group,
                },
            )
        )

        first_category = withholding_defaults["tax_withholding_categories"][0]
        category_doc = frappe.get_doc("Tax Withholding Category", first_category["name"])
        self.assertEqual(category_doc.category_name or "", "")
        self.assertGreaterEqual(len(category_doc.rates), 1)
        self.assertTrue(
            any(
                (row.company or "").strip() == self.company.name
                for row in (category_doc.accounts or [])
            )
        )
        expected_root_type = None
        normalized_category_name = (first_category["name"] or "").lower()
        if "(purchases)" in normalized_category_name:
            expected_root_type = "Liability"
        elif "(sales)" in normalized_category_name:
            expected_root_type = "Asset"

        account_filters = {
            "company": self.company.name,
            "account_name": first_category["account_name"],
        }
        if expected_root_type:
            account_filters["root_type"] = expected_root_type
        expected_account = frappe.db.get_value(
            "Account",
            account_filters,
            "name",
        )
        self.assertTrue(expected_account)
        self.assertTrue(
            any(
                (row.company or "").strip() == self.company.name
                and (row.account or "").strip() == expected_account
                for row in (category_doc.accounts or [])
            )
        )

        account_name_rows = frappe.db.sql(
            """
            select account_name
            from `tabAccount`
            where company = %s
              and (
                account_name like %s
                or account_name like %s
              )
            """,
            (
                self.company.name,
                "%- ND%",
                "%- DD%",
                "%Paktunkwa%",
            ),
        )
        self.assertFalse(account_name_rows)

        item_template_rows = frappe.db.sql(
            """
            select title
            from `tabItem Tax Template`
            where company = %s
              and (
                title like %s
                or title like %s
              )
            """,
            (self.company.name, "%- ND%", "%- DD%"),
        )
        self.assertFalse(item_template_rows)

    def test_idempotent_rerun(self):
        setup_pakistan_tax_accounts_and_item_templates(self.company.name)
        second = setup_pakistan_tax_accounts_and_item_templates(self.company.name)

        self.assertEqual(second["accounts_created"], 0)
        self.assertEqual(second["item_templates_created"], 0)
        self.assertEqual(second["sales_templates_created"], 0)
        self.assertEqual(second["purchase_templates_created"], 0)
        self.assertEqual(second["withholding_groups_created"], 0)
        self.assertEqual(second["withholding_categories_created"], 0)
        self.assertEqual(second["withholding_category_accounts_linked"], 0)
        self.assertEqual(second["withholding_category_rates_added"], 0)
        self.assertFalse(second["errors"])

    def test_non_pakistan_company_update_is_ignored(self):
        doc = frappe._dict({"country": "India", "name": self.company.name})

        with patch(
            "fbr_e_invoicing.coa_setup.overrides.company.setup_pakistan_tax_accounts_and_item_templates"
        ) as mocked:
            on_company_update_setup_pakistan(doc)
            mocked.assert_not_called()

    def test_existing_companies_setup_summary(self):
        summary = setup_pakistan_for_existing_companies(ignore_permissions=True)

        self.assertGreaterEqual(summary["companies_processed"], 1)
        self.assertIn("accounts_created", summary)
        self.assertIn("item_templates_created", summary)
        self.assertIn("sales_templates_created", summary)
        self.assertIn("purchase_templates_created", summary)
        self.assertIn("withholding_groups_created", summary)
        self.assertIn("withholding_categories_created", summary)
