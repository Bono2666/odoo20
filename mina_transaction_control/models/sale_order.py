from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount

APPROVER_GROUP = 'sales_team.group_sale_manager'
APPROVAL_ACTIVITY_SUMMARY = "Approval harga Sales Order"
EDITABLE_STATES = ('draft', 'sent')


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    mina_approval_required = fields.Boolean(
        string="Perlu Approval Harga", compute='_compute_mina_approval_required', store=True,
    )
    mina_approval_state = fields.Selection(
        [
            ('not_required', "Tidak Perlu"),
            ('to_request', "Perlu Approval"),
            ('pending', "Menunggu Approval"),
            ('approved', "Disetujui"),
            ('rejected', "Ditolak"),
        ],
        string="Approval Harga", compute='_compute_mina_approval_state', store=True, index=True, tracking=True,
        help="Order dengan harga jual di bawah harga pricelist (termasuk karena diskon manual) harus disetujui "
             "Sales Administrator sebelum bisa dikonfirmasi. Perubahan harga, diskon, qty, atau produk pada baris "
             "tersebut setelah disetujui memerlukan approval ulang.",
    )
    mina_approval_request_signature = fields.Char(copy=False, readonly=True)
    mina_approval_decision = fields.Selection(
        [('pending', "Menunggu"), ('approved', "Disetujui"), ('rejected', "Ditolak")], copy=False, readonly=True,
    )
    mina_approval_requested_by_id = fields.Many2one('res.users', string="Diminta Oleh", copy=False, readonly=True)
    mina_approval_user_id = fields.Many2one('res.users', string="Diputuskan Oleh", copy=False, readonly=True)
    mina_approval_date = fields.Datetime(string="Tanggal Keputusan", copy=False, readonly=True)

    @api.depends('order_line.mina_needs_approval')
    def _compute_mina_approval_required(self):
        for order in self:
            order.mina_approval_required = any(order.order_line.mapped('mina_needs_approval'))

    @api.depends(
        'mina_approval_required', 'order_line.mina_needs_approval', 'order_line.product_id',
        'order_line.product_uom_qty', 'order_line.price_unit', 'order_line.discount',
        'mina_approval_request_signature', 'mina_approval_decision',
    )
    def _compute_mina_approval_state(self):
        for order in self:
            if not order.mina_approval_required:
                order.mina_approval_state = 'not_required'
            elif order.mina_approval_decision and order.mina_approval_request_signature == order._mina_approval_signature():
                order.mina_approval_state = order.mina_approval_decision
            else:
                order.mina_approval_state = 'to_request'

    def _mina_approval_signature(self):
        """What is approved: the lines below the pricelist price, with their product, quantity, price and discount."""
        self.ensure_one()
        return ';'.join(
            f"{line._origin.id or line.id}:{line.product_id.id}:{line.product_uom_qty:.6f}:{line.price_unit:.6f}:"
            f"{line.discount:.6f}"
            for line in self.order_line.filtered('mina_needs_approval').sorted(lambda l: l._origin.id or 0)
        )

    def _mina_approval_lines_text(self):
        self.ensure_one()
        lines = []
        for line in self.order_line.filtered('mina_needs_approval'):
            net_price = line.price_unit * (1.0 - (line.discount or 0.0) / 100.0)
            lines.append(self.env._(
                "%(product)s (qty %(qty)s): harga %(price)s%(discount)s = %(net)s, harga pricelist %(reference)s",
                product=line.product_id.display_name,
                qty=line.product_uom_qty,
                price=format_amount(self.env, line.price_unit, line.currency_id),
                discount=self.env._(", diskon %s%%", line.discount) if line.discount else "",
                net=format_amount(self.env, net_price, line.currency_id),
                reference=format_amount(self.env, line.mina_reference_price, line.currency_id),
            ))
        return lines

    def _mina_approvers(self):
        self.ensure_one()
        return self.env.ref(APPROVER_GROUP).user_ids.filtered(
            lambda user: user.active and not user.share and self.company_id in user.company_ids,
        )

    def _mina_is_approver(self):
        return self.env.user.has_group(APPROVER_GROUP)

    def _mina_close_approval_activities(self, feedback):
        activities = self.sudo().activity_ids.filtered(lambda a: a.summary == APPROVAL_ACTIVITY_SUMMARY)
        if activities:
            activities.action_feedback(feedback=feedback)

    def _mina_post_lines(self, message):
        self.ensure_one()
        items = Markup('').join(Markup('<li>%s</li>') % line for line in self._mina_approval_lines_text())
        self.message_post(body=Markup('%s<ul>%s</ul>') % (message, items))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_mina_request_approval(self):
        for order in self:
            if order.state not in EDITABLE_STATES:
                raise UserError(self.env._("Approval harga hanya bisa diminta untuk quotation."))
            if order.mina_approval_state not in ('to_request', 'rejected'):
                raise UserError(self.env._("Sales Order %s tidak memerlukan permintaan approval.", order.name))
            approvers = order._mina_approvers()
            if not approvers:
                raise UserError(self.env._("Belum ada user Sales Administrator yang bisa menyetujui harga."))
            order.write({
                'mina_approval_request_signature': order._mina_approval_signature(),
                'mina_approval_decision': 'pending',
                'mina_approval_requested_by_id': self.env.uid,
                'mina_approval_user_id': False,
                'mina_approval_date': False,
            })
            order._mina_close_approval_activities(self.env._("Permintaan approval baru"))
            note = Markup('<br/>').join(order._mina_approval_lines_text())
            for approver in approvers:
                order.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=approver.id,
                    summary=APPROVAL_ACTIVITY_SUMMARY, note=note,
                )
            order._mina_post_lines(self.env._("Approval harga diminta untuk baris berikut:"))
        return True

    def action_mina_approve(self):
        if not self._mina_is_approver():
            raise UserError(self.env._("Hanya Sales Administrator yang bisa menyetujui harga."))
        for order in self:
            if order.mina_approval_state not in ('to_request', 'pending', 'rejected'):
                raise UserError(self.env._("Sales Order %s tidak menunggu approval harga.", order.name))
            order._mina_set_decision('approved')
            order._mina_post_lines(self.env._("Harga disetujui:"))
        return True

    def action_mina_reject(self):
        if not self._mina_is_approver():
            raise UserError(self.env._("Hanya Sales Administrator yang bisa menolak harga."))
        for order in self:
            if order.mina_approval_state not in ('to_request', 'pending'):
                raise UserError(self.env._("Sales Order %s tidak menunggu approval harga.", order.name))
            order._mina_set_decision('rejected')
            partner_ids = order.mina_approval_requested_by_id.partner_id.ids
            items = Markup('').join(Markup('<li>%s</li>') % line for line in order._mina_approval_lines_text())
            order.message_post(
                body=Markup('%s<ul>%s</ul>') % (
                    self.env._("Harga ditolak. Ubah harga/diskon baris berikut atau minta approval lagi:"), items,
                ),
                partner_ids=partner_ids,
            )
        return True

    def _mina_set_decision(self, decision):
        self.ensure_one()
        self.write({
            'mina_approval_request_signature': self._mina_approval_signature(),
            'mina_approval_decision': decision,
            'mina_approval_user_id': self.env.uid,
            'mina_approval_date': fields.Datetime.now(),
        })
        self._mina_close_approval_activities(
            self.env._("Disetujui") if decision == 'approved' else self.env._("Ditolak"),
        )

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------

    def _confirmation_error_message(self):
        error = super()._confirmation_error_message()
        if error or self.mina_approval_state in ('not_required', 'approved'):
            return error
        state = dict(self._fields['mina_approval_state']._description_selection(self.env))[self.mina_approval_state]
        return self.env._(
            "Sales Order %(order)s perlu approval harga dari Sales Administrator sebelum dikonfirmasi "
            "(status: %(state)s). Harga di bawah pricelist:\n%(lines)s",
            order=self.name, state=state, lines='\n'.join(f"- {line}" for line in self._mina_approval_lines_text()),
        )

    def _mina_check_confirmed_approval(self):
        """A price below the pricelist on a confirmed order: approved right away when changed by a Sales
        Administrator, refused otherwise."""
        orders = self.filtered(
            lambda o: o.state not in EDITABLE_STATES and o.mina_approval_state not in ('not_required', 'approved'),
        )
        if not orders:
            return
        if not self._mina_is_approver():
            order = orders[0]
            raise UserError(self.env._(
                "Harga di bawah pricelist pada Sales Order %(order)s yang sudah dikonfirmasi hanya bisa diubah oleh "
                "Sales Administrator:\n%(lines)s",
                order=order.name, lines='\n'.join(f"- {line}" for line in order._mina_approval_lines_text()),
            ))
        for order in orders:
            order._mina_set_decision('approved')
            order._mina_post_lines(self.env._("Harga pada order terkonfirmasi diubah dan disetujui oleh Sales Administrator:"))
