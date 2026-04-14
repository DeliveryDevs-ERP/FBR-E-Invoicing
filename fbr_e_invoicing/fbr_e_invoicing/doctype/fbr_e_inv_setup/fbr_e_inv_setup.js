// Copyright (c) 2025, osama.ahmed@deliverydevs.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("FBR E-Inv Setup", {
	refresh(frm) {
		// Keep any existing refresh logic here
	},

	fetch_master_data(frm) {
		if (!has_required_fbr_credentials(frm)) {
			frappe.msgprint(__("Please save the API Endpoint and Authorization Token first."));
			return;
		}

		frappe.call({
			method: "fbr_e_invoicing.utils.run_master_data_sync",
			freeze: true,
			freeze_message: __("Fetching Master Data..."),
			callback: (r) => {
				const result = (r && r.message) || {};
				if (result.success) {
					frappe.show_alert({ message: __("Master Data Synced Successfully"), indicator: "green" });
				}
				frm.reload_doc();
			}
		});
	},

	setup_tax_accounts_templates(frm) {
		frappe.call({
			method: "fbr_e_invoicing.coa_setup.overrides.company.setup_pakistan_for_existing_companies",
			freeze: true,
			freeze_message: __("Setting up Pakistan tax accounts, templates, and withholding masters..."),
			callback: (r) => {
				const summary = (r && r.message) || {};
				const accountsCreated = to_int(summary.accounts_created);
				const itemTemplatesCreated = to_int(summary.item_templates_created);
				const salesTemplatesCreated = to_int(summary.sales_templates_created);
				const purchaseTemplatesCreated = to_int(summary.purchase_templates_created);
				const salesTemplateRowsAdded = to_int(summary.sales_template_rows_added);
				const purchaseTemplateRowsAdded = to_int(summary.purchase_template_rows_added);
				const errors = summary.errors || [];

				if (
					accountsCreated === 0 &&
					itemTemplatesCreated === 0 &&
					salesTemplatesCreated === 0 &&
					purchaseTemplatesCreated === 0 &&
					salesTemplateRowsAdded === 0 &&
					purchaseTemplateRowsAdded === 0 &&
					!errors.length
				) {
					frappe.msgprint(__("Accounts have already been set up."));
					return;
				}

				frappe.msgprint({
					title: __("Pakistan Tax Setup"),
					indicator: errors.length ? "orange" : "green",
					message: render_pakistan_setup_summary(summary),
				});
			},
		});
	}
});

function has_required_fbr_credentials(frm) {
	const api_endpoint = (frm.doc.api_endpoint || "").trim();
	const pral_authorization_token = (frm.doc.pral_authorization_token || "").trim();
	return Boolean(api_endpoint && pral_authorization_token);
}

function render_pakistan_setup_summary(summary) {
	const errors = summary.errors || [];
	const rows = [
		`<b>${__("Companies Processed")}:</b> ${to_int(summary.companies_processed)}`,
		`<b>${__("Accounts Created")}:</b> ${to_int(summary.accounts_created)}`,
		`<b>${__("Accounts Skipped")}:</b> ${to_int(summary.accounts_skipped)}`,
		`<b>${__("Item Templates Created")}:</b> ${to_int(summary.item_templates_created)}`,
		`<b>${__("Item Templates Skipped")}:</b> ${to_int(summary.item_templates_skipped)}`,
		`<b>${__("Sales Templates Created")}:</b> ${to_int(summary.sales_templates_created)}`,
		`<b>${__("Sales Templates Skipped")}:</b> ${to_int(summary.sales_templates_skipped)}`,
		`<b>${__("Sales Template Rows Added")}:</b> ${to_int(summary.sales_template_rows_added)}`,
		`<b>${__("Purchase Templates Created")}:</b> ${to_int(summary.purchase_templates_created)}`,
		`<b>${__("Purchase Templates Skipped")}:</b> ${to_int(summary.purchase_templates_skipped)}`,
		`<b>${__("Purchase Template Rows Added")}:</b> ${to_int(summary.purchase_template_rows_added)}`,
	];

	if (errors.length) {
		rows.push(`<b>${__("Errors")}:</b> ${errors.length}`);
	}

	return rows.join("<br>");
}

function to_int(value) {
	return Number.parseInt(value || 0, 10) || 0;
}
