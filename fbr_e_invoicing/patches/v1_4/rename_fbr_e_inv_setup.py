import frappe

OLD = "FBR E-Inv Setup"
NEW = "FBR E-Invoicing Setup"


def execute():
	_rename_doctype()
	_fix_workspace_references()
	frappe.db.commit()
	frappe.clear_cache()


def _rename_doctype():
	if not frappe.db.exists("DocType", OLD):
		return
	if frappe.db.exists("DocType", NEW):
		frappe.delete_doc("DocType", OLD, force=True, ignore_missing=True)
		return
	frappe.rename_doc("DocType", OLD, NEW, force=True, merge=False)


def _fix_workspace_references():
	# Covers the case where a user customised the workspace through the UI,
	# which can stop Frappe's workspace sync from overwriting stale link_to /
	# label values. Idempotent: matches zero rows on subsequent runs.
	for doctype in ("Workspace Link", "Workspace Shortcut"):
		if not frappe.db.table_exists(doctype):
			continue
		frappe.db.sql(
			f"UPDATE `tab{doctype}` SET link_to = %(new)s WHERE link_to = %(old)s",
			{"old": OLD, "new": NEW},
		)
		frappe.db.sql(
			f"UPDATE `tab{doctype}` SET label = %(new)s WHERE label = %(old)s",
			{"old": OLD, "new": NEW},
		)
