from fbr_e_invoicing.utils import sync_hs_codes, sync_provinces, create_fbr_sale_types
def after_install():
    create_fbr_sale_types()
    sync_hs_codes()
    sync_provinces()