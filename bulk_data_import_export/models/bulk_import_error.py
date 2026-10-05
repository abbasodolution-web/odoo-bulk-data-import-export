from odoo import fields, models


class BulkImportError(models.Model):
    _name = 'bulk.import.error'
    _description = 'Bulk Import Failed Row'
    _order = 'row_number, id'

    job_id = fields.Many2one('bulk.import.job', required=True, ondelete='cascade', index=True)
    row_number = fields.Integer(string='Row', readonly=True)
    message = fields.Text(string='Reason', readonly=True)
    row_values = fields.Text(readonly=True, help="The original cells of the failed rows, as JSON.")
