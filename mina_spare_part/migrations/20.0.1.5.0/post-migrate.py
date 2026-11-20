import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# "Airend" is now written "Air End". The default records are noupdate: rename them here,
# unless the user already gave them another name.
RENAMES = [
    ('mina_spare_part.product_category_airend', "Airend (Compressor)", "Air End (Compressor)"),
    ('mina_spare_part.part_price_rule_airend', "Harga Part Airend (Compressor)", "Harga Part Air End (Compressor)"),
]


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {'active_test': False, 'tracking_disable': True})
    for xmlid, old_name, new_name in RENAMES:
        record = env.ref(xmlid, raise_if_not_found=False)
        if record and record.name == old_name:
            record.name = new_name
            _logger.info("%s renamed to %r", xmlid, new_name)
