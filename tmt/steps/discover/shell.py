import copy
import shutil
from typing import Any, Optional, TypeVar, cast

import click
import fmf

import tmt
import tmt.base
import tmt.checks
import tmt.log
import tmt.steps
import tmt.steps.discover
import tmt.utils
import tmt.utils.git
from tmt._compat.typing import Self
from tmt.container import SerializableContainer, SpecBasedContainer, container, field
from tmt.steps.prepare.distgit import insert_to_prepare_step
from tmt.utils import (
    Command,
    Environment,
    EnvVarValue,
    Path,
    ShellScript,
)

T = TypeVar('T', bound='TestDescription')


@container
class TestDescription(
    SpecBasedContainer[dict[str, Any], dict[str, Any]],
    tmt.utils.NormalizeKeysMixin,
    SerializableContainer,
):
    """
    Keys necessary to describe a shell-based test.

    Provides basic functionality for transition between "raw" step data representation,
    which consists of keys and values given by fmf tree and CLI options, and this
    container representation for internal use.
    """

    name: str

    # TODO: following keys are copy & pasted from base.Test. It would be much, much better
    # to reuse the definitions from base.Test instead copying them here, but base.Test
    # does not support save/load operations. This is a known issue, introduced by a patch
    # transitioning step data to data classes, it is temporary, and it will be fixed as
    # soon as possible - nobody wants to keep two very same lists of attributes.
    test: ShellScript = field(
        default=ShellScript(''),
        normalize=lambda key_address, raw_value, logger: ShellScript(raw_value),
        serialize=lambda test: str(test),
        unserialize=lambda serialized_test: ShellScript(serialized_test),
    )

    # Core attributes (supported across all levels)
    summary: Optional[str] = None
    description: Optional[str] = None
    enabled: bool = True
    order: int = field(
        default=tmt.steps.PHASE_ORDER_DEFAULT,
        normalize=lambda key_address, raw_value, logger: 50
        if raw_value is None
        else int(raw_value),
    )
    link: Optional[tmt.base.Links] = field(
        default=None,
        normalize=lambda key_address, raw_value, logger: tmt.base.Links(data=raw_value),
        # Using `to_spec()` on purpose: `Links` does not provide serialization
        # methods, because specification of links is already good enough. We
        # can use existing `to_spec()` method, and undo it with a simple
        # `Links(...)` call.
        serialize=lambda link: link.to_spec() if link else None,
        unserialize=lambda serialized_link: tmt.base.Links(data=serialized_link),
    )
    id: Optional[str] = None
    tag: list[str] = field(
        default_factory=list,
        normalize=tmt.utils.normalize_string_list,
    )
    tier: Optional[str] = field(
        default=None,
        normalize=lambda key_address, raw_value, logger: None
        if raw_value is None
        else str(raw_value),
    )
    adjust: Optional[list[tmt.base._RawAdjustRule]] = field(
        default=None,
        normalize=lambda key_address, raw_value, logger: []
        if raw_value is None
        else ([raw_value] if not isinstance(raw_value, list) else raw_value),
    )

    # Basic test information
    author: list[str] = field(
        default_factory=list,
        normalize=tmt.utils.normalize_string_list,
    )
    contact: list[str] = field(
        default_factory=list,
        normalize=tmt.utils.normalize_string_list,
    )
    component: list[str] = field(
        default_factory=list,
        normalize=tmt.utils.normalize_string_list,
    )

    # Test execution data
    path: Optional[str] = None
    framework: Optional[str] = None
    manual: bool = False
    tty: bool = False
    require: list[tmt.base.Dependency] = field(
        default_factory=list,
        normalize=tmt.base.normalize_require,
        serialize=lambda requires: [require.to_spec() for require in requires],
        unserialize=lambda serialized_requires: [
            tmt.base.dependency_factory(require) for require in serialized_requires
        ],
    )
    recommend: list[tmt.base.Dependency] = field(
        default_factory=list,
        normalize=tmt.base.normalize_require,
        serialize=lambda recommends: [recommend.to_spec() for recommend in recommends],
        unserialize=lambda serialized_recommends: [
            tmt.base.DependencySimple.from_spec(recommend)
            if isinstance(recommend, str)
            else tmt.base.DependencyFmfId.from_spec(recommend)
            for recommend in serialized_recommends
        ],
    )
    environment: tmt.utils.Environment = field(
        default_factory=tmt.utils.Environment,
        normalize=tmt.utils.Environment.normalize,
        serialize=lambda environment: environment.to_fmf_spec(),
        unserialize=lambda serialized: tmt.utils.Environment.from_fmf_spec(serialized),
        exporter=lambda environment: environment.to_fmf_spec(),
    )
    check: list[tmt.checks.Check] = field(
        default_factory=list,
        normalize=tmt.checks.normalize_test_checks,
        serialize=lambda checks: [check.to_spec() for check in checks],
        unserialize=lambda serialized: [
            tmt.checks.Check.from_spec(**check) for check in serialized
        ],
        exporter=lambda value: [check.to_minimal_spec() for check in value],
    )
    duration: str = '1h'
    result: str = 'respect'

    # ignore[override]: expected, we do want to accept more specific
    # type than the one declared in superclass.
    @classmethod
    def from_spec(  # type: ignore[override]
        cls, raw_data: dict[str, Any], logger: tmt.log.Logger
    ) -> Self:
        """
        Convert from a specification file or from a CLI option
        """

        data = cls(name=raw_data['name'], test=raw_data['test'])
        data._load_keys(raw_data, cls.__name__, logger)

        return data

    def to_spec(self) -> dict[str, Any]:
        """
        Convert to a form suitable for saving in a specification file
        """

        data = super().to_spec()
        data['link'] = self.link.to_spec() if self.link else None
        data['require'] = [require.to_spec() for require in self.require]
        data['recommend'] = [recommend.to_spec() for recommend in self.recommend]
        data['check'] = [check.to_spec() for check in self.check]
        data['test'] = str(self.test)

        return data


@container
class DiscoverShellData(tmt.steps.discover.DiscoverStepData):
    tests: list[TestDescription] = field(
        default_factory=list,
        normalize=lambda key_address, raw_value, logger: [
            TestDescription.from_spec(raw_datum, logger)
            for raw_datum in cast(list[dict[str, Any]], raw_value)
        ],
        serialize=lambda tests: [test.to_serialized() for test in tests],
        unserialize=lambda serialized_tests: [
            TestDescription.from_serialized(serialized_test)
            for serialized_test in serialized_tests
        ],
    )

    url: Optional[str] = field(
        option="--url",
        metavar='REPOSITORY',
        default=None,
        help="URL of the git repository with tests to be fetched.",
    )

    ref: Optional[str] = field(
        option="--ref",
        metavar='REVISION',
        default=None,
        help="""
            Branch, tag or commit specifying the desired git revision.
            Defaults to the remote repository's default branch.
            """,
    )

    keep_git_metadata: bool = field(
        option="--keep-git-metadata",
        is_flag=True,
        default=False,
        help="""
            By default the ``.git`` directory is removed to save disk space.
            Set to ``true`` to sync the git metadata to guest as well.
            Implicit if ``dist-git-source`` is used.
            """,
    )

    def to_spec(self) -> tmt.steps._RawStepData:
        """
        Convert to a form suitable for saving in a specification file
        """

        data = super().to_spec()
        # ignore[typeddict-unknown-key]: the `tests` key is unknown to generic raw step data,
        # but it's right to be here.
        data['tests'] = [  # type: ignore[typeddict-unknown-key]
            test.to_spec() for test in self.tests
        ]

        return data


@tmt.steps.provides_method('shell')
class DiscoverShell(tmt.steps.discover.DiscoverPlugin[DiscoverShellData]):
    """
    Use provided list of shell script tests.

    List of test cases to be executed can be defined manually directly
    in the plan as a list of dictionaries containing test ``name`` and
    actual ``test`` script. It is also possible to define here any other
    test metadata such as the ``duration`` or a ``path`` to the test.
    The default duration for tests defined directly in the discover step
    is ``1h``.

    Example config:

    .. code-block:: yaml

        discover:
            how: shell
            tests:
              - name: /help/main
                test: tmt --help
              - name: /help/test
                test: tmt test --help
              - name: /help/smoke
                test: ./smoke.sh
                path: /tests/shell

    For DistGit repo one can download sources and use code from them in
    the tests. Sources are extracted into ``$TMT_SOURCE_DIR`` path,
    patches are applied by default. See options to install build
    dependencies or to just download sources without applying patches.
    To apply patches, special ``prepare`` phase with order ``60`` is
    added, and ``prepare`` step has to be enabled for it to run.

    .. code-block:: yaml

        discover:
            how: shell
            dist-git-source: true
            tests:
              - name: /upstream
                test: cd $TMT_SOURCE_DIR/*/tests && make test

    To clone a remote repository and use it as a source specify ``url``.
    It accepts also ``ref`` to checkout provided reference. Dynamic
    reference feature is supported as well.

    .. code-block:: yaml

        discover:
            how: shell
            url: https://github.com/teemtee/tmt.git
            ref: "1.18.0"
            tests:
              - name: first test
                test: ./script-from-the-repo.sh
    """

    _data_class = DiscoverShellData

    _tests: list[tmt.base.Test] = []

    def show(self, keys: Optional[list[str]] = None) -> None:
        """
        Show config details
        """

        super().show([])

        if self.data.tests:
            click.echo(tmt.utils.format('tests', [test.name for test in self.data.tests]))

    def fetch_remote_repository(
        self,
        url: Optional[str],
        ref: Optional[str],
        testdir: Path,
        keep_git_metadata: bool = False,
    ) -> None:
        """
        Fetch remote git repo from given url to testdir
        """

        # Nothing to do if no url provided
        if not url:
            return

        # Clone first - it might clone dist git
        self.info('url', url, 'green')
        tmt.utils.git.git_clone(
            url=url,
            destination=testdir,
            shallow=ref is None,
            env=Environment({"GIT_ASKPASS": EnvVarValue("echo")}),
            logger=self._logger,
        )

        # Resolve possible dynamic references
        try:
            ref = tmt.base.resolve_dynamic_ref(
                logger=self._logger, workdir=testdir, ref=ref, plan=self.step.plan
            )
        except tmt.utils.FileError as error:
            raise tmt.utils.DiscoverError(str(error))

        # Checkout revision if requested
        if ref:
            self.info('ref', ref, 'green')
            self.debug(f"Checkout ref '{ref}'.")
            self.run(Command('git', 'checkout', '-f', ref), cwd=testdir)

        # Log where HEAD leads to
        self.debug('hash', tmt.utils.git.git_hash(directory=testdir, logger=self._logger))

        # Remove .git so that it's not copied to the SUT
        # if 'keep-git-metadata' option is not specified
        if not keep_git_metadata:
            shutil.rmtree(testdir / '.git')

    def go(self, *, logger: Optional[tmt.log.Logger] = None) -> None:
        """
        Discover available tests
        """

        super().go(logger=logger)
        tests = fmf.Tree({'summary': 'tests'})

        assert self.workdir is not None
        testdir = self.workdir / "tests"

        self.log_import_plan_details()

        # dist-git related
        sourcedir = self.workdir / 'source'

        # Fetch remote repository related

        # Git metadata are necessary for dist_git_source
        keep_git_metadata = True if self.data.dist_git_source else self.data.keep_git_metadata

        if self.data.url:
            self.fetch_remote_repository(self.data.url, self.data.ref, testdir, keep_git_metadata)
        else:
            # Symlink tests directory to the plan work tree
            assert self.step.plan.worktree  # narrow type

            relative_path = self.step.plan.worktree.relative_to(self.workdir)
            testdir.symlink_to(relative_path)

            if keep_git_metadata:
                # Copy .git which is excluded when worktree is initialized
                tree_root = Path(self.step.plan.node.root)
                # If exists, git_root can be only the same or parent of fmf_root
                git_root = tmt.utils.git.git_root(fmf_root=tree_root, logger=self._logger)
                if git_root:
                    if git_root != tree_root:
                        raise tmt.utils.DiscoverError(
                            "The 'keep-git-metadata' option can be "
                            "used only when fmf root is the same as git root."
                        )
                    self.run(Command("rsync", "-ar", f"{git_root}/.git", testdir))

        # Check and process each defined shell test
        for data in self.data.tests:
            # Create data copy (we want to keep original data for save()
            data = copy.deepcopy(data)
            # Extract name, make sure it is present
            # TODO: can this ever happen? With annotations, `name: str` and `test: str`, nothing
            # should ever assign `None` there and pass the test.
            if not data.name:
                raise tmt.utils.SpecificationError(
                    f"Missing test name in '{self.step.plan.name}'."
                )
            # Make sure that the test script is defined
            if not data.test:
                raise tmt.utils.SpecificationError(
                    f"Missing test script in '{self.step.plan.name}'."
                )
            # Prepare path to the test working directory (tree root by default)
            data.path = f"/tests{data.path}" if data.path else '/tests'
            # Apply default test duration unless provided
            if not data.duration:
                data.duration = tmt.base.DEFAULT_TEST_DURATION_L2
            # Add source dir path variable
            if self.data.dist_git_source:
                data.environment['TMT_SOURCE_DIR'] = EnvVarValue(sourcedir)

            # Create a simple fmf node, with correct name. Emit only keys and values
            # that are no longer default. Do not add `name` itself into the node,
            # it's not a supported test key, and it's given to the node itself anyway.
            # Note the exception for `duration` key - it's expected in the output
            # even if it still has its default value.
            test_fmf_keys: dict[str, Any] = {
                key: value
                for key, value in data.to_spec().items()
                if key != 'name' and (key == 'duration' or value != data.default(key))
            }
            tests.child(data.name, test_fmf_keys)

        if self.data.dist_git_source:
            assert self.step.plan.my_run is not None  # narrow type
            assert self.step.plan.my_run.tree is not None  # narrow type
            assert self.step.plan.my_run.tree.root is not None  # narrow type
            try:
                run_result = self.run(
                    Command("git", "rev-parse", "--show-toplevel"),
                    cwd=testdir if self.data.url else self.step.plan.my_run.tree.root,
                    ignore_dry=True,
                )
                assert run_result.stdout is not None
                git_root = Path(run_result.stdout.strip('\n'))
            except tmt.utils.RunError:
                assert self.step.plan.my_run is not None  # narrow type
                assert self.step.plan.my_run.tree is not None  # narrow type
                raise tmt.utils.DiscoverError(
                    f"Directory '{self.step.plan.my_run.tree.root}' is not a git repository."
                )
            try:
                self.download_distgit_source(
                    distgit_dir=git_root,
                    target_dir=sourcedir,
                    handler_name=self.data.dist_git_type,
                )
                # Copy rest of files so TMT_SOURCE_DIR has patches, sources and spec file
                # FIXME 'worktree' could be used as sourcedir when 'url' is not set
                shutil.copytree(git_root, sourcedir, symlinks=True, dirs_exist_ok=True)

                if self.data.dist_git_download_only:
                    self.debug("Do not extract sources as 'download_only' is set.")
                else:
                    # Check if prepare is enabled, warn user if not
                    if not self.step.plan.prepare.enabled:
                        self.warn("Sources will not be extracted, prepare step is not enabled.")
                    insert_to_prepare_step(
                        discover_plugin=self,
                        sourcedir=sourcedir,
                    )

            except Exception as error:
                raise tmt.utils.DiscoverError("Failed to process 'dist-git-source'.") from error

        # Use a tmt.Tree to apply possible command line filters
        self._tests = tmt.Tree(logger=self._logger, tree=tests).tests(
            conditions=["manual is False"], sort=False
        )

        # Propagate `where` key and TMT_SOURCE_DIR
        for test in self._tests:
            test.where = cast(tmt.steps.discover.DiscoverStepData, self.data).where
            if self.data.dist_git_source:
                test.environment['TMT_SOURCE_DIR'] = EnvVarValue(sourcedir)

        # Apply tmt run policy
        if self.step.plan.my_run is not None:
            for policy in self.step.plan.my_run.policies:
                policy.apply_to_tests(tests=self._tests, logger=self._logger)

    def tests(
        self, *, phase_name: Optional[str] = None, enabled: Optional[bool] = None
    ) -> list[tmt.steps.discover.TestOrigin]:
        if phase_name is not None and phase_name != self.name:
            return []

        if enabled is None:
            return [
                tmt.steps.discover.TestOrigin(test=test, phase=self.name) for test in self._tests
            ]

        return [
            tmt.steps.discover.TestOrigin(test=test, phase=self.name)
            for test in self._tests
            if test.enabled is enabled
        ]
