import datetime
import os
import re
from re import Pattern
from typing import TYPE_CHECKING, Any, Optional, Union, overload

import requests
import urllib3

import tmt.hardware
import tmt.log
import tmt.steps.report
import tmt.utils
import tmt.utils.templates
from tmt._compat.pathlib import Path
from tmt.container import container, field
from tmt.result import ResultOutcome
from tmt.utils import (
    ActionType,
    catch_warnings_safe,
    format_timestamp,
    sanitize_string,
    yaml_to_dict,
)

if TYPE_CHECKING:
    from tmt._compat.typing import TypeAlias
    from tmt.hardware import Size

JSON: 'TypeAlias' = Any
DEFAULT_LOG_SIZE_LIMIT: 'Size' = tmt.hardware.UNITS('1 MB')
DEFAULT_TRACEBACK_SIZE_LIMIT: 'Size' = tmt.hardware.UNITS('50 kB')

DEFAULT_LOG_PATTERNS: list[Pattern[str]] = [
    re.compile(pattern)
    for pattern in [
        r'avc\.txt',
        r'dmesg-.*\.txt',
        r'output\.txt',
        r'tmt-watchdog\.txt',
    ]
]


def _flag_env_to_default(option: str, default: bool) -> bool:
    env_var = 'TMT_PLUGIN_REPORT_REPORTPORTAL_' + option.upper()
    if env_var not in os.environ:
        return default
    return bool(os.getenv(env_var) == '1')


@overload
def _str_env_to_default(option: str, default: None) -> Optional[str]:
    pass


@overload
def _str_env_to_default(option: str, default: str) -> str:
    pass


def _str_env_to_default(option: str, default: Optional[str]) -> Optional[str]:
    env_var = 'TMT_PLUGIN_REPORT_REPORTPORTAL_' + option.upper()
    if env_var not in os.environ or os.getenv(env_var) is None:
        return default
    return str(os.getenv(env_var))


def _pattern_list_env_to_default(option: str, default: list[Pattern[str]]) -> list[Pattern[str]]:
    env_var = 'TMT_PLUGIN_REPORT_REPORTPORTAL_' + option.upper()
    if env_var not in os.environ or os.getenv(env_var) is None:
        return default
    return tmt.utils.normalize_pattern_list(
        option,
        [item.strip() for item in str(os.getenv(env_var)).split(',') if item.strip()],
        tmt.log.Logger.get_bootstrap_logger(),
    )


def _size_env_to_default(option: str, default: 'Size') -> 'Size':
    return tmt.hardware.UNITS(_str_env_to_default(option, str(default)))


@container
class LogFilterSettings:
    size: 'Size' = DEFAULT_LOG_SIZE_LIMIT
    is_traceback: bool = False


def _filter_invalid_chars(data: str, settings: LogFilterSettings) -> str:
    """Filter out invalid unicode characters"""
    return sanitize_string(data)


def _filter_log_per_size(data: str, settings: LogFilterSettings) -> str:
    size = tmt.hardware.UNITS(f'{len(data)} bytes')
    if size > settings.size:
        if settings.is_traceback:
            variable = "TMT_PLUGIN_REPORT_REPORTPORTAL_TRACEBACK_SIZE_LIMIT"
            option = "--traceback-size-limit"
        else:
            variable = "TMT_PLUGIN_REPORT_REPORTPORTAL_LOG_SIZE_LIMIT"
            option = "--log-size-limit"
        header = (
            f"WARNING: Uploaded log has been truncated because its size {size} "
            f"exceeds tmt reportportal plugin limit of {settings.size}. "
            f"The limit is controlled with {option} plugin option or "
            f"{variable} environment variable.\n\n"
        )
        return f"{header}{data[: int(settings.size.to('bytes').magnitude)]}"
    return data


_LOG_FILTERS = [
    _filter_log_per_size,
    _filter_invalid_chars,
]


def _filter_log(log: str, settings: Optional[LogFilterSettings] = None) -> str:
    settings = settings or LogFilterSettings()
    for log_filter in _LOG_FILTERS:
        log = log_filter(log, settings=settings)
    return log


@container
class ReportReportPortalData(tmt.steps.report.ReportStepData):
    url: Optional[str] = field(
        option="--url",
        metavar="URL",
        default=_str_env_to_default('url', None),
        help="The URL of the ReportPortal instance where the data should be sent to.",
    )

    token: Optional[str] = field(
        option="--token",
        metavar="TOKEN",
        default=_str_env_to_default('token', None),
        help="The token to use for upload to the ReportPortal instance (from the user profile).",
    )

    project: Optional[str] = field(
        option="--project",
        metavar="PROJECT_NAME",
        default=_str_env_to_default('project', None),
        help="Name of the project into which the results should be uploaded.",
    )

    launch: Optional[str] = field(
        option="--launch",
        metavar="LAUNCH_NAME",
        default=_str_env_to_default('launch', None),
        help="""
           Set the launch name, otherwise name of the plan is used by default.
           Should be defined with 'suite-per-plan' option or it will be named after the first plan.
           """,
    )

    launch_description: Optional[str] = field(
        option="--launch-description",
        metavar="DESCRIPTION",
        default=_str_env_to_default('launch_description', None),
        help="""
             Pass the description for ReportPortal launch with 'suite-per-plan' option
             or append the original (plan summary) with additional info.
             Appends test description with 'upload-to-launch/suite' options.
             """,
    )

    launch_per_plan: bool = field(
        option="--launch-per-plan",
        default=_flag_env_to_default('launch_per_plan', False),
        is_flag=True,
        help="Mapping launch per plan, creating one or more launches with no suite structure.",
    )

    suite_per_plan: bool = field(
        option="--suite-per-plan",
        default=_flag_env_to_default('suite_per_plan', False),
        is_flag=True,
        help="""
             Mapping suite per plan, creating one launch and continuous uploading suites into it.
             Recommended to use with 'launch' and 'launch-description' options.
             Can be used with 'upload-to-launch' option for an additional upload of new suites.
             """,
    )

    upload_to_launch: Optional[str] = field(
        option="--upload-to-launch",
        metavar="LAUNCH_ID",
        default=_str_env_to_default('upload_to_launch', None),
        help="""
           Pass the launch ID for an additional test/suite upload to an existing launch. ID can be
           found in the launch URL. Keep the launch structure with options 'launch/suite-per-plan'.
           To upload specific info into description see also 'launch-description'.
           """,
    )

    upload_to_suite: Optional[str] = field(
        option="--upload-to-suite",
        metavar="SUITE_ID",
        default=_str_env_to_default('upload_to_suite', None),
        help="""
             Pass the suite ID for an additional test upload to a suite
             within an existing launch. ID can be found in the suite URL.
             To upload specific info into description see also 'launch-description'.
             """,
    )

    launch_rerun: bool = field(
        option="--launch-rerun",
        default=_flag_env_to_default('launch_rerun', False),
        is_flag=True,
        help="""
             Rerun the last launch based on its name and unique test paths to create Retry item
             with a new version per each test. Supported in 'suite-per-plan' structure only.
             """,
    )

    defect_type: Optional[str] = field(
        option="--defect-type",
        metavar="DEFECT_NAME",
        default=_str_env_to_default('defect_type', None),
        help="""
             Pass the defect type to be used for failed test, which is defined in the project
             (e.g. 'Idle'). 'To Investigate' is used by default.
             """,
    )

    log_size_limit: 'Size' = field(
        option="--log-size-limit",
        metavar="SIZE",
        default=_size_env_to_default('log_size_limit', DEFAULT_LOG_SIZE_LIMIT),
        help=f"""
              Size limit in bytes for log upload to ReportPortal.
              The default limit is {DEFAULT_LOG_SIZE_LIMIT}.
              """,
        normalize=tmt.utils.normalize_data_amount,
        serialize=lambda limit: str(limit),
        unserialize=lambda serialized: tmt.hardware.UNITS(serialized),
    )

    traceback_size_limit: 'Size' = field(
        option="--traceback-size-limit",
        metavar="SIZE",
        default=_size_env_to_default('traceback_size_limit', DEFAULT_TRACEBACK_SIZE_LIMIT),
        help=f"""
              Size limit in bytes for traceback log upload to ReportPortal.
              The default limit is {DEFAULT_TRACEBACK_SIZE_LIMIT}.
              """,
        help_example_values=[str(DEFAULT_TRACEBACK_SIZE_LIMIT), '1MB'],
        normalize=tmt.utils.normalize_data_amount,
        serialize=lambda limit: str(limit),
        unserialize=lambda serialized: tmt.hardware.UNITS(serialized),
    )

    exclude_variables: str = field(
        option="--exclude-variables",
        metavar="PATTERN",
        default=_str_env_to_default('exclude_variables', "^TMT_.*"),
        help="""
             Regular expression for excluding environment variables
             from reporting to ReportPortal ('^TMT_.*' used by default).
             Parameters in ReportPortal get filtered out by the pattern
             to prevent overloading and to preserve the history aggregation
             for ReportPortal item if tmt id is not provided.
             """,
    )

    api_version: str = field(
        option="--api-version",
        metavar="VERSION",
        default=_str_env_to_default('api_version', "v1"),
        help="Override the default reportportal API version (v1).",
    )

    artifacts_url: Optional[str] = field(
        metavar="ARTIFACTS_URL",
        option="--artifacts-url",
        default=_str_env_to_default('artifacts_url', os.getenv('TMT_REPORT_ARTIFACTS_URL')),
        help="Link to test artifacts provided for report plugins.",
    )

    ssl_verify: bool = field(
        default=True,
        option=('--ssl-verify / --no-ssl-verify'),
        is_flag=True,
        show_default=True,
        help="Enable/disable the SSL verification for communication with ReportPortal.",
    )

    upload_subresults: bool = field(
        default=False,
        option=('--upload-subresults / --no-upload-subresults'),
        is_flag=True,
        show_default=True,
        help="Enable/disable uploading of tmt subresults into the ReportPortal.",
    )

    link_template: Optional[str] = field(
        metavar="TEMPLATE",
        option="--link-template",
        default=_str_env_to_default('link_template', None),
        help="""
             Jinja template that will be rendered for each test result and appended to the end
             of its description. The following variables are passed to the template:
             ``PLAN_NAME``, ``RESULT``.
             """,
    )

    upload_log_pattern: list[Pattern[str]] = field(
        metavar="PATTERN",
        option="--upload-log-pattern",
        multiple=True,
        default_factory=lambda: _pattern_list_env_to_default(
            'upload_log_pattern', DEFAULT_LOG_PATTERNS[:]
        ),
        normalize=tmt.utils.normalize_pattern_list,
        serialize=lambda patterns: [pattern.pattern for pattern in patterns],
        unserialize=lambda serialized: [re.compile(pattern) for pattern in serialized],
        help="""
             List of regular expressions to look for in result log names. If any of the
             patterns is found in a log file name, the log will be uploaded to ReportPortal.
             Check result logs will be uploaded only if the check failed or if an error
             occurred during the execution. The search mode is used for pattern matching.
             See the :ref:`regular-expressions` section for details.
             """,
    )

    launch_url: Optional[str] = None
    launch_uuid: Optional[str] = None
    suite_uuid: Optional[str] = None
    test_uuids: dict[int, str] = field(default_factory=dict)


@tmt.steps.provides_method("reportportal")
class ReportReportPortal(tmt.steps.report.ReportPlugin[ReportReportPortalData]):
    """
    Report test results and their subresults to a ReportPortal instance via API.

    For communication with Report Portal API is necessary to provide
    following options:

    * token for authentication
    * url of the ReportPortal instance
    * project name

    In addition to command line options it's possible to use environment
    variables:

    .. code-block:: bash

        export TMT_PLUGIN_REPORT_REPORTPORTAL_${MY_OPTION}=${MY_VALUE}

        # Boolean options are activated with value of 1:
        TMT_PLUGIN_REPORT_REPORTPORTAL_SUITE_PER_PLAN=1

    Assuming the URL and token are provided by the environment variables,
    the plan config can look like this:

    .. code-block:: yaml

        # Use ReportPortal as the default report for given plan
        report:
            how: reportportal
            project: baseosqe

        # Report context attributes for given plan
        context:
            ...

        environment:
            ...

    .. code-block:: yaml

        # Report description, contact, id and environment variables for given test
        summary: ...
        contact: ...
        id: ...
        environment:
            ...

    Where the context and environment sections must be filled with
    corresponding data in order to report context as attributes
    (arch, component, distro, trigger, compose, etc.) and
    environment variables as parameters in the Item Details.

    Other reported fmf data are summary, id, web link and contact per
    test.

    Two types of data structures are supported for reporting to ReportPortal:

    * ``launch-per-plan`` mapping (default) that results in launch-test structure.
    * ``suite-per-plan`` mapping that results in launch-suite-test structure.

    Supported report use cases:

    * Report a new run in launch-suite-test or launch-test structure
    * Report an additional rerun with ``launch-rerun`` option and same launch name (-> Retry items)
      or by reusing the run and reporting with ``again`` option (-> append logs)
    * To see plan progress, discover and report an empty (IDLE) run
      and reuse the run for execution and updating the report with ``again`` option
    * Report contents of a new run to an existing launch via the URL ID in three ways:
      tests to launch, suites to launch and tests to suite.

    Example:

    .. code-block:: shell

        # Enable ReportPortal report from the command line depending on the use case:

        ## Simple upload with all project, url endpoint and user token passed in command line
        tmt run --all report --how reportportal --project=baseosqe --url="https://reportportal.xxx.com" --token="abc...789"

        ## Simple upload with url and token exported in environment variable
        tmt run --all report --how reportportal --project=baseosqe

        ## Upload with project name in fmf data, filtering out parameters (environment variables)
        ## that tend to be unique and break the history aggregation
        tmt run --all report --how reportportal --exclude-variables="^(TMT|PACKIT|TESTING_FARM).*"

        ## Upload all plans as suites into one ReportPortal launch
        tmt run --all report --how reportportal --suite-per-plan --launch=Errata --launch-description="..."

        ## Rerun the launch with suite structure for the test results to be uploaded
        ## into the latest launch with the same name as a new 'Retry' tab
        ## (mapping based on unique paths)
        tmt run --all report --how reportportal --suite-per-plan --launch=Errata --launch-rerun

        ## Rerun the tmt run and append the new result logs under the previous one
        ## uploaded in ReportPortal (precise mapping)
        tmt run --id run-012 --all report --how reportportal --again

        ## Additional upload of new suites into given launch with suite structure
        tmt run --all report --how reportportal --suite-per-plan --upload-to-launch=4321

        ## Additional upload of new tests into given launch with non-suite structure
        tmt run --all report --how reportportal --launch-per-plan --upload-to-launch=1234

        ## Additional upload of new tests into given suite
        tmt run --all report --how reportportal --upload-to-suite=123456

        ## Upload Idle tests, then execute it and add result logs into prepared empty tests
        tmt run discover report --how reportportal --defect-type=Idle
        tmt run --last --all report --how reportportal --again
    """  # noqa: E501

    _data_class = ReportReportPortalData

    TMT_TO_RP_RESULT_STATUS = {
        ResultOutcome.PASS: "PASSED",
        ResultOutcome.FAIL: "FAILED",
        ResultOutcome.INFO: "SKIPPED",
        ResultOutcome.WARN: "FAILED",
        ResultOutcome.ERROR: "FAILED",
        ResultOutcome.SKIP: "SKIPPED",
        ResultOutcome.PENDING: "SKIPPED",
    }

    def handle_response(self, response: requests.Response) -> None:
        """
        Check the endpoint response and raise an exception if needed
        """

        self.debug("Response code from the endpoint", response.status_code)
        self.debug("Message from the endpoint", response.text)

        if not response.ok:
            raise tmt.utils.ReportError(
                f"Received non-ok status code {response.status_code} "
                f"from ReportPortal: {response.text}"
            )

    def check_options(self) -> None:
        """
        Check options for known troublesome combinations
        """

        if self.data.launch_per_plan and self.data.suite_per_plan:
            self.warn(
                "The options '--launch-per-plan' and '--suite-per-plan' are mutually exclusive. "
                "Default option '--launch-per-plan' is used."
            )
            self.data.suite_per_plan = False

        if self.data.launch_rerun and (self.data.upload_to_launch or self.data.upload_to_suite):
            self.warn(
                "Unexpected option combination: "
                "'--launch-rerun' is ignored when uploading additional tests."
            )

        if not self.data.suite_per_plan and self.data.launch_rerun:
            self.warn(
                "Unexpected option combination: '--launch-rerun' "
                "may cause an unexpected behaviour with launch-per-plan structure"
            )

    @property
    def datetime(self) -> str:
        # Use the same format of timestramp as tmt does
        return format_timestamp(datetime.datetime.now(datetime.timezone.utc))

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.data.token}",
            "Accept": "*/*",
            "Content-Type": "application/json",
        }

    @property
    def url(self) -> str:
        return f"{self.data.url}/api/{self.data.api_version}/{self.data.project}"

    def construct_launch_attributes(
        self, suite_per_plan: bool, attributes: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        if not suite_per_plan or not self.step.plan.my_run:
            return attributes.copy()

        # Get common attributes across the plans
        merged_plans = [
            {key: value[0] for key, value in plan._fmf_context.items()}
            for plan in self.step.plan.my_run.plans
        ]
        result_dict = merged_plans[0]
        for current_plan in merged_plans[1:]:
            tmp_dict = {
                key: value
                for key, value in current_plan.items()
                if key in result_dict and result_dict[key] == value
            }
            result_dict = tmp_dict
        return [{'key': key, 'value': value} for key, value in result_dict.items()]

    def get_defect_type_locator(
        self, session: requests.Session, defect_type: Optional[str]
    ) -> str:
        if not defect_type:
            return "ti001"

        response = self.rp_api_get(session, "settings")
        defect_types = yaml_to_dict(response.text).get("subTypes")
        if not defect_types:
            return "ti001"

        groups_to_search = [
            'TO_INVESTIGATE',
            'NO_DEFECT',
            'SYSTEM_ISSUE',
            'AUTOMATION_BUG',
            'PRODUCT_BUG',
        ]
        for group_name in groups_to_search:
            defect_types_list = defect_types[group_name]
            dt_tmp = [
                dt['locator']
                for dt in defect_types_list
                if dt['longName'].lower() == defect_type.lower()
            ]
            dt_locator = dt_tmp[0] if dt_tmp else None
            if dt_locator:
                break
        if not dt_locator:
            raise tmt.utils.ReportError(
                f"Defect type '{defect_type}' "
                "is not be defined in the project {self.data.project}"
            )
        self.verbose("defect_type", defect_type, color="cyan", shift=1)
        return str(dt_locator)

    def rp_api_get(self, session: requests.Session, path: str) -> requests.Response:
        response = session.get(url=f"{self.url}/{path}", headers=self.headers)
        self.handle_response(response)
        return response

    def rp_api_post(self, session: requests.Session, path: str, json: JSON) -> requests.Response:
        response = session.post(url=f"{self.url}/{path}", headers=self.headers, json=json)
        self.handle_response(response)
        return response

    def rp_api_put(self, session: requests.Session, path: str, json: JSON) -> requests.Response:
        response = session.put(url=f"{self.url}/{path}", headers=self.headers, json=json)
        self.handle_response(response)
        return response

    def append_description(self, curr_description: str) -> str:
        """
        Extend text with the launch description (if provided)
        """

        if self.data.launch_description:
            if curr_description:
                curr_description += "<br>" + self.data.launch_description
            else:
                curr_description = self.data.launch_description
        return curr_description

    def append_link_template(
        self,
        description: str,
        link_template: str,
        result: tmt.result.Result,
    ) -> str:
        """
        Extend text with rendered link template
        """

        url = tmt.utils.templates.render_template(
            link_template,
            PLAN_NAME=self.step.plan.pathless_safe_name,
            RESULT=result,
        )
        return f'{description}<br>{url}' if description else url

    def upload_result_logs(
        self,
        result: Union[tmt.result.Result, tmt.result.SubResult],
        session: requests.Session,
        item_uuid: str,
        launch_uuid: str,
        timestamp: str,
        write_out_failures: bool = True,
    ) -> None:
        """
        Upload all result log files into the ReportPortal instance
        """

        def upload_log(log_path: Path, is_yaml: bool = False, is_traceback: bool = False) -> None:
            try:
                if is_yaml:
                    logs = tmt.utils.yaml_to_list(self.step.plan.execute.read(log_path))
                else:
                    logs = [self.step.plan.execute.read(log_path)]
            except (tmt.utils.FileError, tmt.utils.GeneralError) as error:
                tmt.utils.show_exception_as_warning(
                    exception=error,
                    message=f"Failed to read log file '{log_path}' for ReportPortal upload.",
                    logger=self._logger,
                )
                return

            filter_settings = LogFilterSettings(
                size=self.data.traceback_size_limit if is_traceback else self.data.log_size_limit,
                is_traceback=is_traceback,
            )

            for log in logs:
                # Add file name to the log if it is not a traceback
                if not is_traceback:
                    log = f'### `{log_path.name}`\n{log}'
                # Upload log
                self.rp_api_post(
                    session=session,
                    path="log/entry",
                    json={
                        "message": _filter_log(log, filter_settings),
                        "itemUuid": item_uuid,
                        "launchUuid": launch_uuid,
                        "level": "ERROR" if is_traceback else "INFO",
                        "time": timestamp,
                    },
                )

        # Upload result logs
        for log_path in result.log:
            if any(pattern.search(log_path.name) for pattern in self.data.upload_log_pattern):
                upload_log(log_path)

        # Upload check result logs if the check failed
        for check in result.check:
            if check.result not in (ResultOutcome.FAIL, ResultOutcome.ERROR):
                continue
            for log_path in check.log:
                if any(pattern.search(log_path.name) for pattern in self.data.upload_log_pattern):
                    upload_log(log_path, is_traceback=True)

        # Upload failure logs
        if write_out_failures:
            for failure_log in result.failure_logs:
                upload_log(failure_log, is_yaml=True, is_traceback=True)

    def execute_rp_import(self) -> None:
        """
        Execute the import of test, results and subresults into ReportPortal
        """

        assert self.step.plan.my_run is not None
        # Use the current datetime as a default, but this is the worst case scenario
        # and we should use timestamps from results log as much as possible.
        launch_start_time = self.datetime

        # Support for idle tests
        executed = bool(self.step.plan.execute.results())
        if executed:
            # Launch time should be the earliest start time of all plans.
            #
            # The datetime *strings* are in fact sorted here, but finding the minimum will work,
            # because the datetime in ISO format is designed to be lexicographically sortable.
            launch_start_time = min(
                r.start_time or self.datetime for r in self.step.plan.execute.results()
            )

        # Create launch, suites (if "--suite_per_plan") and tests;
        # or report to existing launch/suite if its id is given
        suite_per_plan = self.data.suite_per_plan
        launch_per_plan = self.data.launch_per_plan
        if not launch_per_plan and not suite_per_plan:
            launch_per_plan = True  # by default

        suite_id = self.data.upload_to_suite
        launch_id = self.data.upload_to_launch

        suite_uuid = self.data.suite_uuid
        launch_uuid = self.data.launch_uuid
        additional_upload = suite_id or launch_id or launch_uuid
        is_the_first_plan = self.step.plan == self.step.plan.my_run.plans[0]
        if not launch_uuid and suite_per_plan and not is_the_first_plan:
            rp_phases = list(self.step.plan.my_run.plans[0].report.phases(ReportReportPortal))
            if rp_phases:
                launch_uuid = rp_phases[0].data.launch_uuid

        create_test = not self.data.test_uuids
        create_suite = suite_per_plan and not (suite_uuid or suite_id)
        create_launch = not (launch_uuid or launch_id or suite_uuid or suite_id)

        launch_name = self.data.launch or self.step.plan.name
        suite_name = ""
        launch_url = ""

        launch_rerun = self.data.launch_rerun
        envar_pattern = self.data.exclude_variables or "$^"
        defect_type = self.data.defect_type or ""

        attributes = [
            {'key': key, 'value': value[0]} for key, value in self.step.plan._fmf_context.items()
        ]
        launch_attributes = self.construct_launch_attributes(suite_per_plan, attributes)

        if suite_per_plan:
            launch_description = self.data.launch_description or ""
            suite_description = self.append_description(self.step.plan.summary or "")
        else:
            launch_description = self.step.plan.summary or ""
            launch_description = self.append_description(launch_description)
            suite_description = ""

        # Check whether artifacts URL has been provided
        if self.data.artifacts_url:
            launch_description += f"<br>{self.data.artifacts_url}"
            suite_description += f"<br>{self.data.artifacts_url}"

        # Communication with RP instance
        with tmt.utils.retry_session(
            status_forcelist=(
                429,  # Too Many Requests
                500,  # Internal Server Error
                502,  # Bad Gateway
                503,  # Service Unavailable
                504,  # Gateway Timeout
            )
        ) as session:
            session.verify = self.data.ssl_verify

            if create_launch:
                # Create a launch
                self.info("launch", launch_name, color="cyan")
                response = self.rp_api_post(
                    session=session,
                    path="launch",
                    json={
                        "name": launch_name,
                        "description": launch_description,
                        "attributes": launch_attributes,
                        "startTime": launch_start_time,
                        "rerun": launch_rerun,
                    },
                )
                launch_uuid = yaml_to_dict(response.text).get("id")

            else:
                # Get the launch_uuid or info to log
                if suite_id:
                    response = self.rp_api_get(session, f"item/{suite_id}")
                    suite_uuid = yaml_to_dict(response.text).get("uuid")
                    suite_name = str(yaml_to_dict(response.text).get("name"))
                    launch_id = yaml_to_dict(response.text).get("launchId")

                if launch_id:
                    response = self.rp_api_get(session, f"launch/{launch_id}")
                    launch_uuid = yaml_to_dict(response.text).get("uuid")

            if launch_uuid and not launch_id:
                response = self.rp_api_get(session, f"launch/uuid/{launch_uuid}")
                launch_id = yaml_to_dict(response.text).get("id")

            # Print the launch info
            if not create_launch:
                launch_name = yaml_to_dict(response.text).get("name") or ""
                self.verbose("launch", launch_name, color="green")
                self.verbose("id", launch_id, "yellow", shift=1)

            assert launch_uuid is not None
            self.verbose("uuid", launch_uuid, "yellow", shift=1)
            self.data.launch_uuid = launch_uuid

            launch_url = f"{self.data.url}/ui/#{self.data.project}/launches/all/{launch_id}"

            if create_suite:
                # Create a suite
                suite_name = self.step.plan.name
                self.info("suite", suite_name, color="cyan")
                response = self.rp_api_post(
                    session=session,
                    path="item",
                    json={
                        "name": suite_name,
                        "description": suite_description,
                        "attributes": attributes,
                        "startTime": launch_start_time,
                        "launchUuid": launch_uuid,
                        "type": "suite",
                    },
                )
                suite_uuid = yaml_to_dict(response.text).get("id")
                assert suite_uuid is not None

            elif suite_name:
                self.info("suite", suite_name, color="green")
                self.verbose("id", suite_id, "yellow", shift=1)

            if suite_uuid:
                self.verbose("uuid", suite_uuid, "yellow", shift=1)
                self.data.suite_uuid = suite_uuid

            # The first test starts with the launch (at the worst case)
            test_start_time = launch_start_time

            for result, test_origin in self.step.plan.execute.results_for_tests(
                self.step.plan.discover.tests()
            ):
                test = test_origin.test if test_origin else None

                test_name = None
                test_description = ''
                test_link = None
                test_id = None
                env_vars = None

                item_attributes = attributes.copy()
                if result:
                    serial_number = result.serial_number
                    test_name = result.name

                    # Use the actual timestamp or reuse the old one if missing
                    test_start_time = result.start_time or test_start_time

                    # for guests, save their primary address
                    if result.guest.primary_address:
                        item_attributes.append(
                            {'key': 'guest_primary_address', 'value': result.guest.primary_address}
                        )
                    # for multi-host tests store also provision name and role
                    if result.guest.name != 'default-0':
                        item_attributes.append({'key': 'guest_name', 'value': result.guest.name})
                    if result.guest.role:
                        item_attributes.append({'key': 'guest_role', 'value': result.guest.role})

                # update RP item with additional attributes if test details are available
                if test:
                    serial_number = test.serial_number
                    if not test_name:
                        test_name = test.name
                    if test.author:
                        item_attributes += [
                            {'key': 'author', 'value': author} for author in test.author
                        ]
                    if test.contact:
                        item_attributes += [
                            {'key': 'contact', 'value': contact} for contact in test.contact
                        ]
                    if test.summary:
                        test_description = test.summary
                    if test.web_link():
                        test_link = test.web_link()
                    if test.id:
                        test_id = test.id

                    env_vars = [
                        {'key': key, 'value': value}
                        for key, value in test.environment.items()
                        if not re.search(envar_pattern, key)
                    ]

                if create_test:
                    if (
                        self.data.upload_to_launch and launch_per_plan
                    ) or self.data.upload_to_suite:
                        test_description = self.append_description(test_description)

                    if result and self.data.link_template:
                        test_description = self.append_link_template(
                            test_description,
                            self.data.link_template,
                            result,
                        )

                    # Create a test item
                    self.info("test", test_name, color="cyan")
                    response = self.rp_api_post(
                        session=session,
                        path=f"item{f'/{suite_uuid}' if suite_uuid else ''}",
                        json={
                            "name": test_name,
                            "description": test_description,
                            "attributes": item_attributes,
                            "parameters": env_vars,
                            "codeRef": test_link,
                            "launchUuid": launch_uuid,
                            "type": "step",
                            "testCaseId": test_id,
                            "startTime": test_start_time,
                        },
                    )

                    item_uuid = yaml_to_dict(response.text).get("id")
                    assert item_uuid is not None
                    self.verbose("uuid", item_uuid, "yellow", shift=1)
                    self.data.test_uuids[serial_number] = item_uuid
                else:
                    item_uuid = self.data.test_uuids[serial_number]

                # If the result end-time is not defined, use the latest result start-time as
                # default.
                test_end_time = test_start_time

                # Support for idle tests
                item_status = "SKIPPED"
                if result:
                    # Shift the timestamp to the end of a test.
                    test_end_time = result.end_time or test_end_time

                    item_status = self.TMT_TO_RP_RESULT_STATUS[result.result]

                    self.upload_result_logs(
                        result=result,
                        session=session,
                        item_uuid=item_uuid,
                        launch_uuid=launch_uuid,
                        timestamp=test_end_time,
                        write_out_failures=bool(item_status == "FAILED"),
                    )

                    # If upload of subresults is required, create (and *finish*) the child test
                    # item for every tmt subresult and map it under the parent test item.
                    if self.data.upload_subresults:
                        for subresult in result.subresult:
                            # Create a child item
                            self.info("sub-test", subresult.name, color="cyan", shift=1)

                            # Use the parent test start-time as a default
                            subresult_start_time = subresult.start_time or test_start_time

                            response = self.rp_api_post(
                                session=session,
                                path=f"item/{item_uuid}",
                                json={
                                    "name": subresult.name,
                                    "launchUuid": launch_uuid,
                                    "type": "step",
                                    "startTime": subresult_start_time,
                                },
                            )

                            child_item_uuid = yaml_to_dict(response.text).get("id")
                            assert child_item_uuid is not None

                            child_item_status = self.TMT_TO_RP_RESULT_STATUS[subresult.result]
                            subtest_end_time = subresult.end_time or test_end_time

                            # Upload the subtest (child) logs
                            self.upload_result_logs(
                                result=subresult,
                                session=session,
                                item_uuid=child_item_uuid,
                                launch_uuid=launch_uuid,
                                timestamp=subtest_end_time,
                                write_out_failures=bool(child_item_status == "FAILED"),
                            )

                            # Finish the child item
                            response = self.rp_api_put(
                                session=session,
                                path=f"item/{child_item_uuid}",
                                json={
                                    "launchUuid": launch_uuid,
                                    "status": child_item_status,
                                    "endTime": subtest_end_time,
                                },
                            )

                            self.verbose("uuid", child_item_uuid, "yellow", shift=2)

                # Finish the parent test item
                response = self.rp_api_put(
                    session=session,
                    path=f"item/{item_uuid}",
                    json={
                        "launchUuid": launch_uuid,
                        "endTime": test_end_time,
                        "status": item_status,
                        "issue": {"issueType": self.get_defect_type_locator(session, defect_type)},
                    },
                )

                # The launch ends with the last test
                launch_end_time = test_end_time

            if create_suite:
                # Finish the test suite
                response = self.rp_api_put(
                    session=session,
                    path=f"item{f'/{suite_uuid}' if suite_uuid else ''}",
                    json={"launchUuid": launch_uuid, "endTime": launch_end_time},
                )

            is_the_last_plan = self.step.plan == self.step.plan.my_run.plans[-1]
            if is_the_last_plan:
                self.data.defect_type = None

            if (
                launch_per_plan or (suite_per_plan and is_the_last_plan)
            ) and not additional_upload:
                # Finish the launch
                response = self.rp_api_put(
                    session=session,
                    path=f"launch/{launch_uuid}/finish",
                    json={"endTime": launch_end_time},
                )
                launch_url = str(yaml_to_dict(response.text).get("link"))

            assert launch_url is not None
            self.info("url", launch_url, "magenta")
            self.data.launch_url = launch_url

    def go(self, *, logger: Optional[tmt.log.Logger] = None) -> None:
        """
        Report test results to the endpoint

        Create a ReportPortal launch and its test items,
        fill it with all parts needed and report the logs.
        """

        super().go(logger=logger)

        if not self.data.url:
            raise tmt.utils.ReportError("No ReportPortal endpoint url provided.")
        self.data.url = self.data.url.rstrip("/")

        if not self.data.project:
            raise tmt.utils.ReportError("No ReportPortal project provided.")

        if not self.data.token:
            raise tmt.utils.ReportError("No ReportPortal token provided.")

        if not self.step.plan.my_run:
            raise tmt.utils.ReportError("No run data available.")

        self.check_options()

        # If SSL verification is disabled, do not print warnings with urllib3
        warning_filter_action: ActionType = 'default'
        if not self.data.ssl_verify:
            warning_filter_action = 'ignore'
            self.warn(
                "SSL verification is disabled for all requests being made to ReportPortal "
                f"instance ({self.data.url})."
            )

        with catch_warnings_safe(
            action=warning_filter_action, category=urllib3.exceptions.InsecureRequestWarning
        ):
            self.execute_rp_import()
