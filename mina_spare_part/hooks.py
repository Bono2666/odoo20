def set_part_categories_average_cost(env):
    """Spare parts are valued at their actual purchase cost (AVCO) in every company:
    the selling price formula then only provides the initial cost of a part without stock."""
    categories = env['product.category'].search([('part_type', '!=', False)])
    root = env.ref('mina_spare_part.product_category_spare_part', raise_if_not_found=False)
    categories |= root or env['product.category']
    for company in env['res.company'].search([]):
        categories.with_company(company).property_cost_method = 'average'


def post_init_hook(env):
    set_part_categories_average_cost(env)
