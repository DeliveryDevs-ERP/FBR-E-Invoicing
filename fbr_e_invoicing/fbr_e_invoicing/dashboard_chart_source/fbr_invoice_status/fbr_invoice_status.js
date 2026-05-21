frappe.provide("frappe.dashboards.chart_sources");

frappe.dashboards.chart_sources["FBR Invoice Status"] = {
	method:
		"fbr_e_invoicing.fbr_e_invoicing.dashboard_chart_source.fbr_invoice_status.fbr_invoice_status.get",
};
