from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import random_string

from fbr_e_invoicing.coa_setup.overrides.company import (
    on_company_update_setup_pakistan,
    setup_pakistan_for_existing_companies,
    setup_pakistan_tax_accounts_and_item_templates,
)


class TestPakistanCompanyFixtures(IntegrationTestCase):
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
        self.assertFalse(
            frappe.db.exists(
                "Sales Taxes and Charges Template",
                {
                    "company": self.company.name,
                    "title": "Sales Tax Services 17%",
                },
            )
        )
        self.assertFalse(
            frappe.db.exists(
                "Purchase Taxes and Charges Template",
                {
                    "company": self.company.name,
                    "title": "Purchases Tax Services 17%",
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

    def test_idempotent_rerun(self):
        setup_pakistan_tax_accounts_and_item_templates(self.company.name)
        second = setup_pakistan_tax_accounts_and_item_templates(self.company.name)

        self.assertEqual(second["accounts_created"], 0)
        self.assertEqual(second["item_templates_created"], 0)
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
