// Copyright (c) 2025, osama.ahmed@deliverydevs.com and contributors
// For license information, please see license.txt

frappe.query_reports["FBR Sales Tax Report - Balochistan"] = {
	"filters": [
		{
			"fieldname": "company",
			"label": __("Company"),
			"fieldtype": "Link",
			"options": "Company",
			"default": frappe.defaults.get_user_default("Company"),
			"reqd": 1
		},
		{
			"fieldname": "from_date",
			"label": __("From Date"),
			"fieldtype": "Date",
			"default": frappe.datetime.add_months(frappe.datetime.get_today(), -1),
			"reqd": 1
		},
		{
			"fieldname": "to_date",
			"label": __("To Date"),
			"fieldtype": "Date",
			"default": frappe.datetime.get_today(),
			"reqd": 1
		},
		{
			"fieldname": "customer",
			"label": __("Buyer"),
			"fieldtype": "Link",
			"options": "Customer",
			"reqd": 0
		},
		{
			"fieldname": "report_status",
			"label": __("Report Status"),
			"fieldtype": "Select",
			"options": "Submitted\nDraft\nCancelled",
			"default": "Submitted",
			"reqd": 0
		},
		{
			"fieldname": "fbr_status",
			"label": __("FBR Status"),
			"fieldtype": "Select",
			"options": "\nValid\nInvalid",
			"reqd": 0
		}
	]
};