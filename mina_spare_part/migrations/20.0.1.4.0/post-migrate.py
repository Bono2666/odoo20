import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.mina_spare_part.hooks import set_part_categories_average_cost

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    set_part_categories_average_cost(env)
    _logger.info("mina_spare_part: spare part categories set to Average Cost (AVCO)")
