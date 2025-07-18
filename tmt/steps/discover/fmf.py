import contextlib
import glob
import re
import shutil
from typing import Any, Optional, cast

import fmf

import tmt
import tmt.base
import tmt.libraries
import tmt.log
import tmt.options
import tmt.steps
import tmt.steps.discover
import tmt.utils
import tmt.utils.filesystem
import tmt.utils.git
from tmt.base import _RawAdjustRule
from tmt.container import container, field
from tmt.steps.prepare.distgit import insert_to_prepare_step
from tmt.utils import Command, Environment, EnvVarValue, Path


def normalize_ref(
    key_address: str,
    value: Optional[Any],
    logger: tmt.log.Logger,
) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, str):
        return value

    raise tmt.utils.NormalizationError(key_address, value, 'unset or a string')


@container
class DiscoverFmfStepData(tmt.steps.discover.DiscoverStepData):
    # Basic options
    url: Optional[str] = field(
        default=cast(Optional[str], None),
        option=('-u', '--url'),
        metavar='REPOSITORY',
        help="""
            Git repository containing the metadata tree.
            Current git repository used by default.
            """,
    )

    ref: Optional[str] = field(
        default=cast(Optional[str], None),
        option=('-r', '--ref'),
        metavar='REVISION',
        help="""
            Branch, tag or commit specifying the desired git
            revision. Defaults to the remote repository's default
            branch if ``url`` was set or to the current ``HEAD``
            of the current repository.

            Additionally, one can set ``ref`` dynamically.
            This is possible using a special file in tmt format
            stored in the *default* branch of a tests repository.
            This special file should contain rules assigning attribute ``ref``
            in an *adjust* block, for example depending on a test run context.

            Dynamic ``ref`` assignment is enabled whenever a test plan
            reference has the format ``ref: @FILEPATH``.
            """,
        normalize=normalize_ref,
    )

    path: Optional[str] = field(
        default=cast(Optional[str], None),
        option=('-p', '--path'),
        metavar='ROOT',
        help="""
            Path to the metadata tree root. Must be relative to
            the git repository root if ``url`` was provided, absolute
            local filesystem path otherwise. By default ``.`` is used.
            """,
    )

    # Selecting tests
    test: list[str] = field(
        default_factory=list,
        option=('-t', '--test'),
        metavar='NAMES',
        multiple=True,
        help="""
            List of test names or regular expressions used to
            select tests by name. Duplicate test names are allowed
            to enable repetitive test execution, preserving the
            listed test order. The search mode is used for pattern
            matching. See the :ref:`regular-expressions` section for
            details.
            """,
        normalize=tmt.utils.normalize_string_list,
    )

    link: list[str] = field(
        default_factory=list,
        option='--link',
        metavar="RELATION:TARGET",
        multiple=True,
        help="""
            Select tests using the :ref:`/spec/core/link` keys.
            Values must be in the form of ``RELATION:TARGET``,
            tests containing at least one of them are selected.
            Regular expressions are supported for both relation
            and target. Relation part can be omitted to match all
            relations.
             """,
    )

    filter: list[str] = field(
        default_factory=list,
        option=('-F', '--filter'),
        metavar='FILTERS',
        multiple=True,
        help="""
            Apply advanced filter based on test metadata attributes.
            See ``pydoc fmf.filter`` for more info.
            """,
        normalize=tmt.utils.normalize_string_list,
    )

    include: list[str] = field(
        default_factory=list,
        option=('-i', '--include'),
        metavar='REGEXP',
        multiple=True,
        help="""
            Include only tests matching given regular expression.
            Respect the :ref:`/spec/core/order` defined in test.
            The search mode is used for pattern matching. See the
            :ref:`regular-expressions` section for details.
            """,
        normalize=tmt.utils.normalize_string_list,
    )

    exclude: list[str] = field(
        default_factory=list,
        option=('-x', '--exclude'),
        metavar='REGEXP',
        multiple=True,
        help="""
            Exclude tests matching given regular expression.
            The search mode is used for pattern matching. See the
            :ref:`regular-expressions` section for details.
            """,
        normalize=tmt.utils.normalize_string_list,
    )

    # Modified only
    modified_only: bool = field(
        default=False,
        option=('-m', '--modified-only'),
        is_flag=True,
        help="""
            Set to ``true`` if you want to filter modified tests
            only. The test is modified if its name starts with
            the name of any directory modified since ``modified-ref``.
            """,
    )

    modified_url: Optional[str] = field(
        default=cast(Optional[str], None),
        option='--modified-url',
        metavar='REPOSITORY',
        help="""
            An additional remote repository to be used as the
            reference for comparison. Will be fetched as a
            reference remote in the test dir.
            """,
    )

    modified_ref: Optional[str] = field(
        default=cast(Optional[str], None),
        option='--modified-ref',
        metavar='REVISION',
        help="""
            The branch, tag or commit specifying the reference git revision (if not provided, the
            default branch is used). Note that you need to specify ``reference/<branch>`` to
            compare to a branch from the repository specified in ``modified-url``.
            """,
    )

    # Dist git integration
    dist_git_init: bool = field(
        default=False,
        option='--dist-git-init',
        is_flag=True,
        help="""
             Set to ``true`` to initialize fmf root inside extracted sources at
             ``dist-git-extract`` location or top directory. To be used when the
             sources contain fmf files (for example tests) but do not have an
             associated fmf root.
             """,
    )
    dist_git_remove_fmf_root: bool = field(
        default=False,
        option='--dist-git-remove-fmf-root',
        is_flag=True,
        help="""
             Remove fmf root from extracted source (top one or selected by copy-path, happens
             before dist-git-extract.
             """,
    )
    dist_git_merge: bool = field(
        default=False,
        option='--dist-git-merge',
        is_flag=True,
        help="""
            Set to ``true`` to combine fmf root from the sources and fmf root from the plan.
            It allows to have plans and tests defined in the DistGit repo which use tests
            and other resources from the downloaded sources. Any plans in extracted sources
            will not be processed.
            """,
    )
    dist_git_extract: Optional[str] = field(
        default=cast(Optional[str], None),
        option='--dist-git-extract',
        help="""
             What to copy from extracted sources, globbing is supported. Defaults to the top fmf
             root if it is present, otherwise top directory (shortcut "/").
             """,
    )

    # Special options
    sync_repo: bool = field(
        default=False,
        option='--sync-repo',
        is_flag=True,
        help="""
             Force the sync of the whole git repo. By default, the repo is copied only if the used
             options require it.
             """,
    )
    fmf_id: bool = field(
        default=False,
        option='--fmf-id',
        is_flag=True,
        help='Only print fmf identifiers of discovered tests to the standard output and exit.',
    )
    prune: bool = field(
        default=False,
        option=('--prune / --no-prune'),
        is_flag=True,
        show_default=True,
        help="Copy only immediate directories of executed tests and their required files.",
    )

    # Edit discovered tests
    adjust_tests: Optional[list[_RawAdjustRule]] = field(
        default_factory=list,
        normalize=tmt.utils.normalize_adjust,
        help="""
             Modify metadata of discovered tests from the plan itself. Use the
             same format as for adjust rules.
             """,
    )

    # Upgrade plan path so the plan is not pruned
    upgrade_path: Optional[str] = None

    # Legacy fields
    repository: Optional[str] = None
    revision: Optional[str] = None

    def post_normalization(
        self,
        raw_data: tmt.steps._RawStepData,
        logger: tmt.log.Logger,
    ) -> None:
        super().post_normalization(raw_data, logger)

        if self.repository:
            self.url = self.repository

        if self.revision:
            self.ref = self.revision


@tmt.steps.provides_method('fmf')
class DiscoverFmf(tmt.steps.discover.DiscoverPlugin[DiscoverFmfStepData]):
    """
    Discover available tests from fmf metadata.

    By default all available tests from the current repository are used
    so the minimal configuration looks like this:

    .. code-block:: yaml

        discover:
            how: fmf

    Full config example:

    .. code-block:: yaml

        discover:
            how: fmf
            url: https://github.com/teemtee/tmt
            ref: main
            path: /fmf/root
            test: /tests/basic
            filter: 'tier: 1'

    If no ``ref`` is provided, the default branch from the origin is used.


    Dist Git
    ^^^^^^^^

    For DistGit repo one can download sources and use code from them in
    the tests. Sources are extracted into ``$TMT_SOURCE_DIR`` path,
    patches are applied by default. See options to install build
    dependencies or to just download sources without applying patches.
    To apply patches, special ``prepare`` phase with order ``60`` is
    added, and ``prepare`` step has to be enabled for it to run.

    It can be used together with ``ref``, ``path`` and ``url``,
    however ``ref`` is not possible without using ``url``.

    .. code-block:: yaml

        discover:
            how: fmf
            dist-git-source: true

    Name Filter
    ^^^^^^^^^^^

    Use the ``test`` key to limit which tests should be executed by
    providing regular expression matching the test name:

    .. code-block:: yaml

        discover:
            how: fmf
            test: ^/tests/area/.*

    .. code-block:: shell

        tmt run discover --how fmf --verbose --test "^/tests/core.*"

    When several regular expressions are provided, tests matching each
    regular expression are concatenated. In this way it is possible to
    execute a single test multiple times:

    .. code-block:: yaml

        discover:
            how: fmf
            test:
              - ^/test/one$
              - ^/test/two$
              - ^/special/setup$
              - ^/test/one$
              - ^/test/two$

    .. code-block:: shell

        tmt run discover -h fmf -v -t '^/test/one$' -t '^/special/setup$' -t '^/test/two$'

    The ``include`` key also allows to select tests by name, with two
    important distinctions from the ``test`` key:

    * The original test :ref:`/spec/core/order` is preserved so it does
      not matter in which order tests are listed under the ``include``
      key.

    * Test duplication is not allowed, so even if a test name is
      repeated several times, test will be executed only once.

    Finally, the ``exclude`` key can be used to specify regular
    expressions matching tests which should be skipped during the
    discovery.

    The ``test``, ``include`` and ``exclude`` keys use search mode for
    matching patterns. See the :ref:`regular-expressions` section for
    detailed information about how exactly the regular expressions are
    handled.

    Link Filter
    ^^^^^^^^^^^

    Selecting tests containing specified link is possible using ``link``
    key accepting ``RELATION:TARGET`` format of values. Regular
    expressions are supported for both relation and target part of the
    value. Relation can be omitted to target match any relation.

    .. code-block:: yaml

        discover:
            how: fmf
            link: verifies:.*issue/850$

    Advanced Filter
    ^^^^^^^^^^^^^^^

    The ``filter`` key can be used to apply an advanced filter based on
    test metadata attributes. These can be especially useful when tests
    are grouped by the :ref:`/spec/core/tag` or :ref:`/spec/core/tier`
    keys:

    .. code-block:: yaml

        discover:
            how: fmf
            filter: tier:3 & tag:provision

    .. code-block:: shell

        tmt run discover --how fmf --filter "tier:3 & tag:provision"

    See the ``pydoc fmf.filter`` documentation for more details about
    the supported syntax and available operators.

    Modified Tests
    ^^^^^^^^^^^^^^

    It is also possible to limit tests only to those that have changed
    in git since a given revision. This can be particularly useful when
    testing changes to tests themselves (e.g. in a pull request CI).

    Related keys: ``modified-only``, ``modified-url``, ``modified-ref``

    Example to compare local repo against upstream ``main`` branch:

    .. code-block:: yaml

        discover:
            how: fmf
            modified-only: True
            modified-url: https://github.com/teemtee/tmt
            modified-ref: reference/main

    Note that internally the modified tests are appended to the list
    specified via ``test``, so those tests will also be selected even if
    not modified.

    Adjust Tests
    ^^^^^^^^^^^^

    Use the ``adjust-tests`` key to modify the discovered tests'
    metadata directly from the plan. For example, extend the test
    duration for slow hardware or modify the list of required packages
    when you do not have write access to the remote test repository.
    The value should follow the ``adjust`` rules syntax.

    The following example adds an ``avc`` check for each discovered
    test, doubles its duration and replaces each occurrence of the word
    ``python3.11`` in the list of required packages.

    .. code-block:: yaml

        discover:
            how: fmf
            adjust-tests:
              - check+:
                  - how: avc
              - duration+: '*2'
                because: Slow system under test
                when: arch == i286
              - require~:
                  - '/python3.11/python3.12/'
    """

    _data_class = DiscoverFmfStepData

    # Options which require .git to be present for their functionality
    _REQUIRES_GIT = (
        "ref",
        "modified-url",
        "modified-only",
        "fmf-id",
    )

    @property
    def is_in_standalone_mode(self) -> bool:
        """
        Enable standalone mode when listing fmf ids
        """

        if self.opt('fmf_id'):
            return True
        return super().is_in_standalone_mode

    def get_git_root(self, directory: Path) -> Path:
        """
        Find git root of the path
        """

        output = self.run(
            Command("git", "rev-parse", "--show-toplevel"),
            cwd=directory,
            ignore_dry=True,
        )
        assert output.stdout is not None
        return Path(output.stdout.strip("\n"))

    def go(self, *, logger: Optional[tmt.log.Logger] = None) -> None:
        """
        Discover available tests
        """

        super().go(logger=logger)

        # Check url and path, prepare test directory
        url = self.get('url')
        # FIXME: cast() - typeless "dispatcher" method
        path = Path(cast(str, self.get('path'))) if self.get('path') else None
        # Save the test directory so that others can reference it
        ref = self.get('ref')
        assert self.workdir is not None
        self.testdir = self.workdir / 'tests'
        sourcedir = self.workdir / 'source'
        dist_git_source = self.get('dist-git-source', False)
        dist_git_merge = self.get('dist-git-merge', False)

        # No tests are selected in some cases
        self._tests: list[tmt.Test] = []

        # Self checks
        if dist_git_source and not dist_git_merge and (ref or url):
            raise tmt.utils.DiscoverError(
                "Cannot manipulate with dist-git without the `--dist-git-merge` option."
            )

        self.log_import_plan_details()

        # Clone provided git repository (if url given) with disabled
        # prompt to ignore possibly missing or private repositories
        if url:
            self.info('url', url, 'green')
            self.debug(f"Clone '{url}' to '{self.testdir}'.")
            # Shallow clone to speed up testing and
            # minimize data transfers if ref is not provided
            tmt.utils.git.git_clone(
                url=url,
                destination=self.testdir,
                shallow=ref is None,
                env=Environment({"GIT_ASKPASS": EnvVarValue("echo")}),
                logger=self._logger,
            )
            git_root = self.testdir
        # Copy git repository root to workdir
        else:
            if path is not None:
                fmf_root: Optional[Path] = path
            else:
                fmf_root = Path(self.step.plan.fmf_root) if self.step.plan.fmf_root else None
            requires_git = self.opt('sync-repo') or any(
                self.get(opt) for opt in self._REQUIRES_GIT
            )
            # Path for distgit sources cannot be checked until the
            # they are extracted
            if path and not path.is_dir() and not dist_git_source:
                raise tmt.utils.DiscoverError(f"Provided path '{path}' is not a directory.")
            if dist_git_source:
                # Ensure we're in a git repo when extracting dist-git sources
                try:
                    git_root = self.get_git_root(Path(self.step.plan.node.root))
                except tmt.utils.RunError:
                    assert self.step.plan.my_run is not None  # narrow type
                    assert self.step.plan.my_run.tree is not None  # narrow type
                    raise tmt.utils.DiscoverError(f"{self.step.plan.node.root} is not a git repo")
            else:
                if fmf_root is None:
                    raise tmt.utils.DiscoverError("No metadata found in the current directory.")
                # Check git repository root (use fmf root if not found)
                try:
                    git_root = self.get_git_root(fmf_root)
                except tmt.utils.RunError:
                    self.debug(f"Git root not found, using '{fmf_root}.'")
                    git_root = fmf_root
                # Set path to relative path from the git root to fmf root
                path = fmf_root.resolve().relative_to(
                    git_root.resolve() if requires_git else fmf_root.resolve()
                )

            # And finally copy the git/fmf root directory to testdir
            # (for dist-git case only when merge explicitly requested)
            if requires_git:
                directory: Path = git_root
            else:
                assert fmf_root is not None  # narrow type
                directory = fmf_root
            self.info('directory', directory, 'green')
            if not dist_git_source or dist_git_merge:
                self.debug(f"Copy '{directory}' to '{self.testdir}'.")
                if not self.is_dry_run:
                    tmt.utils.filesystem.copy_tree(directory, self.testdir, self._logger)

        # Prepare path of the dynamic reference
        try:
            ref = tmt.base.resolve_dynamic_ref(
                logger=self._logger,
                workdir=self.testdir,
                ref=ref,
                plan=self.step.plan,
            )
        except tmt.utils.FileError as error:
            raise tmt.utils.DiscoverError(str(error))

        # Checkout revision if requested
        if ref:
            self.info('ref', ref, 'green')
            self.debug(f"Checkout ref '{ref}'.")
            self.run(Command('git', 'checkout', '-f', ref), cwd=self.testdir)

        # Show current commit hash if inside a git repository
        if self.testdir.is_dir():
            with contextlib.suppress(tmt.utils.RunError, AttributeError):
                self.verbose(
                    'hash',
                    tmt.utils.git.git_hash(directory=self.testdir, logger=self._logger),
                    'green',
                )

        # Dist-git source processing during discover step
        if dist_git_source:
            try:
                distgit_dir = self.testdir if ref else git_root
                self.process_distgit_source(distgit_dir, sourcedir)
                return
            except Exception as error:
                raise tmt.utils.DiscoverError("Failed to process 'dist-git-source'.") from error

        # Discover tests
        self.do_the_discovery(path)

        # Apply tmt run policy
        if self.step.plan.my_run is not None:
            for policy in self.step.plan.my_run.policies:
                policy.apply_to_tests(tests=self._tests, logger=self._logger)

    def process_distgit_source(self, distgit_dir: Path, sourcedir: Path) -> None:
        """
        Process dist-git source during the discover step.
        """

        self.download_distgit_source(
            distgit_dir=distgit_dir,
            target_dir=sourcedir,
            handler_name=self.get('dist-git-type'),
        )

        # Copy rest of files so TMT_SOURCE_DIR has patches, sources and spec file
        # FIXME 'worktree' could be used as sourcedir when 'url' is not set
        tmt.utils.filesystem.copy_tree(
            distgit_dir,
            sourcedir,
            self._logger,
        )

        # patch & rediscover will happen later in the prepare step
        if not self.get('dist-git-download-only'):
            # Check if prepare is enabled, warn user if not
            if not self.step.plan.prepare.enabled:
                self.warn("Sources will not be extracted, prepare step is not enabled.")

            insert_to_prepare_step(
                discover_plugin=self,
                sourcedir=sourcedir,
            )

        # merge or not, detect later
        self.step.plan.discover.extract_tests_later = True
        self.info("Tests will be discovered after dist-git patching in prepare.")

    def do_the_discovery(self, path: Optional[Path] = None) -> None:
        """
        Discover the tests
        """

        # Original path might adjusted already in go()
        if path is None:
            path = Path(cast(str, self.get('path'))) if self.get('path') else None
        prune = self.get('prune')
        # Adjust path and optionally show
        if path is None or path.resolve() == Path.cwd().resolve():
            path = Path('')
        else:
            self.info('path', path, 'green')

        # Prepare the whole tree path
        tree_path = self.testdir / path.unrooted()
        if not tree_path.is_dir() and not self.is_dry_run:
            raise tmt.utils.DiscoverError(f"Metadata tree path '{path}' not found.")

        # Show filters and test names if provided
        # Check the 'test --filter' option first, then from discover
        filters = list(tmt.base.Test._opt('filters') or self.get('filter', []))
        for filter_ in filters:
            self.info('filter', filter_, 'green')
        # Names of tests selected by --test option
        names = self.get('test', [])
        if names:
            self.info('tests', fmf.utils.listed(names), 'green')

        # Check the 'test --link' option first, then from discover
        # FIXME: cast() - typeless "dispatcher" method
        raw_link_needles = cast(list[str], tmt.Test._opt('links', []) or self.get('link', []))
        link_needles = [
            tmt.base.LinkNeedle.from_spec(raw_needle) for raw_needle in raw_link_needles
        ]

        for link_needle in link_needles:
            self.info('link', str(link_needle), 'green')

        excludes = list(tmt.base.Test._opt('exclude') or self.data.exclude)
        includes = list(tmt.base.Test._opt('include') or self.data.include)

        # Filter only modified tests if requested
        modified_only = self.get('modified-only')
        modified_url = self.get('modified-url')
        if modified_url:
            previous = modified_url
            modified_url = tmt.utils.git.clonable_git_url(modified_url)
            self.info('modified-url', modified_url, 'green')
            if previous != modified_url:
                self.debug(f"Original url was '{previous}'.")
            self.debug(f"Fetch also '{modified_url}' as 'reference'.")
            self.run(
                Command('git', 'remote', 'add', 'reference', modified_url),
                cwd=self.testdir,
            )
            self.run(
                Command('git', 'fetch', 'reference'),
                cwd=self.testdir,
            )
        if modified_only:
            modified_ref = self.get(
                'modified-ref',
                tmt.utils.git.default_branch(repository=self.testdir, logger=self._logger),
            )
            self.info('modified-ref', modified_ref, 'green')
            ref_commit = self.run(
                Command('git', 'rev-parse', '--short', str(modified_ref)),
                cwd=self.testdir,
            )
            assert ref_commit.stdout is not None
            self.verbose('modified-ref hash', ref_commit.stdout.strip(), 'green')
            output = self.run(
                Command(
                    'git', 'log', '--format=', '--stat', '--name-only', f"{modified_ref}..HEAD"
                ),
                cwd=self.testdir,
            )
            if output.stdout:
                directories = [Path(name).parent for name in output.stdout.split('\n')]
                modified = {
                    f"^/{re.escape(str(directory))}" for directory in directories if directory
                }
                if not modified:
                    # Nothing was modified, do not select anything
                    return
                self.debug(f"Limit to modified test dirs: {modified}", level=3)
                names.extend(modified)
            else:
                self.debug(f"No modified directories between '{modified_ref}..HEAD' found.")
                # Nothing was modified, do not select anything
                return

        # Initialize the metadata tree, search for available tests
        self.debug(f"Check metadata tree in '{tree_path}'.")
        if self.is_dry_run:
            return
        tree = tmt.Tree(
            logger=self._logger,
            path=tree_path,
            fmf_context=self.step.plan._fmf_context,
            additional_rules=self.data.adjust_tests,
        )
        self._tests = tree.tests(
            filters=filters,
            names=names,
            conditions=["manual is False"],
            unique=False,
            links=link_needles,
            includes=includes,
            excludes=excludes,
        )

        if prune:
            # Save fmf metadata
            clonedir = self.clone_dirpath / 'tests'
            clone_tree_path = clonedir / path.unrooted()
            for file_path in tmt.utils.filter_paths(tree_path, [r'\.fmf']):
                tmt.utils.filesystem.copy_tree(
                    file_path,
                    clone_tree_path / file_path.relative_to(tree_path),
                    self._logger,
                )

            # Save upgrade plan
            upgrade_path = self.get('upgrade_path')
            if upgrade_path:
                upgrade_path = f"{upgrade_path.lstrip('/')}.fmf"
                (clone_tree_path / upgrade_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tree_path / upgrade_path, clone_tree_path / upgrade_path)
                shutil.copymode(tree_path / upgrade_path, clone_tree_path / upgrade_path)

        # Prefix tests and handle library requires
        for test in self._tests:
            # Propagate `where` key
            test.where = cast(tmt.steps.discover.DiscoverStepData, self.data).where

            if prune:
                # Save only current test data
                assert test.path is not None  # narrow type
                relative_test_path = test.path.unrooted()
                tmt.utils.filesystem.copy_tree(
                    tree_path / relative_test_path,
                    clone_tree_path / relative_test_path,
                    self._logger,
                )

                # Copy all parent main.fmf files
                parent_dir = relative_test_path
                while parent_dir.resolve() != Path.cwd().resolve():
                    parent_dir = parent_dir.parent
                    if (tree_path / parent_dir / 'main.fmf').exists():
                        # Ensure parent directory exists
                        (clone_tree_path / parent_dir).mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(
                            tree_path / parent_dir / 'main.fmf',
                            clone_tree_path / parent_dir / 'main.fmf',
                        )

            # Prefix test path with 'tests' and possible 'path' prefix
            assert test.path is not None  # narrow type
            test.path = Path('/tests') / path.unrooted() / test.path.unrooted()
            # Check for possible required beakerlib libraries
            if test.require or test.recommend:
                test.require, test.recommend, _ = tmt.libraries.dependencies(
                    original_require=test.require,
                    original_recommend=test.recommend,
                    parent=self,
                    logger=self._logger,
                    source_location=self.testdir,
                    target_location=clonedir if prune else self.testdir,
                )

        if prune:
            # Clean self.testdir and copy back only required tests and files from clonedir
            # This is to have correct paths in tests
            shutil.rmtree(self.testdir, ignore_errors=True)
            tmt.utils.filesystem.copy_tree(clonedir, self.testdir, self._logger)

        # Cleanup clone directories
        if self.clone_dirpath.exists():
            shutil.rmtree(self.clone_dirpath, ignore_errors=True)

    def post_dist_git(self, created_content: list[Path]) -> None:
        """
        Discover tests after dist-git applied patches
        """

        # Directory to copy out from sources
        dist_git_extract = self.get('dist-git-extract', None)
        dist_git_init = self.get('dist-git-init', False)
        dist_git_merge = self.get('dist-git-merge', False)
        dist_git_remove_fmf_root = self.get('dist-git-remove-fmf-root', False)

        assert self.workdir is not None  # narrow type
        sourcedir = self.workdir / 'source'

        # '/' means everything which was extracted from the srpm and do not flatten
        # glob otherwise
        if dist_git_extract and dist_git_extract != '/':
            try:
                dist_git_extract = Path(
                    glob.glob(str(sourcedir / dist_git_extract.lstrip('/')))[0]
                )
            except IndexError:
                raise tmt.utils.DiscoverError(
                    f"Couldn't glob '{dist_git_extract}' within extracted sources."
                )
        if dist_git_init:
            if dist_git_extract == '/' or not dist_git_extract:
                dist_git_extract = '/'
                location = sourcedir
            else:
                location = dist_git_extract
            # User specified location or 'root' of extracted sources
            if not (Path(location) / '.fmf').is_dir() and not self.is_dry_run:
                fmf.Tree.init(location)
        elif dist_git_remove_fmf_root:
            try:
                extracted_fmf_root = tmt.utils.find_fmf_root(
                    sourcedir,
                    ignore_paths=[sourcedir],
                )[0]
            except tmt.utils.MetadataError:
                self.warn("No fmf root to remove, there isn't one already.")
            if not self.is_dry_run:
                shutil.rmtree((dist_git_extract or extracted_fmf_root) / '.fmf')
        if not dist_git_extract:
            try:
                top_fmf_root = tmt.utils.find_fmf_root(sourcedir, ignore_paths=[sourcedir])[0]
            except tmt.utils.MetadataError:
                dist_git_extract = '/'  # Copy all extracted files as well (but later)
                if not dist_git_merge:
                    self.warn(
                        "Extracted sources do not contain fmf root, "
                        "merging with plan data. Avoid this warning by "
                        "explicit use of the '--dist-git-merge' option."
                    )
                    # FIXME - Deprecate this behavior?
                    git_root = self.get_git_root(Path(self.step.plan.node.root))
                    self.debug(f"Copy '{git_root}' to '{self.testdir}'.")
                    if not self.is_dry_run:
                        tmt.utils.filesystem.copy_tree(git_root, self.testdir, self._logger)

        # Copy extracted sources into testdir
        if not self.is_dry_run:
            flatten = True
            if dist_git_extract == '/':
                flatten = False
                copy_these = created_content
            elif dist_git_extract:
                copy_these = [dist_git_extract.relative_to(sourcedir)]
            else:
                copy_these = [top_fmf_root.relative_to(sourcedir)]
            for to_copy in copy_these:
                src = sourcedir / to_copy
                if src.is_dir():
                    tmt.utils.filesystem.copy_tree(
                        sourcedir / to_copy,
                        self.testdir if flatten else self.testdir / to_copy,
                        self._logger,
                    )
                else:
                    shutil.copyfile(src, self.testdir / to_copy)

        # Discover tests
        self.do_the_discovery()

        # Add TMT_SOURCE_DIR variable for each test
        for test in self._tests:
            test.environment['TMT_SOURCE_DIR'] = EnvVarValue(sourcedir)

        # Apply tmt run policy
        if self.step.plan.my_run is not None:
            for policy in self.step.plan.my_run.policies:
                policy.apply_to_tests(tests=self._tests, logger=self._logger)

        # Inject newly found tests into parent discover at the right position
        # FIXME
        # Prefix test name only if multiple plugins configured
        prefix = f'/{self.name}' if len(self.step.phases()) > 1 else ''
        # Check discovered tests, modify test name/path
        for test_origin in self.tests(enabled=True):
            test = test_origin.test

            test.name = f"{prefix}{test.name}"
            test.path = Path(f"/{self.safe_name}{test.path}")
            # Update test environment with plan environment
            test.environment.update(self.step.plan.environment)
            self.step.plan.discover._tests[self.name].append(test)
            test.serial_number = self.step.plan.draw_test_serial_number(test)
        self.step.save()
        self.step.summary()

    def tests(
        self, *, phase_name: Optional[str] = None, enabled: Optional[bool] = None
    ) -> list[tmt.steps.discover.TestOrigin]:
        """
        Return all discovered tests
        """

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
