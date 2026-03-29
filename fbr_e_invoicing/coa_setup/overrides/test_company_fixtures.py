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
                "custom_province": "SINDH",
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
            expected_sales_taxes = row["sales_taxes"]
            expected_purchase_taxes = row["purchase_taxes"]

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
            self.assertEqual(len(sales_doc.taxes), len(expected_sales_taxes))
            for tax_row, expected_tax in zip(sales_doc.taxes, expected_sales_taxes):
                expected_sales_account = frappe.db.get_value(
                    "Account",
                    {
                        "company": self.company.name,
                        "account_name": expected_tax["account_name"],
                    },
                    "name",
                )
                self.assertEqual(tax_row.account_head, expected_sales_account)
                self.assertEqual(flt(tax_row.rate), flt(expected_tax["rate"]))
                self.assertEqual(
                    (tax_row.description or "").strip(),
                    (expected_tax["description"] or "").strip(),
                )

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
            self.assertEqual(len(purchase_doc.taxes), len(expected_purchase_taxes))
            for tax_row, expected_tax in zip(purchase_doc.taxes, expected_purchase_taxes):
                expected_purchase_account = frappe.db.get_value(
                    "Account",
                    {
                        "company": self.company.name,
                        "account_name": expected_tax["account_name"],
                    },
                    "name",
                )
                self.assertEqual(tax_row.account_head, expected_purchase_account)
                self.assertEqual(flt(tax_row.rate), flt(expected_tax["rate"]))
                self.assertEqual(
                    (tax_row.description or "").strip(),
                    (expected_tax["description"] or "").strip(),
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

        current_assets = frappe.db.get_value(
            "Account",
            {
                "company": self.company.name,
                "account_name": "Current Assets",
                "is_group": 1,
                "root_type": "Asset",
            },
            "name",
        )
        withholding_asset_parent = frappe.db.get_value(
            "Account",
            {
                "company": self.company.name,
                "account_name": "Withholding Tax",
                "is_group": 1,
                "root_type": "Asset",
            },
            "parent_account",
        )
        self.assertTrue(current_assets)
        self.assertEqual(withholding_asset_parent, current_assets)

        current_liabilities = frappe.db.get_value(
            "Account",
            {
                "company": self.company.name,
                "account_name": "Current Liabilities",
                "is_group": 1,
                "root_type": "Liability",
            },
            "name",
        )
        withholding_liability_parent = frappe.db.get_value(
            "Account",
            {
                "company": self.company.name,
                "account_name": "Withholding Tax L",
                "is_group": 1,
                "root_type": "Liability",
            },
            "parent_account",
        )
        self.assertTrue(current_liabilities)
        self.assertEqual(withholding_liability_parent, current_liabilities)

        categories = withholding_defaults["tax_withholding_categories"]
        sales_category = next(
            (
                row
                for row in categories
                if "(sales)" in (row.get("name") or "").strip().lower()
            ),
            None,
        )
        purchase_category = next(
            (
                row
                for row in categories
                if "(purchases)" in (row.get("name") or "").strip().lower()
            ),
            None,
        )
        self.assertTrue(sales_category)
        self.assertTrue(purchase_category)

        def assert_withholding_category_link(category, expected_root_type):
            category_doc = frappe.get_doc("Tax Withholding Category", category["name"])
            self.assertEqual(category_doc.category_name or "", "")
            self.assertGreaterEqual(len(category_doc.rates), 1)
            self.assertTrue(
                any(
                    (row.company or "").strip() == self.company.name
                    for row in (category_doc.accounts or [])
                )
            )

            expected_account = frappe.db.get_value(
                "Account",
                {
                    "company": self.company.name,
                    "account_name": category["account_name"],
                    "root_type": expected_root_type,
                },
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

        assert_withholding_category_link(sales_category, "Asset")
        assert_withholding_category_link(purchase_category, "Liability")

        account_name_rows = frappe.db.sql(
            """
            select account_name
            from `tabAccount`
            where company = %s
              and (
                account_name like %s
                or account_name like %s
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
        self.assertEqual(second["sales_template_rows_added"], 0)
        self.assertEqual(second["purchase_template_rows_added"], 0)
        self.assertEqual(second["withholding_groups_created"], 0)
        self.assertEqual(second["withholding_categories_created"], 0)
        self.assertEqual(second["withholding_category_accounts_linked"], 0)
        self.assertEqual(second["withholding_category_rates_added"], 0)
        self.assertFalse(second["errors"])

    def test_existing_sindh_sales_template_is_backfilled(self):
        company = self._create_company(country="Pakistan")
        current_year = self._current_year()
        initial = setup_pakistan_tax_accounts_and_item_templates(company.name)
        self.assertFalse(initial["errors"])

        standard_sales_account = frappe.db.get_value(
            "Account",
            {
                "company": company.name,
                "account_name": "Standard Sales Service Tax Sindh 13%",
            },
            "name",
        )
        self.assertTrue(standard_sales_account)

        sales_name = frappe.db.get_value(
            "Sales Taxes and Charges Template",
            {
                "company": company.name,
                "title": f"Sindh Sales Tax {current_year}",
                "tax_category": "SINDH",
            },
            "name",
        )
        self.assertTrue(sales_name)
        existing_doc = frappe.get_doc("Sales Taxes and Charges Template", sales_name)
        existing_doc.set(
            "taxes",
            [
                {
                    "charge_type": "On Net Total",
                    "account_head": standard_sales_account,
                    "description": "Sindh Sales Tax",
                    "rate": 13.0,
                }
            ],
        )
        existing_doc.save(ignore_permissions=True)

        result = setup_pakistan_tax_accounts_and_item_templates(company.name)

        self.assertEqual(result["sales_templates_created"], 0)
        self.assertEqual(result["sales_template_rows_added"], 9)
        self.assertEqual(result["purchase_template_rows_added"], 0)
        self.assertFalse(result["errors"])

        sales_doc = frappe.get_doc("Sales Taxes and Charges Template", sales_name)
        expected_sindh_sales = next(
            row["sales_taxes"]
            for row in self._province_template_defaults()
            if row["tax_category"] == "SINDH"
        )
        self.assertEqual(len(sales_doc.taxes), len(expected_sindh_sales))

        for tax_row, expected_tax in zip(sales_doc.taxes, expected_sindh_sales):
            expected_account = frappe.db.get_value(
                "Account",
                {
                    "company": company.name,
                    "account_name": expected_tax["account_name"],
                },
                "name",
            )
            self.assertEqual(tax_row.account_head, expected_account)
            self.assertEqual(flt(tax_row.rate), flt(expected_tax["rate"]))
            self.assertEqual(
                (tax_row.description or "").strip(),
                (expected_tax["description"] or "").strip(),
            )

        second = setup_pakistan_tax_accounts_and_item_templates(company.name)
        self.assertEqual(second["sales_template_rows_added"], 0)
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
        self.assertIn("sales_template_rows_added", summary)
        self.assertIn("purchase_template_rows_added", summary)
        self.assertIn("withholding_groups_created", summary)
        self.assertIn("withholding_categories_created", summary)
