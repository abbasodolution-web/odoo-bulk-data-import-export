from odoo import models


class BulkJobMixin(models.AbstractModel):
    _name = 'bulk.job.mixin'
    _description = 'Bulk Import/Export Job Helpers'

    def _lock(self):
        """Lock the job row so two workers never process the same job."""
        self.env.cr.execute('SELECT id FROM %s WHERE id = %%s FOR UPDATE SKIP LOCKED' % self._table, [self.id])
        return bool(self.env.cr.fetchone())

    def _download(self, field, filename_field, filename=None):
        url = '/web/content/?model=%s&id=%s&field=%s&download=true' % (self._name, self.id, field)
        url += '&filename_field=%s' % filename_field if filename_field else '&filename=%s' % filename
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'self'}

    def _notify_action(self, message, kind='info'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'message': message, 'type': kind, 'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}},
        }

    def _bus_notify(self, title, message, kind, sticky=False):
        self.env['bus.bus']._sendone(self.user_id.partner_id, 'simple_notification', {
            'title': title, 'message': message, 'type': kind, 'sticky': sticky,
        })
