import frappe

from fbr_e_invoicing.api.build_fbr_payload import build_pos_fbr_payload


@frappe.whitelist()
def get(doc: object, method: str | None = None):
    """
    Deprecated compatibility shim.
    POS payload is now built on demand.
    """
    docname = doc.name if hasattr(doc, "name") else str(doc)
    return build_pos_fbr_payload(docname)
