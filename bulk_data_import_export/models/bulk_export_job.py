import base64
import csv
import logging
import os
import tempfile

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import split_every
from odoo.tools.safe_eval import safe_eval

from ..tools import spreadsheet

_logger = logging.getLogger(__name__)


class BulkExportJob(models.Model):
    _name = 'bulk.export.job'
    _inherit = 'bulk.job.mixin'
    _description = 'Bulk Export Job'
    _order = 'id desc'

    name = fields.Char(required=True, default=lambda self: _('New export'))
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], default='draft', required=True, readonly=True, copy=False)
    model_id = fields.Many2one('ir.model', string='Export from', required=True, ondelete='cascade',
                               domain=[('transient', '=', False)])
    model_name = fields.Char(related='model_id.model')
    domain = fields.Char(string='Records', default='[]', help="Only records matching these conditions are exported.")
    include_archived = fields.Boolean(string='Include archived records')
    field_ids = fields.Many2many(
        'ir.model.fields', string='Fields', domain="[('model_id', '=', model_id), ('ttype', '!=', 'binary')]",
        help="Leave empty to export every stored field.")
    include_external_id = fields.Boolean(
        string='Add External ID column', default=True,
        help="Adds an \"id\" column. Importing the edited file again then updates these same records.")
    relations_as_xmlid = fields.Boolean(
        string='Linked records as External IDs',
        help="Export many2one and many2many values as External IDs (\"partner_id/id\") instead of names. "
             "Best for moving data to another database.")
    use_labels = fields.Boolean(string='Field labels as column titles',
                                help="Use readable titles instead of technical names. The importer accepts both.")
    file_format = fields.Selection([('xlsx', 'Excel (.xlsx)'), ('csv', 'CSV (.csv)')], default='xlsx', required=True)
    chunk_size = fields.Integer(default=2000, required=True)
    record_count = fields.Integer(string='Records to export', compute='_compute_record_count')
    exported_count = fields.Integer(string='Exported', readonly=True, copy=False)
    file = fields.Binary(string='Exported file', attachment=True, readonly=True, copy=False)
    file_name = fields.Char(copy=False)
    message = fields.Text(readonly=True, copy=False)
    user_id = fields.Many2one('res.users', default=lambda self: self.env.user, required=True, readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    date_start = fields.Datetime(readonly=True, copy=False)
    date_end = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        ('chunk_size_positive', 'CHECK(chunk_size > 0)', 'The chunk size must be at least 1.'),
    ]

    @api.constrains('model_id')
    def _check_model_id(self):
        for job in self:
            model = self.env.get(job.model_id.model)
            if model is None or model._abstract or not model._auto:
                raise ValidationError(_("%s has no records of its own and cannot be used here.", job.model_id.name))

    @api.onchange('model_id')
    def _onchange_model_id(self):
        self.field_ids = False
        self.domain = '[]'
        if self.model_id and self.name in (False, _('New export')):
            self.name = self.model_id.name

    @api.depends('model_id', 'domain', 'include_archived')
    def _compute_record_count(self):
        for job in self:
            try:
                job.record_count = job._export_model().search_count(job._get_domain()) if job.model_id else 0
            except Exception:
                job.record_count = 0

    def _get_domain(self):
        try:
            return safe_eval(self.domain or '[]')
        except Exception as e:
            raise UserError(_("The record filter is not valid: %s", e)) from e

    def _export_model(self):
        model = self.env[self.model_name].with_user(self.user_id).with_company(self.company_id)
        return model.with_context(active_test=not self.include_archived, lang=self.user_id.lang)

    def _export_paths(self):
        model = self.env[self.model_name]
        if self.field_ids:
            names = sorted((f.name for f in self.field_ids if f.name in model._fields), key=lambda n: (n != model._rec_name, n))
        else:
            names = [name for name, field in model._fields.items()
                     if field.store and field.type not in ('binary', 'one2many')
                     and name not in models.MAGIC_COLUMNS and name != 'id']
            names.sort(key=lambda n: (n != model._rec_name, n))
        paths = []
        for name in names:
            field = model._fields[name]
            if field.type == 'binary':
                continue
            paths.append(name + '/id' if self.relations_as_xmlid and field.type in ('many2one', 'many2many') else name)
        if self.include_external_id:
            paths.insert(0, 'id')
        return paths

    def _column_titles(self, paths):
        if not self.use_labels:
            return paths
        model = self.env[self.model_name].with_context(lang=self.user_id.lang)
        titles = []
        for path in paths:
            name, _sep, sub = path.partition('/')
            if name == 'id':
                titles.append(_('External ID'))
                continue
            label = model._fields[name]._description_string(model.env)
            titles.append('%s/%s' % (label, _('External ID')) if sub == 'id' else label)
        return titles

    def action_export(self):
        self.ensure_one()
        if self.state in ('queued', 'running'):
            raise UserError(_("This export is already running."))
        self._get_domain()
        self.write({'state': 'queued', 'file': False, 'exported_count': 0, 'message': False,
                    'date_start': fields.Datetime.now(), 'date_end': False})
        inline_limit = int(self.env['ir.config_parameter'].sudo().get_param(
            'bulk_data_import_export.inline_records', 20000))
        if self.record_count <= inline_limit:
            self._process()
            return self.action_download()
        self.env.ref('bulk_data_import_export.ir_cron_bulk_jobs')._trigger()
        return self._notify_action(_("The export runs in the background. You'll be notified when the file is ready."))

    def action_download(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("The file is not ready yet."))
        return self._download('file', 'file_name')

    def action_reset(self):
        self.write({'state': 'draft', 'file': False, 'exported_count': 0, 'message': False,
                    'date_start': False, 'date_end': False})

    def action_refresh(self):
        return True

    def _run_safely(self, deadline=None):
        try:
            self._process()
            self.env.cr.commit()
        except Exception as e:
            _logger.exception("Bulk export %s failed", self.id)
            self.env.cr.rollback()
            self.write({'state': 'failed', 'message': str(e), 'date_end': fields.Datetime.now()})
            self._bus_notify(_("Bulk export"), _("Export \"%s\" failed: %s", self.name, e), 'danger', sticky=True)
            self.env.cr.commit()
        return True

    def _process(self):
        """Write the file in one pass, reading records chunk by chunk.

        Rows go straight to a temporary file and the record cache is emptied
        after each chunk, so memory stays flat whatever the number of records.
        """
        self.ensure_one()
        self.state = 'running'
        model = self._export_model()
        paths = self._export_paths()
        ids = model.search(self._get_domain(), order='id').ids
        fd, path = tempfile.mkstemp(suffix='.' + self.file_format)
        os.close(fd)
        exported = 0
        try:
            if self.file_format == 'xlsx':
                writer = spreadsheet.XlsxWriter(path, self._column_titles(paths), sheet_name='Data')
                write, close = writer.write_row, writer.close
            else:
                handle = open(path, 'w', encoding='utf-8-sig', newline='')
                csv_writer = csv.writer(handle)
                csv_writer.writerow(self._column_titles(paths))
                write = lambda row: csv_writer.writerow([spreadsheet.format_cell(v) for v in row])  # noqa: E731
                close = handle.close
            try:
                for chunk in split_every(self.chunk_size, ids):
                    for row in model.browse(chunk).export_data(paths)['datas']:
                        write(row)
                    exported += len(chunk)
                    model.env.invalidate_all()
            finally:
                close()
            with open(path, 'rb') as handle:
                content = handle.read()
        finally:
            os.unlink(path)
        self.write({
            'state': 'done',
            'exported_count': exported,
            'file': base64.b64encode(content),
            'file_name': '%s.%s' % (self.model_name.replace('.', '_'), self.file_format),
            'date_end': fields.Datetime.now(),
        })
        self._bus_notify(_("Bulk export"), _("Export \"%(name)s\" is ready: %(count)s records.",
                                             name=self.name, count=exported), 'success')
        return True
