"""
Easily try tests and experiment with guests
"""

import enum
import re
import textwrap
from collections.abc import Iterator
from typing import Any, cast

import fmf
import fmf.utils

import tmt
import tmt.base
import tmt.config
import tmt.container
import tmt.log
import tmt.steps
import tmt.steps.execute
import tmt.steps.prepare
import tmt.steps.prepare.feature
import tmt.templates
import tmt.utils
from tmt import Plan
from tmt.base import RunData
from tmt.steps.prepare import PreparePlugin
from tmt.utils import GeneralError, MetadataError, Path
from tmt.utils.themes import style

USER_PLAN_NAME = "/user/plan"


class Action(enum.Enum):
    """
    Available actions and their keyboard shortcuts
    """

    TEST = "t", "rediscover tests and execute them again"
    LOGIN = "l", "log into the guest for experimenting"
    VERBOSE = "v", "set the desired level of verbosity"
    DEBUG = "b", "choose a different debugging level"

    DISCOVER = "d", "gather information about tests to be executed"
    PREPARE = "p", "prepare the environment for testing"
    EXECUTE = "e", "run tests using the specified executor"
    REPORT = "r", "provide test results overview and send reports"
    FINISH = "f", "perform the finishing tasks, clean up guests"

    KEEP = "k", "exit the session but keep the run for later use"
    QUIT = "q", "clean up the run and quit the session"

    START_LOGIN = "-", "jump directly to login after start"
    START_ASK = "-", "do nothing without first asking the user"
    START_TEST = "-", "start directly with executing detected tests"

    @property
    def key(self) -> str:
        """
        Keyboard shortcut
        """

        return self.value[0]

    @property
    def description(self) -> str:
        """
        Action description
        """

        return self.value[1]

    @property
    def action(self) -> str:
        """
        Action name in lower case
        """

        return self.name.lower()

    @property
    def menu(self) -> str:
        """
        Show menu with the keyboard shortcut highlighted
        """

        index = self.action.index(self.key)

        before = style(self.action[0:index], fg="bright_blue")
        key = style(self.key, fg="blue", bold=True, underline=True)
        after = style(self.action[index + 1 :], fg="bright_blue")

        longest = max(len(action.name) for action in Action)
        padding = " " * (longest + 3 - len(self.action))

        return before + key + after + padding + self.description

    @classmethod
    def find(cls, answer: str) -> "Action":
        """
        Return action for given keyboard input (shortcut or whole word)
        """

        answer = answer.lower()

        for action in cls:
            if answer in (action.key, action.action):
                return action

        raise KeyError


class Try(tmt.utils.Common):
    def __init__(
        self,
        *,
        tree: tmt.Tree,
        logger: tmt.log.Logger,
        **kwargs: Any,
    ) -> None:
        """
        Just store the tree
        """

        super().__init__(logger=logger, **kwargs)

        self.tree = tree
        self.tests: list[tmt.Test] = []
        self.plans: list[Plan] = []
        self.image_and_how = self.opt("image_and_how")
        self.cli_options = ["epel", "fips", "install"]

        # Use the verbosity level 3 unless user explicitly requested
        # a different level on the command line
        if self.verbosity_level == 0:
            self.verbosity_level: int = 3

        # Use the interactive mode during test execution
        tmt.steps.execute.Execute.store_cli_invocation(
            context=None,
            options={"interactive": True},
        )

    def _show_exception(self, exc: Exception) -> None:
        """
        A little helper for consistent exception reporting.
        """

        tmt.utils.show_exception(
            exc,
            traceback_verbosity=tmt.utils.TracebackVerbosity.DEFAULT,
            include_logfiles=True,
        )

    def check_tree(self) -> None:
        """
        Make sure there is a sane metadata tree
        """

        # Both tree and root should be defined
        try:
            if self.tree and self.tree.root:
                return

        # Create a dumb fmf Tree if no metadata around
        except MetadataError:
            self.tree.tree = fmf.Tree({"nothing": "here"})

    def check_tests(self) -> None:
        """
        Check for available tests
        """

        # Search for tests according to provided names
        test_names = list(self.opt("test"))
        if test_names:
            self.tests = self.tree.tests(names=test_names)
            if not self.tests:
                raise tmt.utils.GeneralError(
                    f"No test matching '{fmf.utils.listed(test_names)}' found."
                )

        # Default to tests under the current working directory
        else:
            if not self.tree.root:
                self.debug("No fmf tree root, no tests.")
                return
            relative_path = Path(".").relative_to(self.tree.root)
            test_names = [f"^/{relative_path}"]
            self.tests = self.tree.tests(names=test_names)
            if not self.tests:
                self.warn(f"No tests found under the '{relative_path}' directory.")

        # Short debug info about what was found
        self.debug("Test name filter", fmf.utils.listed(test_names, quote="'"))
        self.debug("Matching tests found\n" + tmt.utils.format_value(self.tests))

        # Inject the test filtering options into the Test class
        options = {"names": [f"^{re.escape(test.name)}$" for test in self.tests]}
        tmt.Test.store_cli_invocation(context=None, options=options)

    def get_default_plans(self, run: tmt.base.Run) -> list[Plan]:
        """
        Get default plan from user config or the standard template
        """

        # Check user config for custom default plans. Search for all
        # plans starting with the default user plan name (there might be
        # more than just one).
        config_tree = tmt.config.Config(self._logger).fmf_tree
        if config_tree is not None:
            plan_name = re.escape(USER_PLAN_NAME)
            # cast: once fmf is properly annotated, cast() would not be needed.
            # pyright isn't able to infer the type.
            user_plans = list(cast(Iterator[fmf.Tree], config_tree.prune(names=[f"^{plan_name}"])))
            if user_plans:
                for user_plan in user_plans:
                    plan_dict: dict[str, Any] = {user_plan.name: user_plan.data}
                    self.tree.tree.update(plan_dict)
                self.debug("Use the default user plan config.")
                return self.tree.plans(names=[f"^{plan_name}"], run=run)

        # Use the default plan template otherwise
        plan_name = re.escape(tmt.templates.DEFAULT_PLAN_NAME)
        plan_dict = tmt.utils.yaml_to_dict(tmt.templates.MANAGER.render_default_plan())
        self.tree.tree.update(plan_dict)
        self.debug("Use the default plan template.")
        return self.tree.plans(names=[f"^{plan_name}"], run=run)

    def check_plans(self, run: tmt.base.Run) -> None:
        """
        Check for plans to be used for testing
        """

        # Search for matching plans if plan names provided
        plan_names = list(self.opt("plan"))
        if plan_names:
            self.debug("Plan names filter", fmf.utils.listed(plan_names, quote="'"))
            self.plans = self.tree.plans(names=plan_names, run=run)
            if not self.plans:
                raise tmt.utils.GeneralError(
                    f"No plan matching '{fmf.utils.listed(plan_names)}' found."
                )

        # Use default plans if no plan names requested
        else:
            self.plans = self.get_default_plans(run)

        self.debug("Matching plans found\n" + tmt.utils.format_value(self.plans))

        # Attach a login instance to each plan
        for plan in self.plans:
            plan.login = tmt.steps.Login(
                logger=plan.provision._logger.descend(),
                step=plan.provision,
                order=tmt.steps.PHASE_END,
            )

    def welcome(self) -> None:
        """
        Welcome message with summary of what we're going to try
        """

        parts = ["Let's try"]

        # Test names, login, or something
        test_names = [style(test.name, fg="red") for test in self.tests]
        if self.opt("login"):
            parts += [style("login", fg="red")]
        elif test_names and not self.opt("ask"):
            parts += [fmf.utils.listed(test_names, 'test', max=3)]
        else:
            parts += ["something"]
        parts += ["with"]

        # Plan names
        plan_names = [style(plan.name, fg="magenta") for plan in self.plans]
        parts += [fmf.utils.listed(plan_names, 'plan', max=3)]

        # Image names
        if self.image_and_how:
            parts += ["on"]
            image_names = [style(image, fg="blue") for image in self.image_and_how]
            parts += [fmf.utils.listed(image_names)]

        self.print(" ".join(parts) + ".")

    def save(self) -> None:
        """
        Save list of selected plans and enabled steps
        """
        assert self.tree is not None  # narrow type
        assert self._cli_context_object is not None  # narrow type
        data = RunData(
            root=str(self.tree.root) if self.tree.root else None,
            plans=[plan.name for plan in self.plans],
            steps=list(self._cli_context_object.steps),
            environment=self.environment,
            remove=self.opt('remove'),
        )
        self.write(Path('run.yaml'), tmt.utils.dict_to_yaml(data.to_serialized()))

    def choose_action(self) -> Action:
        """
        Print menu, get next action
        """

        while True:
            self.print(
                textwrap.dedent(f"""
                    What do we do next?

                        {Action.TEST.menu}
                        {Action.LOGIN.menu}
                        {Action.VERBOSE.menu}
                        {Action.DEBUG.menu}

                        {Action.DISCOVER.menu}
                        {Action.PREPARE.menu}
                        {Action.EXECUTE.menu}
                        {Action.REPORT.menu}
                        {Action.FINISH.menu}

                        {Action.KEEP.menu}
                        {Action.QUIT.menu}
                """)
            )

            try:
                answer = input("> ")
            except EOFError:
                return Action.QUIT

            try:
                self.print("")
                return Action.find(answer)
            except KeyError:
                self.print(style(f"Invalid action '{answer}'.", fg="red"))

    def action_start(self, plan: Plan) -> None:
        """
        Common start actions
        """

        plan.wake()

    def action_start_test(self, plan: Plan) -> None:
        """
        Start with testing
        """

        self.action_start(plan)

        plan.discover.go()
        plan.provision.go()
        try:
            plan.prepare.go()
        except GeneralError as error:
            self._show_exception(error)
            return
        plan.execute.go()

    def action_start_login(self, plan: Plan) -> None:
        """
        Start with login
        """

        self.action_start(plan)

        plan.provision.go()
        try:
            plan.prepare.go()
        except GeneralError as error:
            self._show_exception(error)
            return
        assert plan.login is not None  # Narrow type
        plan.login.go(force=True)

    def action_start_ask(self, plan: Plan) -> None:
        """
        Ask what to do
        """

        self.action_start(plan)

        plan.provision.go()

    def action_test(self, plan: Plan) -> None:
        """
        Test again
        """

        plan.discover.go(force=True)
        plan.execute.go(force=True)

    def action_login(self, plan: Plan) -> None:
        """
        Log into the guest
        """

        assert plan.login is not None  # Narrow type
        plan.login.go(force=True)

    def prompt_verbose(self) -> None:
        """
        Ask for the desired verbosity level
        """

        self.print("What verbose level do you need?")
        answer = input(f"Choose 0-3, hit Enter to keep {self.verbosity_level}> ")

        if answer == "":
            self.print(f"Keeping verbose level {self.verbosity_level}.")
            return

        try:
            self.verbosity_level = int(answer)
            self.print(f"Switched to verbose level {self.verbosity_level}.")
        except ValueError:
            self.print(f"Invalid level '{answer}'.")

    def action_verbose(self, plan: Plan) -> None:
        """
        Set verbosity level of all loggers in given plan
        """

        for step in plan.steps(enabled_only=False):
            step.verbosity_level = self.verbosity_level
            for phase in step.phases():
                phase.verbosity_level = self.verbosity_level

    def prompt_debug(self) -> None:
        """
        Choose the right debug level
        """

        self.print("Which debug level would you like?")
        answer = input(f"Choose 0-3, hit Enter to keep {self.debug_level}> ")

        if answer == "":
            self.print(f"Keeping debug level {self.debug_level}.")
            return

        try:
            self.debug_level = int(answer)
            self.print(f"Switched to debug level {self.debug_level}.")
        except ValueError:
            self.print(f"Invalid level '{answer}'.")

    def action_debug(self, plan: Plan) -> None:
        """
        Set verbosity level of all loggers in given plan
        """

        for step in plan.steps(enabled_only=False):
            step.debug_level = self.debug_level
            for phase in step.phases():
                phase.debug_level = self.debug_level

    def action_discover(self, plan: Plan) -> None:
        """
        Discover tests
        """

        plan.discover.go(force=True)

    def action_prepare(self, plan: Plan) -> None:
        """
        Prepare the guest
        """

        try:
            plan.prepare.go(force=True)
        except GeneralError as error:
            self._show_exception(error)

    def action_execute(self, plan: Plan) -> None:
        """
        Execute tests
        """

        plan.execute.go(force=True)

    def action_report(self, plan: Plan) -> None:
        """
        Report results
        """

        plan.report.go(force=True)

    def action_finish(self, plan: Plan) -> None:
        """
        Clean up guests and finish
        """

        plan.finish.go()

    def action_keep(self, plan: Plan) -> None:
        """
        Keep run and exit the session
        """

        assert plan.my_run is not None  # Narrow type
        run_id = style(str(plan.my_run.workdir), fg="magenta")
        self.print(f"Run {run_id} kept unfinished. See you soon!")

    def action_quit(self, plan: Plan) -> None:
        """
        Clean up the run and quit the session
        """

        # Finish the run unless already done
        if plan.finish.status() != "done":
            plan.finish.go()

        # Mention the run id and say good bye
        assert plan.my_run is not None  # Narrow type
        run_id = style(str(plan.my_run.workdir), fg="magenta")
        self.print(f"Run {run_id} successfully finished. Bye for now!")

    def handle_options(self, plan: Plan) -> None:
        """
        Choose requested cli option
        """

        for option in self.cli_options:
            if self.opt(option):
                getattr(self, f"handle_{option}")(plan)

    def handle_epel(self, plan: Plan) -> None:
        """
        Enable EPEL repository
        """

        # tmt run prepare --how feature --epel enabled
        # cast: linters do not detect the class `get_class_data()`
        # returns, it's reported as `type[Unknown]`. mypy does not care,
        # pyright does.
        prepare_data_class = cast(  # type: ignore[redundant-cast]
            type[tmt.steps.prepare.feature.PrepareFeatureData],
            tmt.steps.prepare.feature.PrepareFeature.get_data_class(),
        )

        if not tmt.container.container_has_field(prepare_data_class, 'epel'):
            raise GeneralError("Feature 'epel' is not available.")

        # ignore[reportCallIssue,call-arg,unused-ignore]: thanks to
        # dynamic nature of the data class, the field is indeed unknown
        # to type checkers.
        data = prepare_data_class(
            name="tmt-try-epel",
            how='feature',
            epel="enabled",  # type: ignore[reportCallIssue,call-arg,unused-ignore]
        )

        phase: PreparePlugin[Any] = cast(
            PreparePlugin[Any],
            PreparePlugin.delegate(plan.prepare, data=data),
        )

        plan.prepare._phases.append(phase)

    def handle_fips(self, plan: Plan) -> None:
        """
        Enable FIPS mode
        """

        # tmt run prepare --how feature --fips enabled
        # cast: linters do not detect the class `get_class_data()`
        # returns, it's reported as `type[Unknown]`. mypy does not care,
        # pyright does.
        prepare_data_class = cast(  # type: ignore[redundant-cast]
            type[tmt.steps.prepare.feature.PrepareFeatureData],
            tmt.steps.prepare.feature.PrepareFeature.get_data_class(),
        )

        if not tmt.container.container_has_field(prepare_data_class, 'fips'):
            raise GeneralError("Feature 'fips' is not available.")

        # ignore[reportCallIssue,call-arg,unused-ignore]: thanks to
        # dynamic nature of the data class, the field is indeed unknown
        # to type checkers.
        data = prepare_data_class(
            name="tmt-try-fips",
            how='feature',
            fips="enabled",  # type: ignore[reportCallIssue,call-arg,unused-ignore]
        )

        phase: PreparePlugin[Any] = cast(
            PreparePlugin[Any],
            PreparePlugin.delegate(plan.prepare, data=data),
        )

        plan.prepare._phases.append(phase)

    def handle_install(self, plan: Plan) -> None:
        """
        Install local rpm package on the guest.
        """

        # tmt run prepare --how install --package PACKAGE
        from tmt.steps.prepare.install import PrepareInstallData

        data = PrepareInstallData(
            name="tmt-try-install",
            how='install',
            package=list(self.opt("install")),
        )

        phase: PreparePlugin[Any] = cast(
            PreparePlugin[Any],
            PreparePlugin.delegate(plan.prepare, data=data),
        )

        plan.prepare._phases.append(phase)

    def go(self) -> None:
        """
        Run the interactive session
        """

        # Create run, prepare it for testing
        run = tmt.base.Run(tree=self.tree, logger=self._logger, parent=self)
        run.prepare_for_try(self.tree)
        self._workdir = run.workdir
        self.environment = run.environment

        # Check tree, plans and tests, welcome summary
        self.check_tree()
        self.check_plans(run=run)
        self.check_tests()
        self.welcome()
        self.save()

        # Set the default verbosity level, handle options
        for plan in self.plans:
            self.handle_options(plan)
            self.action_verbose(plan)

        # Choose the initial action
        if self.opt("login"):
            action = Action.START_LOGIN
        elif self.opt("ask"):
            action = Action.START_ASK
        elif self.tests:
            action = Action.START_TEST
        else:
            action = Action.START_ASK

        # Loop over the actions
        try:
            while True:
                # Choose the verbose and debug level
                if action in [Action.VERBOSE, Action.DEBUG]:
                    getattr(self, f"prompt_{action.action}")()

                # Handle the individual actions
                for plan in self.plans:
                    plan.header()
                    getattr(self, f"action_{action.action}")(plan)

                # Finish for keep and quit
                if action in [Action.KEEP, Action.QUIT]:
                    break

                action = self.choose_action()

        # Make sure we clean up when interrupted
        except KeyboardInterrupt:
            for plan in self.plans:
                self.action_quit(plan)
