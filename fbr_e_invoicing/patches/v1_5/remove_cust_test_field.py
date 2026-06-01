import frappe


def execute():
    """Drop the orphaned `cust_test` Custom Field from Customer.

    `sync_customizations` only upserts, so removing the field from the fixture
    is not enough — the existing Custom Field row must be deleted explicitly
    for the field to disappear from forms. The column is also dropped, but
    only when no Customer record has a non-empty value (defensive guard for
    other deployments where the field may have been repurposed).
    """
    name = "Customer-cust_test"
    if frappe.db.exists("Custom Field", name):
        frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)

    try:
        if frappe.db.has_column("Customer", "cust_test"):
            populated = frappe.db.sql(
                "SELECT COUNT(*) FROM `tabCustomer` "
                "WHERE cust_test IS NOT NULL AND cust_test != ''"
            )[0][0]
            if populated:
                frappe.log_error(
                    f"cust_test column on tabCustomer has {populated} non-empty "
                    f"value(s); column not dropped.",
                    "Drop cust_test column skipped",
                )
            else:
                frappe.db.sql_ddl("ALTER TABLE `tabCustomer` DROP COLUMN `cust_test`")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Drop cust_test column failed")

    frappe.clear_cache(doctype="Customer")
