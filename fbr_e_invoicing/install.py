from fbr_e_invoicing.utils import (
    sync_hs_codes,
    sync_uoms,
    populate_provinces,
    create_fbr_sale_types,
)
from fbr_e_invoicing.coa_setup.overrides.company import (
    setup_pakistan_for_existing_companies,
)


def after_install():
    create_fbr_sale_types()
    populate_provinces()
    sync_hs_codes()
    sync_uoms()


def after_sync():
    # Run Pakistan COA/template setup after fixtures are synced, so Tax Category links exist.
    setup_pakistan_for_existing_companies(ignore_permissions=True)
