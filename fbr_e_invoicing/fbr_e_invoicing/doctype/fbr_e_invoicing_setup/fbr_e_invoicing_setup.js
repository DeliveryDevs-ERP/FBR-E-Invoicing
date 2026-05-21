// Copyright (c) 2025, osama.ahmed@deliverydevs.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("FBR E-Invoicing Setup", {
	refresh(frm) {
		// Keep any existing refresh logic here
	},

	fetch_master_data(frm) {
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

	// setup_tax_accounts_templates handler removed — backend function kept for future use.
});

function to_int(value) {
	return Number.parseInt(value || 0, 10) || 0;
}
