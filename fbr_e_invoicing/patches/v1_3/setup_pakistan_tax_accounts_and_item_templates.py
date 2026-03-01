from fbr_e_invoicing.regional_compliance.overrides.company import (
    setup_pakistan_for_existing_companies,
)


def execute():
    setup_pakistan_for_existing_companies(ignore_permissions=True)

