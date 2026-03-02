import frappe


def check_app_permission():
    """Allow access for any authenticated (non-Guest) user."""
    return frappe.session.user != "Guest"
