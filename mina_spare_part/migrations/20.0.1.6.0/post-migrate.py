import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Tag the reseller prices written by the price rules so they can be removed when the
    rule's reseller pricelist changes."""
    env = api.Environment(cr, SUPERUSER_ID, {'active_test': False})
    for rule in env['mina.part.price.rule'].search([('reseller_pricelist_id', '!=', False)]):
        items = env['product.pricelist.item'].search([
            ('pricelist_id', '=', rule.reseller_pricelist_id.id),
            ('applied_on', '=', '1_product'),
            ('compute_price', '=', 'fixed'),
            ('date_start', '=', False),
            ('date_end', '=', False),
            ('mina_part_rule_id', '=', False),
            ('product_tmpl_id', 'in', rule._get_products().ids),
        ])
        items.write({'mina_part_rule_id': rule.id})
        _logger.info("%s reseller prices of %s tagged", len(items), rule.name)
