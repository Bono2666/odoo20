def post_init_hook(env):
    """Use the contact tags named "Customer" and "Supplier" as filters, and accept the prices of the
    orders confirmed before the installation."""
    Category = env['res.partner.category'].with_context(active_test=False)
    customer = Category.search([('name', '=', 'Customer'), ('parent_id', '=', False)], limit=1)
    supplier = Category.search([('name', '=', 'Supplier'), ('parent_id', '=', False)], limit=1) \
        or Category.search([('name', '=', 'Vendor'), ('parent_id', '=', False)], limit=1)
    for company in env['res.company'].search([]):
        company.write({
            'mina_customer_category_id': company.mina_customer_category_id.id or customer.id,
            'mina_supplier_category_id': company.mina_supplier_category_id.id or supplier.id,
        })
    orders = env['sale.order'].search([
        ('state', 'not in', ('draft', 'sent')), ('mina_approval_state', 'not in', ('not_required', 'approved')),
    ])
    for order in orders:
        order.write({
            'mina_approval_request_signature': order._mina_approval_signature(),
            'mina_approval_decision': 'approved',
        })
