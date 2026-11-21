from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.mina_transaction_control.hooks import post_init_hook


@tagged('post_install', '-at_install')
class TestTransactionControl(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        Category = cls.env['res.partner.category']
        cls.customer_tag = Category.create({'name': "Customer Test"})
        cls.end_customer_tag = Category.create({'name': "End Customer Test", 'parent_id': cls.customer_tag.id})
        cls.supplier_tag = Category.create({'name': "Supplier Test"})
        cls.vendor_tag = Category.create({'name': "Vendor Test", 'parent_id': cls.supplier_tag.id})
        cls.company.write({
            'mina_customer_category_id': cls.customer_tag.id,
            'mina_supplier_category_id': cls.supplier_tag.id,
        })
        Partner = cls.env['res.partner']
        cls.customer = Partner.create({
            'name': "PT Customer", 'is_company': True, 'category_id': [Command.set(cls.end_customer_tag.ids)],
        })
        cls.customer_contact = Partner.create({'name': "Budi", 'parent_id': cls.customer.id})
        cls.untagged = Partner.create({'name': "Tanpa Tag"})
        cls.vendor = Partner.create({'name': "PT Vendor", 'category_id': [Command.set(cls.vendor_tag.ids)]})
        cls.partners = cls.customer | cls.customer_contact | cls.untagged | cls.vendor

        cls.salesman = new_test_user(cls.env, 'mina_salesman', groups='sales_team.group_sale_salesman')
        cls.manager = new_test_user(cls.env, 'mina_sale_manager', groups='sales_team.group_sale_manager')
        cls.product = cls.env['product.product'].create({'name': "Filter Oli", 'list_price': 100000.0, 'taxes_id': False})
        cls.pricelist = cls.env['product.pricelist'].create({'name': "Harga Jual", 'currency_id': cls.company.currency_id.id})

    # ------------------------------------------------------------------
    # Partner filter
    # ------------------------------------------------------------------

    def _allowed_partners(self, model, category, **values):
        domain = self.env[model].fields_get(['partner_id'])['partner_id']['domain']
        context = {'mina_partner_category_id': category.id, 'company_id': self.company.id, **values}
        return self.env['res.partner'].search(safe_eval(domain, context) + [('id', 'in', self.partners.ids)])

    def test_partner_filter(self):
        self.assertEqual(self.env['sale.order'].new({}).mina_partner_category_id, self.customer_tag)
        self.assertEqual(self.env['purchase.order'].new({}).mina_partner_category_id, self.supplier_tag)
        self.assertEqual(
            self._allowed_partners('sale.order', self.customer_tag), self.customer | self.customer_contact,
        )
        self.assertEqual(self._allowed_partners('purchase.order', self.supplier_tag), self.vendor)

        Move = self.env['account.move']
        self.assertEqual(Move.new({'move_type': 'out_invoice'}).mina_partner_category_id, self.customer_tag)
        self.assertEqual(Move.new({'move_type': 'out_refund'}).mina_partner_category_id, self.customer_tag)
        self.assertEqual(Move.new({'move_type': 'in_invoice'}).mina_partner_category_id, self.supplier_tag)
        self.assertFalse(Move.new({'move_type': 'entry'}).mina_partner_category_id)
        self.assertEqual(self._allowed_partners('account.move', self.supplier_tag), self.vendor)

        # Without tag on the company: no filter.
        self.company.mina_customer_category_id = False
        self.assertFalse(self.env['sale.order'].new({}).mina_partner_category_id)
        self.assertEqual(self._allowed_partners('sale.order', self.env['res.partner.category']), self.partners)

    def test_post_init_hook_sets_tags_by_name(self):
        self.company.write({'mina_customer_category_id': False, 'mina_supplier_category_id': False})
        customer = self.env['res.partner.category'].search([('name', '=', 'Customer'), ('parent_id', '=', False)], limit=1) \
            or self.env['res.partner.category'].create({'name': "Customer"})
        supplier = self.env['res.partner.category'].search([('name', '=', 'Supplier'), ('parent_id', '=', False)], limit=1) \
            or self.env['res.partner.category'].create({'name': "Supplier"})
        # A top-level "Vendor" tag exists too: "Supplier" is preferred.
        self.env['res.partner.category'].create({'name': "Vendor"})
        post_init_hook(self.env)
        self.assertEqual(self.company.mina_customer_category_id, customer)
        self.assertEqual(self.company.mina_supplier_category_id, supplier)

    # ------------------------------------------------------------------
    # Price approval
    # ------------------------------------------------------------------

    def _order(self, user=None, **line_vals):
        order = self.env['sale.order'].with_user(user or self.salesman).create({
            'partner_id': self.customer.id,
            'pricelist_id': self.pricelist.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_uom_qty': 2, **line_vals})],
        })
        return order

    def test_price_at_or_above_pricelist_needs_no_approval(self):
        order = self._order()
        line = order.order_line
        self.assertEqual((line.price_unit, line.mina_reference_price), (100000.0, 100000.0))
        self.assertEqual(order.mina_approval_state, 'not_required')
        line.price_unit = 120000.0
        self.assertEqual(order.mina_approval_state, 'not_required')
        order.action_confirm()
        self.assertEqual(order.state, 'sale')

    def test_pricelist_discount_needs_no_approval(self):
        self.env['product.pricelist.item'].create({
            'pricelist_id': self.pricelist.id, 'applied_on': '3_global',
            'compute_price': 'discount', 'price_discount': 10.0,
        })
        order = self._order()
        line = order.order_line
        self.assertAlmostEqual(line.mina_reference_price, 90000.0)
        self.assertAlmostEqual(line.price_unit * (1 - line.discount / 100), 90000.0)
        self.assertEqual(order.mina_approval_state, 'not_required')

    def test_lower_price_needs_approval(self):
        order = self._order(price_unit=90000.0)
        line = order.order_line
        self.assertTrue(line.mina_needs_approval)
        self.assertEqual(order.mina_approval_state, 'to_request')
        with self.assertRaisesRegex(UserError, "perlu approval harga"):
            order.action_confirm()
        with self.assertRaises(UserError):
            order.action_mina_approve()

        order.action_mina_request_approval()
        self.assertEqual(order.mina_approval_state, 'pending')
        self.assertEqual(order.mina_approval_requested_by_id, self.salesman)
        activities = order.activity_ids
        self.assertIn(self.manager, activities.user_id)
        self.assertNotIn(self.salesman, activities.user_id)
        with self.assertRaisesRegex(UserError, "perlu approval harga"):
            order.action_confirm()

        order.with_user(self.manager).action_mina_approve()
        self.assertEqual(order.mina_approval_state, 'approved')
        self.assertEqual(order.mina_approval_user_id, self.manager)
        self.assertFalse(order.activity_ids)

        # A further change of the approved line requires a new approval.
        line.price_unit = 85000.0
        self.assertEqual(order.mina_approval_state, 'to_request')
        line.price_unit = 90000.0
        self.assertEqual(order.mina_approval_state, 'approved')
        line.write({'product_uom_qty': 5, 'price_unit': 90000.0})
        self.assertEqual(order.mina_approval_state, 'to_request')
        line.write({'product_uom_qty': 2, 'price_unit': 90000.0})
        self.assertEqual(order.mina_approval_state, 'approved')
        order.action_confirm()
        self.assertEqual(order.state, 'sale')

    def test_manual_discount_needs_approval_and_reject(self):
        order = self._order(discount=5.0)
        self.assertEqual(order.mina_approval_state, 'to_request')
        order.action_mina_request_approval()
        order.with_user(self.manager).action_mina_reject()
        self.assertEqual(order.mina_approval_state, 'rejected')
        self.assertIn("Harga ditolak", order.message_ids[0].body)
        self.assertIn(self.salesman.partner_id, order.message_ids[0].partner_ids)
        with self.assertRaisesRegex(UserError, "perlu approval harga"):
            order.action_confirm()
        # The discount is removed: no approval needed anymore.
        order.order_line.discount = 0.0
        self.assertEqual(order.mina_approval_state, 'not_required')
        order.action_confirm()

    def test_manager_can_approve_without_request(self):
        order = self._order(price_unit=80000.0)
        order.with_user(self.manager).action_mina_approve()
        self.assertEqual(order.mina_approval_state, 'approved')
        order.action_confirm()

    def test_confirmed_order_price_change(self):
        order = self._order()
        order.action_confirm()
        line = order.order_line
        with self.assertRaisesRegex(UserError, "hanya bisa diubah oleh"):
            line.with_user(self.salesman).price_unit = 70000.0
        line.with_user(self.manager).price_unit = 70000.0
        self.assertEqual(order.mina_approval_state, 'approved')
        self.assertEqual(order.mina_approval_user_id, self.manager)
        # Other changes on confirmed orders are not affected.
        line.with_user(self.salesman).name = "Filter Oli (urgent)"

    def test_post_init_hook_accepts_confirmed_orders(self):
        order = self._order(price_unit=80000.0)
        order.with_user(self.manager).action_mina_approve()
        order.action_confirm()
        order.mina_approval_decision = False
        self.assertEqual(order.mina_approval_state, 'to_request')
        post_init_hook(self.env)
        self.assertEqual(order.mina_approval_state, 'approved')

    def test_views(self):
        for model in ('sale.order', 'purchase.order', 'account.move', 'res.config.settings'):
            result = self.env[model].get_views([(False, 'form')])
            if model != 'res.config.settings':
                # The web client applies the field domain sent with the view.
                domain = result['models'][model]['fields']['partner_id']['domain']
                self.assertIn("('category_id', 'child_of', mina_partner_category_id)", domain)
                self.assertIn('mina_partner_category_id', result['models'][model]['fields'])
        self.env['sale.order'].get_views([(False, 'list'), (False, 'search')])
