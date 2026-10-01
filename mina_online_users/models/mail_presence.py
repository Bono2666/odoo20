from odoo import fields, models


class MailPresence(models.Model):
    _inherit = "mail.presence"

    login = fields.Char(
        related="user_id.login",
        string="Login",
        readonly=True,
    )
