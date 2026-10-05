{
    'name': 'Bulk Data Import / Export',
    'version': '17.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Fast import and export for any model: chunked background jobs, '
               'bad rows never stop the import, Excel report of failed rows',
    'description': """
Bulk Data Import / Export
=========================
Import and export data for any Odoo model, built for large files:

* Excel (.xlsx, .xls) and CSV files, columns matched to fields automatically
* Imports run in chunks; one bad row never stops the others
* Failed rows come back as an Excel file with the reason, ready to fix and re-import
* Large files run in the background, can be cancelled and resumed
* Create, update or both, matching records by any field or External ID
* Test run checks a whole file without saving anything
* Exports stream to Excel or CSV in chunks, with External IDs for round trips
""",
    'author': 'ABBAS',
    'maintainer': 'ABBAS',
    'support': 'abbasodolution@gmail.com',
    'license': 'LGPL-3',
    'depends': ['base', 'web', 'bus'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/bulk_import_job_views.xml',
        'views/bulk_export_job_views.xml',
        'views/menus.xml',
    ],
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
}
