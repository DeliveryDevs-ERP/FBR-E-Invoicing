import frappe
from frappe import _


@frappe.whitelist()
def get(
    chart_name=None,
    chart=None,
    no_cache=None,
    filters=None,
    from_date=None,
    to_date=None,
    timespan=None,
    time_interval=None,
    heatmap_year=None,
):
    """Combined Valid vs Invalid FBR invoice counts across Sales Invoice and POS Invoice.

    Returned in Frappe's standard chart shape so it renders directly in a Pie
    (or Donut / Bar) Dashboard Chart.
    """
    valid = _count_combined("Valid")
    invalid = _count_combined("Invalid")

    return {
        "labels": [_("Valid"), _("Invalid")],
        "datasets": [
            {
                "name": _("Invoices"),
                "values": [valid, invalid],
            }
        ],
    }


def _count_combined(status):
    si = frappe.db.count(
        "Sales Invoice",
        filters={"custom_fbr_status": status, "docstatus": 1},
    )
    pos = frappe.db.count(
        "POS Invoice",
        filters={"custom_fbr_status": status, "docstatus": 1},
    )
    return (si or 0) + (pos or 0)
