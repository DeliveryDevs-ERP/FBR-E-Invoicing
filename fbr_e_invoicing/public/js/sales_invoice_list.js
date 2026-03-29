frappe.listview_settings["Sales Invoice"] = {
    onload(listview) {
        listview.page.add_action_item(__("Submit to FBR"), async () => {
            const selected_items = listview.get_checked_items() || [];
            if (!selected_items.length) {
                frappe.msgprint(__("Please select Sales Invoices first."));
                return;
            }

            const docnames = selected_items.map((row) => row.name).filter(Boolean);

            try {
                const r = await frappe.call({
                    method: "fbr_e_invoicing.api.fbr_submission.bulk_submit_sales_invoices",
                    args: {docnames},
                    freeze: true,
                    freeze_message: __("Queuing Sales Invoices for FBR submission..."),
                });

                const result = r?.message || {};
                const result_rows = result.results || {};
                const queued_count = Number(result.queued_count || 0);
                const draft_invoices =
                    result.draft_invoices || result_rows.drafts_skipped || [];
                const cancelled_invoices =
                    result.cancelled_invoices || result_rows.cancelled_skipped || [];
                const already_submitted_invoices =
                    result.already_submitted_invoices ||
                    result_rows.already_submitted_skipped ||
                    [];
                const raw_failed_invoices =
                    result.failed_invoices || result_rows.failed_to_queue || [];
                const failed_invoices = raw_failed_invoices.map((row) => {
                    if (row && typeof row === "object") {
                        return {
                            name: row.name || "",
                            error: row.error || __("Unknown error"),
                        };
                    }
                    const raw_text = String(row || "").trim();
                    const matches = raw_text.match(/^(.*)\s+\((.*)\)$/);
                    if (matches) {
                        return {
                            name: matches[1],
                            error: matches[2],
                        };
                    }
                    return {
                        name: raw_text,
                        error: __("Unknown error"),
                    };
                });
                const queue_route = result.queue_route || "/app/fbr-queue";

                let message = __(
                    "{0} report(s) have been queued for submission. (Note: Invoices in Draft or Cancelled status are not posted to FBR.)",
                    [queued_count]
                );

                message +=
                    "<br><br>" +
                    __(
                        "To check the status, go to <a href=\"{0}\">FBR Queue logs</a>.",
                        [queue_route]
                    );

                if (draft_invoices.length) {
                    message +=
                        "<br><br>" +
                        __(
                            "The following invoices were not submitted because their status is Draft:"
                        );
                    message += "<br>" + draft_invoices.join("<br>");
                }

                if (cancelled_invoices.length) {
                    message +=
                        "<br><br>" +
                        __(
                            "The following invoices were not submitted because their status is Cancelled:"
                        );
                    message += "<br>" + cancelled_invoices.join("<br>");
                }

                if (already_submitted_invoices.length) {
                    message +=
                        "<br><br>" +
                        __(
                            "The following invoices were skipped because they are already submitted to FBR:"
                        );
                    message += "<br>" + already_submitted_invoices.join("<br>");
                }

                if (failed_invoices.length) {
                    message +=
                        "<br><br>" + __("The following invoices failed to queue:");
                    message +=
                        "<br>" +
                        failed_invoices
                            .map((row) => `${row.name}: ${row.error || __("Unknown error")}`)
                            .join("<br>");
                }

                frappe.msgprint({
                    title: __("Bulk Submit to FBR"),
                    indicator: queued_count > 0 ? "green" : "orange",
                    message,
                });

                listview.refresh();
            } catch (error) {
                frappe.msgprint({
                    title: __("Error"),
                    indicator: "red",
                    message: __(error?.message || "Failed to queue Sales Invoices."),
                });
            }
        });
    },
};
