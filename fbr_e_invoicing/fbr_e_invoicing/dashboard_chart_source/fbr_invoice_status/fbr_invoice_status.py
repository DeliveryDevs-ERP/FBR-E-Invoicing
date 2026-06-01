import frappe
from frappe import _


@frappe.whitelist()
def get(
    chart_name: str | None = None,
    chart: dict | str | None = None,
    no_cache: bool | int | str | None = None,
    filters: list | dict | str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    timespan: str | None = None,
    time_interval: str | None = None,
    heatmap_year: int | str | None = None,
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
