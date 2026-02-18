// Copyright (c) 2025, osama.ahmed@deliverydevs.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("FBR E-Inv Setup", {
	refresh(frm) {
		toggle_fetch_master_data_button(frm);
	},
	after_save(frm) {
		toggle_fetch_master_data_button(frm);
	},
});

function has_required_fbr_credentials(frm) {
	const api_endpoint = (frm.doc.api_endpoint || "").trim();
	const pral_authorization_token = (frm.doc.pral_authorization_token || "").trim();
	return Boolean(api_endpoint && pral_authorization_token);
}

function toggle_fetch_master_data_button(frm) {
	const label = __("Fetch Master Data");
	frm.page.remove_inner_button(label);

	if (!has_required_fbr_credentials(frm)) {
		return;
	}

	frm.page.add_inner_button(label, () => {
		frappe.call({
			method: "fbr_e_invoicing.utils.run_master_data_sync",
			freeze: true,
			freeze_message: __("Fetching Master Data..."),
			callback: () => frm.reload_doc(),
		});
	});
}
