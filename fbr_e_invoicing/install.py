from fbr_e_invoicing.utils import sync_hs_codes
def after_install():
    sync_hs_codes()