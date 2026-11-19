from odoo import api, fields, models

PART_TYPES = [
    ('engine', "Engine"),
    ('airend', "Airend (Compressor)"),
]


class ProductCategory(models.Model):
    _inherit = 'product.category'

    part_type = fields.Selection(
        PART_TYPES,
        string="Jenis Part",
        compute='_compute_part_type', store=True, readonly=False, recursive=True,
        help="Kategori dengan jenis part dipakai sebagai tujuan import data part. "
             "Sub-kategori otomatis mengikuti jenis part kategori induknya.",
    )

    @api.depends('parent_id.part_type')
    def _compute_part_type(self):
        for categ in self:
            categ.part_type = categ.parent_id.part_type or categ.part_type

    @api.model_create_multi
    def create(self, vals_list):
        categories = super().create(vals_list)
        for categ, vals in zip(categories, vals_list):
            if categ.part_type and categ.parent_id:
                categ._mina_copy_costing_from_parent([
                    fname for fname in ('property_cost_method', 'property_valuation') if fname not in vals
                ])
        return categories

    def _mina_copy_costing_from_parent(self, fnames):
        """Costing settings are per category and per company: a new spare part sub-category
        (e.g. created by an import) follows its parent instead of the company default."""
        self.ensure_one()
        for company in self.env['res.company'].sudo().search([]):
            categ = self.sudo().with_company(company)
            parent = categ.parent_id
            vals = {fname: parent[fname] for fname in fnames if parent[fname] and parent[fname] != categ[fname]}
            if vals:
                categ.write(vals)
