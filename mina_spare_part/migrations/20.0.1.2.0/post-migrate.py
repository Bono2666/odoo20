import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Spare parts imported before 20.0.1.2.0 were created without inventory tracking."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    parts = env['product.template'].with_context(active_test=False, tracking_disable=True).search([
        ('part_type', '!=', False),
        ('type', '=', 'consu'),
        ('is_storable', '=', False),
    ])
    if parts:
        parts.write({'is_storable': True})
    _logger.info("mina_spare_part: inventory tracking enabled on %s spare parts", len(parts))
