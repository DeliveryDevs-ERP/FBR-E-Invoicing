# Copyright (c) 2025, osama.ahmed@deliverydevs.com and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fbr_e_invoicing.utils import get_fbr_setup_status


class TestFBREInvoicingSetup(FrappeTestCase):
    def test_setup_button_exists(self):
        meta = frappe.get_meta("FBR E-Invoicing Setup")
        self.assertTrue(meta.has_field("setup_tax_accounts_templates"))
        self.assertEqual(
            meta.get_field("setup_tax_accounts_templates").label,
            "Setup Tax Accounts + Templates",
        )

    def test_setup_status_behavior_unchanged(self):
        def get_single_value(_doctype, fieldname):
            return {"api_endpoint": ""}.get(fieldname)

        with patch(
            "fbr_e_invoicing.utils.frappe.get_roles",
            return_value=["System Manager"],
        ), patch(
            "fbr_e_invoicing.utils._has_setup_field",
            return_value=False,
        ), patch(
            "fbr_e_invoicing.utils.frappe.db.get_single_value",
            side_effect=get_single_value,
        ), patch(
            "fbr_e_invoicing.utils.frappe.db.has_column",
            return_value=False,
        ), patch(
            "fbr_e_invoicing.utils.frappe.defaults.get_global_default",
            return_value="",
        ):
            status = get_fbr_setup_status()

        self.assertEqual(
            set(status.keys()),
            {
                "endpoint_missing",
                "token_missing",
                "default_company",
                "master_data_missing",
                "province_missing",
                "companies_missing_province",
                "show_instructions",
            },
        )
        self.assertTrue(status["show_instructions"])
        self.assertTrue(status["endpoint_missing"])
        self.assertTrue(status["token_missing"])
