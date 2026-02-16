from fbr_e_invoicing.utils import sync_hs_codes, populate_provinces, create_fbr_sale_types


def after_install():
    create_fbr_sale_types()
    populate_provinces()
    sync_hs_codes()
    from fbr_e_invoicing.tax_setup import setup_fbr_tax_artifacts

    setup_fbr_tax_artifacts()
