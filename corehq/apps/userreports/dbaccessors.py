from corehq.apps.domain.dbaccessors import get_docs_in_domain_by_class
from corehq.apps.domain.models import Domain
from corehq.apps.userreports.const import UCR_ES_BACKEND, UCR_LABORATORY_BACKEND
from corehq.dbaccessors.couchapps.all_docs import delete_all_docs_by_doc_type
from corehq.util.test_utils import unit_testing_only


def get_number_of_report_configs_by_data_source(domain, data_source_id):
    """
    Return the number of report configurations that use the given data source.
    """
    from corehq.apps.userreports.models import ReportConfiguration
    return ReportConfiguration.view(
        'userreports/report_configs_by_data_source',
        reduce=True,
        key=[domain, data_source_id]
    ).one()['value']


def get_report_configs_for_domain(domain):
    from corehq.apps.userreports.models import ReportConfiguration
    return sorted(
        get_docs_in_domain_by_class(domain, ReportConfiguration),
        key=lambda report: report.title,
    )


def get_datasources_for_domain(domain):
    from corehq.apps.userreports.models import DataSourceConfiguration
    return sorted(
        DataSourceConfiguration.view(
            'userreports/data_sources_by_build_info',
            start_key=[domain],
            end_key=[domain, {}],
            reduce=False,
            include_docs=True
        ),
        key=lambda config: config.display_name
    )


@unit_testing_only
def get_all_report_configs():
    all_domains = Domain.get_all()
    for domain in all_domains:
        for report_config in get_report_configs_for_domain(domain.name):
            yield report_config


@unit_testing_only
def delete_all_report_configs():
    from corehq.apps.userreports.models import ReportConfiguration
    delete_all_docs_by_doc_type(ReportConfiguration.get_db(), ('ReportConfiguration',))


def _get_all_data_sources():
    from corehq.apps.userreports.models import DataSourceConfiguration
    return DataSourceConfiguration.view(
        'userreports/data_sources_by_build_info',
        reduce=False,
        include_docs=True
    )


def get_all_es_data_sources():
    return [s for s in _get_all_data_sources() if s.backend_id in [UCR_ES_BACKEND, UCR_LABORATORY_BACKEND]]
