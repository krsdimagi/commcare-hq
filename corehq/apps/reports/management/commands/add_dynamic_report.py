from django.core.management import BaseCommand
from optparse import make_option
import json
import sys
from corehq.apps.domain.models import Domain, DynamicReportSet, DynamicReportConfig
from dimagi.utils.modules import to_function


class Command(BaseCommand):
    help = "Set up a dynamic report for a domain"
    args = "<domain> <subsection> <reportname> <reportclass> <reportproperties (json) (if '-' read from stdin)>"
    option_list = (
        make_option('-u', '--update', action='store_true', help='update existing report config if already exists'),
        make_option('-x', '--execute', action='store_true', help='actually make the change; otherwise just a dry run'),
        make_option('-a', '--all', action='store_true', help='make report visible to all (otherwise just previewers'),
    )

    def handle(self, *args, **options):
        domainname, subsection, reportname, reportclass, config_raw = args

        domain = Domain.get_by_name(domainname)
        if not domain:
            self.stderr.write('no domain [%s]\n' % domainname)
            return

        try:
            to_function(reportclass)
        except:
            self.stderr.write('cannot find class [%s]\n' % reportclass)
            return

        try:
            config = json.loads(config_raw) if config_raw != '-' else json.load(sys.stdin)
        except (TypeError, ValueError):
            self.stderr.write('json config not valid\n')
            return

        try:
            section = dict((s.section_title, s) for s in domain.dynamic_reports)[subsection]
        except KeyError:
            section = DynamicReportSet(section_title=subsection)
            domain.dynamic_reports.append(section)
            self.stdout.write('creating subsection [%s]\n' % subsection)

        try:
            report = dict((r.name, r) for r in section.reports)[reportname]
            if not options['update']:
                self.stderr.write('report [%s] exists! use -u to update.\n' % reportname)
                return
        except KeyError:
            report = DynamicReportConfig(name=reportname)
            section.reports.append(report)
            self.stdout.write('creating report [%s]\n' % reportname)

        report.report = reportclass
        report.kwargs = config
        report.previewers_only = not bool(options['all'])

        if options['execute']:
            domain.save()
            self.stdout.write('done; report visible to %s\n' %
                              ('all' if options['all'] else 'previewers only'))
        else:
            self.stdout.write('dry run only. no changes saves (use -x)\n')

