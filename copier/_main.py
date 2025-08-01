"""Main functions and classes, used to generate or update projects."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import field, replace
from filecmp import dircmp
from functools import cached_property, partial, wraps
from itertools import chain
from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import (
    Any,
    Callable,
    Literal,
    TypeVar,
    get_args,
    overload,
)
from unicodedata import normalize

from jinja2.loaders import FileSystemLoader
from pathspec import PathSpec
from plumbum import ProcessExecutionError, colors
from plumbum.machines import local
from pydantic import ConfigDict, PositiveInt
from pydantic.dataclasses import dataclass
from pydantic_core import to_jsonable_python
from questionary import confirm, unsafe_prompt

from ._jinja_ext import YieldEnvironment, YieldExtension
from ._subproject import Subproject
from ._template import Task, Template
from ._tools import (
    OS,
    Style,
    cast_to_bool,
    escape_git_path,
    normalize_git_path,
    printf,
    scantree,
    set_git_alternates,
)
from ._types import (
    MISSING,
    AnyByStrDict,
    AnyByStrMutableMapping,
    JSONSerializable,
    LazyDict,
    Operation,
    ParamSpec,
    Phase,
    RelativePath,
    StrOrPath,
    VcsRef,
)
from ._user_data import AnswersMap, Question, load_answersfile_data
from ._vcs import get_git
from .errors import (
    CopierAnswersInterrupt,
    ExtensionNotFoundError,
    InteractiveSessionError,
    TaskError,
    UnsafeTemplateError,
    UserMessageError,
    YieldTagInFileError,
)
from .settings import Settings

_T = TypeVar("_T")
_P = ParamSpec("_P")

_operation: ContextVar[Operation] = ContextVar("_operation")


def as_operation(value: Operation) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Decorator to set the current operation context, if not defined already.

    This value is used to template specific configuration options.
    """

    def _decorator(func: Callable[_P, _T]) -> Callable[_P, _T]:
        @wraps(func)
        def _wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            token = _operation.set(_operation.get(value))
            try:
                return func(*args, **kwargs)
            finally:
                _operation.reset(token)

        return _wrapper

    return _decorator


@dataclass(config=ConfigDict(extra="forbid"))
class Worker:
    """Copier process state manager.

    This class represents the state of a copier work, and contains methods to
    actually produce the desired work.

    To use it properly, use it as a context manager and fill all dataclass fields.

    Then, execute one of its main methods, which are prefixed with `run_`:

    -   [run_copy][copier.main.Worker.run_copy] to copy a subproject.
    -   [run_recopy][copier.main.Worker.run_recopy] to recopy a subproject.
    -   [run_update][copier.main.Worker.run_update] to update a subproject.

    Example:
        ```python
        with Worker(
            src_path="https://github.com/copier-org/autopretty.git", "output"
        ) as worker:
            worker.run_copy()
        ```

    Attributes:
        src_path:
            String that can be resolved to a template path, be it local or remote.

            See [copier.vcs.get_repo][].

            If it is `None`, then it means that you are
            [updating a project][updating-a-project], and the original
            `src_path` will be obtained from
            [the answers file][the-copier-answersyml-file].

        dst_path:
            Destination path where to render the subproject.

        answers_file:
            Indicates the path for [the answers file][the-copier-answersyml-file].

            The path must be relative to `dst_path`.

            If it is `None`, the default value will be obtained from
            [copier.template.Template.answers_relpath][].

        vcs_ref:
            Specify the VCS tag/commit to use in the template.

        data:
            Answers to the questionnaire defined in the template.

        exclude:
            User-chosen additional [file exclusion patterns][exclude].

        use_prereleases:
            Consider prereleases when detecting the *latest* one?

            See [use_prereleases][].

            Useless if specifying a [vcs_ref][].

        skip_if_exists:
            User-chosen additional [file skip patterns][skip_if_exists].

        cleanup_on_error:
            Delete `dst_path` if there's an error?

            See [cleanup_on_error][].

        defaults:
            When `True`, use default answers to questions, which might be null if not specified.

            See [defaults][].

        user_defaults:
            Specify user defaults that may override a template's defaults during question prompts.

        overwrite:
            When `True`, Overwrite files that already exist, without asking.

            See [overwrite][].

        pretend:
            When `True`, produce no real rendering.

            See [pretend][].

        quiet:
            When `True`, disable all output.

            See [quiet][].

        conflict:
            One of "inline" (default), "rej".

        context_lines:
            Lines of context to consider when solving conflicts in updates.

            With more lines, context resolution is more accurate, but it will
            also produce more conflicts if your subproject has evolved.

            With less lines, context resolution is less accurate, but it will
            respect better the evolution of your subproject.

        unsafe:
            When `True`, allow usage of unsafe templates.

            See [unsafe][]

        skip_answered:
            When `True`, skip questions that have already been answered.

        skip_tasks:
            When `True`, skip template tasks execution.
    """

    # NOTE: attributes are fully documented in [creating.md](../docs/creating.md)
    # make sure to update documentation upon any changes.

    src_path: str | None = None
    dst_path: Path = Path()
    answers_file: RelativePath | None = None
    vcs_ref: str | VcsRef | None = None
    data: AnyByStrDict = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings.from_file)
    exclude: Sequence[str] = ()
    use_prereleases: bool = False
    skip_if_exists: Sequence[str] = ()
    cleanup_on_error: bool = True
    defaults: bool = False
    user_defaults: AnyByStrDict = field(default_factory=dict)
    overwrite: bool = False
    pretend: bool = False
    quiet: bool = False
    conflict: Literal["inline", "rej"] = "inline"
    context_lines: PositiveInt = 3
    unsafe: bool = False
    skip_answered: bool = False
    skip_tasks: bool = False

    answers: AnswersMap = field(default_factory=AnswersMap, init=False)
    _cleanup_hooks: list[Callable[[], None]] = field(default_factory=list, init=False)

    def __enter__(self) -> Worker:
        """Allow using worker as a context manager."""
        return self

    @overload
    def __exit__(self, type: None, value: None, traceback: None) -> None: ...

    @overload
    def __exit__(
        self, type: type[BaseException], value: BaseException, traceback: TracebackType
    ) -> None: ...

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Clean up garbage files after worker usage ends."""
        if value is not None:
            # exception was raised from code inside context manager:
            # try to clean up, ignoring any exception, then re-raise
            with suppress(Exception):
                self._cleanup()
            raise value
        # otherwise clean up and let any exception bubble up
        self._cleanup()

    def _cleanup(self) -> None:
        """Execute all stored cleanup methods."""
        for method in self._cleanup_hooks:
            method()

    def _check_unsafe(self, mode: Operation) -> None:
        """Check whether a template uses unsafe features."""
        if self.unsafe or self.settings.is_trusted(self.template.url):
            return
        features: set[str] = set()
        if self.template.jinja_extensions:
            features.add("jinja_extensions")
        if self.template.tasks and not self.skip_tasks:
            features.add("tasks")
        if mode == "update" and self.subproject.template:
            if self.subproject.template.jinja_extensions:
                features.add("jinja_extensions")
            if self.subproject.template.tasks:
                features.add("tasks")
            for stage in get_args(Literal["before", "after"]):
                if self.template.migration_tasks(stage, self.subproject.template):
                    features.add("migrations")
                    break
        if features:
            raise UnsafeTemplateError(sorted(features))

    def _external_data(self) -> LazyDict[str, Any]:
        """Load external data lazily.

        Result keys are used for rendering, and values are the parsed contents
        of the YAML files specified in [external_data][].

        Files will only be parsed lazily on 1st access. This helps avoiding
        circular dependencies when the file name also comes from a variable.
        """

        def _render(path: str) -> str:
            with Phase.use(Phase.UNDEFINED):
                return self._render_string(path)

        # Given those values are lazily rendered on 1st access then cached
        # the phase value is irrelevant and could be misleading.
        # As a consequence it is explicitly set to "undefined".
        return LazyDict(
            {
                name: lambda path=path: load_answersfile_data(  # type: ignore[misc]
                    self.dst_path, _render(path), warn_on_missing=True
                )
                for name, path in self.template.external_data.items()
            }
        )

    def _print_message(self, message: str) -> None:
        if message and not self.quiet:
            print(self._render_string(message), file=sys.stderr)

    def _answers_to_remember(self) -> Mapping[str, Any]:
        """Get only answers that will be remembered in the copier answers file."""
        # All internal values must appear first
        answers: AnyByStrDict = {}
        commit = self.template.commit
        src = self.template.url
        for key, value in (("_commit", commit), ("_src_path", src)):
            if value is not None:
                answers[key] = value
        # Other data goes next
        answers.update(
            (str(k), v)
            for (k, v) in self.answers.combined.items()
            if not k.startswith("_")
            and k not in self.answers.hidden
            and k not in self.template.secret_questions
            and k in self.template.questions_data
            and isinstance(k, JSONSerializable)
            and isinstance(v, JSONSerializable)
        )
        return answers

    def _execute_tasks(self, tasks: Sequence[Task]) -> None:
        """Run the given tasks.

        Arguments:
            tasks: The list of tasks to run.
        """
        operation = _operation.get()
        for i, task in enumerate(tasks):
            extra_context = {f"_{k}": v for k, v in task.extra_vars.items()}
            extra_context["_copier_operation"] = operation

            if not cast_to_bool(self._render_value(task.condition, extra_context)):
                continue

            task_cmd = task.cmd
            if isinstance(task_cmd, str):
                task_cmd = self._render_string(task_cmd, extra_context)
                use_shell = True
            else:
                task_cmd = [
                    self._render_string(str(part), extra_context) for part in task_cmd
                ]
                use_shell = False

            if not self.quiet:
                print(
                    colors.info
                    | f" > Running task {i + 1} of {len(tasks)}: {task_cmd}",
                    file=sys.stderr,
                )
            if self.pretend:
                continue

            working_directory = (
                # We can't use _render_path here, as that function has special handling for files in the template
                self.subproject.local_abspath
                / Path(self._render_string(str(task.working_directory), extra_context))
            ).absolute()

            extra_env = {k[1:].upper(): str(v) for k, v in extra_context.items()}
            with local.cwd(working_directory), local.env(**extra_env):
                process = subprocess.run(task_cmd, shell=use_shell, env=local.env)
                if process.returncode:
                    raise TaskError.from_process(process)

    def _render_context(self) -> AnyByStrMutableMapping:
        """Produce render context for Jinja."""
        conf = LazyDict(
            {
                "src_path": lambda: self.template.local_abspath,
                "dst_path": lambda: self.dst_path,
                "answers_file": lambda: self.answers_relpath,
                "vcs_ref": lambda: self.resolved_vcs_ref,
                "vcs_ref_hash": lambda: self.template.commit_hash,
                "data": lambda: self.data,
                "settings": lambda: self.settings,
                "exclude": lambda: self.exclude,
                "use_prereleases": lambda: self.use_prereleases,
                "skip_if_exists": lambda: self.skip_if_exists,
                "cleanup_on_error": lambda: self.cleanup_on_error,
                "defaults": lambda: self.defaults,
                "user_defaults": lambda: self.user_defaults,
                "overwrite": lambda: self.overwrite,
                "pretend": lambda: self.pretend,
                "quiet": lambda: self.quiet,
                "conflict": lambda: self.conflict,
                "context_lines": lambda: self.context_lines,
                "unsafe": lambda: self.unsafe,
                "skip_answered": lambda: self.skip_answered,
                "skip_tasks": lambda: self.skip_tasks,
                "sep": lambda: os.sep,
                "os": lambda: OS,
            }
        )
        return dict(
            **self.answers.combined,
            _copier_answers=self._answers_to_remember(),
            _copier_conf=conf,
            _folder_name=self.subproject.local_abspath.name,
            _copier_python=sys.executable,
            _copier_phase=Phase.current(),
        )

    def _path_matcher(self, patterns: Iterable[str]) -> Callable[[Path], bool]:
        """Produce a function that matches against specified patterns."""
        # TODO Is normalization really needed?
        normalized_patterns = (normalize("NFD", pattern) for pattern in patterns)
        spec = PathSpec.from_lines("gitwildmatch", normalized_patterns)
        return spec.match_file

    def _solve_render_conflict(self, dst_relpath: Path) -> bool:
        """Properly solve render conflicts.

        It can ask the user if running in interactive mode.
        """
        assert not dst_relpath.is_absolute()
        printf(
            "conflict",
            dst_relpath,
            style=Style.DANGER,
            quiet=self.quiet,
            file_=sys.stderr,
        )
        if self.match_skip(dst_relpath):
            printf(
                "skip",
                dst_relpath,
                style=Style.OK,
                quiet=self.quiet,
                file_=sys.stderr,
            )
            return False
        if self.overwrite or dst_relpath == self.answers_relpath:
            printf(
                "overwrite",
                dst_relpath,
                style=Style.WARNING,
                quiet=self.quiet,
                file_=sys.stderr,
            )
            return True
        try:
            answer = confirm(f" Overwrite {dst_relpath}?", default=True).unsafe_ask()
        except EOFError as err:
            raise InteractiveSessionError("Consider using `--overwrite`") from err
        return bool(answer)

    def _render_allowed(
        self,
        dst_relpath: Path,
        is_dir: bool = False,
        is_symlink: bool = False,
        expected_contents: bytes | Path = b"",
    ) -> bool:
        """Determine if a file or directory can be rendered.

        Args:
            dst_relpath:
                Relative path to destination.
            is_dir:
                Indicate if the path must be treated as a directory or not.
            is_symlink:
                Indicate if the path must be treated as a symlink or not.
            expected_contents:
                Used to compare existing file contents with them. Allows to know if
                rendering is needed.
        """
        assert not dst_relpath.is_absolute()
        assert not expected_contents or not is_dir, "Dirs cannot have expected content"
        dst_abspath = Path(self.subproject.local_abspath, dst_relpath)
        previous_is_symlink = dst_abspath.is_symlink()
        try:
            previous_content: bytes | Path
            if previous_is_symlink:
                previous_content = dst_abspath.readlink()
            else:
                previous_content = dst_abspath.read_bytes()
        except FileNotFoundError:
            printf(
                "create",
                dst_relpath,
                style=Style.OK,
                quiet=self.quiet,
                file_=sys.stderr,
            )
            return True
        except PermissionError as error:
            # HACK https://bugs.python.org/issue43095
            if not (error.errno == 13 and platform.system() == "Windows"):
                raise
        except IsADirectoryError:
            assert is_dir
        if is_dir or (
            previous_content == expected_contents and previous_is_symlink == is_symlink
        ):
            printf(
                "identical",
                dst_relpath,
                style=Style.IGNORE,
                quiet=self.quiet,
                file_=sys.stderr,
            )
            return is_dir
        return self._solve_render_conflict(dst_relpath)

    def _ask(self) -> None:  # noqa: C901
        """Ask the questions of the questionnaire and record their answers."""
        self.answers = AnswersMap(
            user_defaults=self.user_defaults,
            init=self.data,
            last=self.subproject.last_answers,
            metadata=self.template.metadata,
            external=self._external_data(),
        )

        for var_name, details in self.template.questions_data.items():
            question = Question(
                answers=self.answers,
                context=self._render_context(),
                jinja_env=self.jinja_env,
                settings=self.settings,
                var_name=var_name,
                **details,
            )
            # Delete last answer if it cannot be parsed or validated, so a new
            # valid answer can be provided.
            if var_name in self.answers.last:
                try:
                    answer = question.parse_answer(self.answers.last[var_name])
                    question.validate_answer(answer)
                except Exception:
                    del self.answers.last[var_name]
            # Skip a question when the skip condition is met.
            if not question.get_when():
                # Omit its answer from the answers file.
                self.answers.hide(var_name)
                # Delete last answers to re-compute the answer from the default
                # value (if it exists).
                if var_name in self.answers.last:
                    del self.answers.last[var_name]
                # Skip immediately to the next question when it has no default
                # value.
                if question.default is MISSING:
                    continue
            if var_name in self.answers.init:
                # Try to parse and validate (if the question has a validator)
                # the answer value.
                answer = question.parse_answer(self.answers.init[var_name])
                question.validate_answer(answer)
                # At this point, the answer value is valid. Do not ask the
                # question again, but set answer as the user's answer instead.
                self.answers.user[var_name] = answer
                continue
            # Skip a question when the user already answered it.
            if self.skip_answered and var_name in self.answers.last:
                continue

            # Display TUI and ask user interactively only without --defaults
            try:
                if self.defaults:
                    new_answer = question.get_default()
                    if new_answer is MISSING:
                        raise ValueError(f'Question "{var_name}" is required')
                else:
                    try:
                        new_answer = unsafe_prompt(
                            [question.get_questionary_structure()],
                            answers={question.var_name: question.get_default()},
                        )[question.var_name]
                    except EOFError as err:
                        raise InteractiveSessionError(
                            "Use `--defaults` and/or `--data`/`--data-file`"
                        ) from err
            except KeyboardInterrupt as err:
                raise CopierAnswersInterrupt(
                    self.answers, question, self.template
                ) from err
            self.answers.user[var_name] = new_answer

        # Reload external data, which may depend on answers
        self.answers.external = self._external_data()

    @property
    def answers_relpath(self) -> Path:
        """Obtain the proper relative path for the answers file.

        It comes from:

        1. User choice.
        2. Template default.
        3. Copier default.
        """
        path = self.answers_file or self.template.answers_relpath
        template = self.jinja_env.from_string(str(path))
        # HACK: Override `_copier_conf.answers_file` in the render context to
        # avoid infinite recursion when accessing it in a Jinja context hook via
        # `copier-templates-extensions`.
        context = self._render_context()
        context["_copier_conf"]["answers_file"] = ""
        return Path(template.render(**context))

    @cached_property
    def all_exclusions(self) -> Sequence[str]:
        """Combine default, template and user-chosen exclusions."""
        return self.template.exclude + tuple(self.exclude)

    @cached_property
    def jinja_env(self) -> YieldEnvironment:
        """Return a pre-configured Jinja environment.

        Respects template settings.
        """
        paths = [str(self.template.local_abspath)]
        loader = FileSystemLoader(paths)
        default_extensions = [
            "jinja2_ansible_filters.AnsibleCoreFiltersExtension",
            YieldExtension,
        ]
        extensions = default_extensions + list(self.template.jinja_extensions)
        try:
            env = YieldEnvironment(
                loader=loader, extensions=extensions, **self.template.envops
            )
        except ModuleNotFoundError as error:
            raise ExtensionNotFoundError(
                f"Copier could not load some Jinja extensions:\n{error}\n"
                "Make sure to install these extensions alongside Copier itself.\n"
                "See the docs at https://copier.readthedocs.io/en/latest/configuring/#jinja_extensions"
            )
        # patch the `to_json` filter to support Pydantic dataclasses
        env.filters["to_json"] = partial(
            env.filters["to_json"],
            default=partial(
                to_jsonable_python,
                fallback=lambda v: dict(v) if isinstance(v, LazyDict) else v,
            ),
        )

        # Add a global function to join filesystem paths.
        separators = {
            "posix": "/",
            "windows": "\\",
            "native": os.path.sep,
        }

        def _pathjoin(
            *path: str, mode: Literal["posix", "windows", "native"] = "posix"
        ) -> str:
            return separators[mode].join(path)

        env.globals["pathjoin"] = _pathjoin
        return env

    @cached_property
    def match_exclude(self) -> Callable[[Path], bool]:
        """Get a callable to match paths against all exclusions."""
        # Include the current operation in the rendering context.
        # Note: This method is a cached property, it needs to be regenerated
        # when reusing an instance in different contexts.
        extra_context = {"_copier_operation": _operation.get()}
        return self._path_matcher(
            self._render_string(exclusion, extra_context=extra_context)
            for exclusion in self.all_exclusions
        )

    @cached_property
    def match_skip(self) -> Callable[[Path], bool]:
        """Get a callable to match paths against all skip-if-exists patterns."""
        return self._path_matcher(
            map(
                self._render_string,
                tuple(chain(self.skip_if_exists, self.template.skip_if_exists)),
            )
        )

    def _render_template(self) -> None:
        """Render the template in the subproject root."""
        follow_symlinks = not self.template.preserve_symlinks
        for src in scantree(str(self.template_copy_root), follow_symlinks):
            src_abspath = Path(src.path)
            src_relpath = Path(src_abspath).relative_to(self.template.local_abspath)
            dst_relpaths_ctxs = self._render_path(
                Path(src_abspath).relative_to(self.template_copy_root)
            )
            for dst_relpath, ctx in dst_relpaths_ctxs:
                if self.match_exclude(dst_relpath):
                    continue
                if src.is_symlink() and self.template.preserve_symlinks:
                    self._render_symlink(src_relpath, dst_relpath)
                elif src.is_dir(follow_symlinks=follow_symlinks):
                    self._render_folder(dst_relpath)
                else:
                    self._render_file(src_relpath, dst_relpath, extra_context=ctx or {})

    def _render_file(
        self,
        src_relpath: Path,
        dst_relpath: Path,
        extra_context: AnyByStrDict | None = None,
    ) -> None:
        """Render one file.

        Args:
            src_relpath:
                File to be rendered. It must be a path relative to the template
                root.
            dst_relpath:
                File to be created. It must be a path relative to the subproject
                root.
            extra_context:
                Additional variables to use for rendering the template.
        """
        # TODO Get from main.render_file()
        assert not src_relpath.is_absolute()
        assert not dst_relpath.is_absolute()
        src_abspath = self.template.local_abspath / src_relpath
        if src_relpath.name.endswith(self.template.templates_suffix):
            try:
                tpl = self.jinja_env.get_template(src_relpath.as_posix())
            except UnicodeDecodeError:
                if self.template.templates_suffix:
                    # suffix is not empty, re-raise
                    raise
                # suffix is empty, fallback to copy
                new_content = src_abspath.read_bytes()
            else:
                new_content = tpl.render(
                    **self._render_context(), **(extra_context or {})
                ).encode()
                if self.jinja_env.yield_name:
                    raise YieldTagInFileError(
                        f"File {src_relpath} contains a yield tag, but it is not allowed."
                    )
        else:
            new_content = src_abspath.read_bytes()
        dst_abspath = self.subproject.local_abspath / dst_relpath
        src_mode = src_abspath.stat().st_mode
        if not self._render_allowed(dst_relpath, expected_contents=new_content):
            return
        if not self.pretend:
            dst_abspath.parent.mkdir(parents=True, exist_ok=True)
            if dst_abspath.is_symlink():
                # Writing to a symlink just writes to its target, so if we want to
                # replace a symlink with a file we have to unlink it first
                dst_abspath.unlink()
            dst_abspath.write_bytes(new_content)
            dst_abspath.chmod(src_mode)

    def _render_symlink(self, src_relpath: Path, dst_relpath: Path) -> None:
        """Render one symlink.

        Args:
            src_relpath:
                Symlink to be rendered. It must be a path relative to the
                template root.
            dst_relpath:
                Symlink to be created. It must be a path relative to the
                subproject root.
        """
        assert not src_relpath.is_absolute()
        assert not dst_relpath.is_absolute()
        if dst_relpath is None or self.match_exclude(dst_relpath):
            return

        src_abspath = self.template.local_abspath / src_relpath
        src_target = src_abspath.readlink()
        if src_abspath.name.endswith(self.template.templates_suffix):
            dst_target = Path(self._render_string(str(src_target)))
        else:
            dst_target = src_target

        if not self._render_allowed(
            dst_relpath,
            expected_contents=dst_target,
            is_symlink=True,
        ):
            return

        if not self.pretend:
            dst_abspath = self.subproject.local_abspath / dst_relpath
            # symlink_to doesn't overwrite existing files, so delete it first
            if dst_abspath.is_symlink() or dst_abspath.exists():
                dst_abspath.unlink()
            dst_abspath.parent.mkdir(parents=True, exist_ok=True)
            dst_abspath.symlink_to(dst_target)
            if sys.platform == "darwin":
                # Only macOS supports permissions on symlinks.
                # Other platforms just copy the permission of the target
                src_mode = src_abspath.lstat().st_mode
                dst_abspath.lchmod(src_mode)

    def _render_folder(self, dst_relpath: Path) -> None:
        """Create one folder (without content).

        Args:
            dst_relpath:
                Folder to be created. It must be a path relative to the
                subproject root.
        """
        assert not dst_relpath.is_absolute()
        if not self.pretend and self._render_allowed(dst_relpath, is_dir=True):
            dst_abspath = self.subproject.local_abspath / dst_relpath
            dst_abspath.mkdir(parents=True, exist_ok=True)

    def _adjust_rendered_part(self, rendered_part: str) -> str:
        """Adjust the rendered part if necessary.

        If `{{ _copier_conf.answers_file }}` becomes the full path,
        restore part to be just the end leaf.

        Args:
            rendered_part:
                The rendered part of the path to adjust.

        """
        if str(self.answers_relpath) == rendered_part:
            return Path(rendered_part).name
        return rendered_part

    def _render_parts(
        self,
        parts: tuple[str, ...],
        rendered_parts: tuple[str, ...] | None = None,
        extra_context: AnyByStrDict | None = None,
        is_template: bool = False,
    ) -> Iterable[tuple[Path, AnyByStrDict | None]]:
        """Render a set of parts into path and context pairs.

        If a yield tag is found in a part, it will recursively yield multiple path and context pairs.
        """
        if rendered_parts is None:
            rendered_parts = tuple()

        if not parts:
            rendered_path = Path(*rendered_parts)

            templated_sibling = (
                self.template.local_abspath
                / f"{rendered_path}{self.template.templates_suffix}"
            )
            if is_template or not templated_sibling.exists():
                yield rendered_path, extra_context

            return

        part = parts[0]
        parts = parts[1:]

        if not extra_context:
            extra_context = {}

        # If the `part` has a yield tag, `self.jinja_env` will be set with the yield name and iterable
        rendered_part = self._render_string(part, extra_context=extra_context)

        yield_name = self.jinja_env.yield_name
        if yield_name:
            for value in self.jinja_env.yield_iterable or ():
                new_context = {**extra_context, yield_name: value}
                rendered_part = self._render_string(part, extra_context=new_context)
                rendered_part = self._adjust_rendered_part(rendered_part)

                # Skip if any part is rendered as an empty string
                if not rendered_part:
                    continue

                yield from self._render_parts(
                    parts, rendered_parts + (rendered_part,), new_context, is_template
                )

            return

        # Skip if any part is rendered as an empty string
        if not rendered_part:
            return

        rendered_part = self._adjust_rendered_part(rendered_part)

        yield from self._render_parts(
            parts, rendered_parts + (rendered_part,), extra_context, is_template
        )

    def _render_path(self, relpath: Path) -> Iterable[tuple[Path, AnyByStrDict | None]]:
        """Render one relative path into multiple path and context pairs.

        Args:
            relpath:
                The relative path to be rendered. Obviously, it can be templated.
        """
        is_template = relpath.name.endswith(self.template.templates_suffix)
        templated_sibling = (
            self.template_copy_root / f"{relpath}{self.template.templates_suffix}"
        )
        # With an empty suffix, the templated sibling always exists.
        if templated_sibling.exists() and self.template.templates_suffix:
            return
        if self.template.templates_suffix and is_template:
            relpath = relpath.with_suffix("")

        yield from self._render_parts(relpath.parts, is_template=is_template)

    def _render_string(
        self, string: str, extra_context: AnyByStrDict | None = None
    ) -> str:
        """Render one templated string.

        Args:
            string:
                The template source string.

            extra_context:
                Additional variables to use for rendering the template.
        """
        tpl = self.jinja_env.from_string(string)
        return tpl.render(**self._render_context(), **(extra_context or {}))

    def _render_value(
        self, value: _T, extra_context: AnyByStrDict | None = None
    ) -> str | _T:
        """Render a value, which may or may not be a templated string.

        Args:
            value:
                The value to render.

            extra_context:
                Additional variables to use for rendering the template.
        """
        try:
            return self._render_string(value, extra_context=extra_context)  # type: ignore[arg-type]
        except TypeError:
            return value

    @cached_property
    def resolved_vcs_ref(self) -> str | None:
        """Get the resolved VCS reference to use.

        This is either `vcs_ref` or the subproject template ref
        if `vcs_ref` is `VcsRef.CURRENT`.
        """
        if self.vcs_ref is VcsRef.CURRENT:
            if self.subproject.template is None:
                raise TypeError("Template not found")
            return self.subproject.template.ref
        return self.vcs_ref

    @cached_property
    def subproject(self) -> Subproject:
        """Get related subproject."""
        result = Subproject(
            local_abspath=self.dst_path.absolute(),
            answers_relpath=self.answers_file or Path(".copier-answers.yml"),
        )
        self._cleanup_hooks.append(result._cleanup)
        return result

    @cached_property
    def template(self) -> Template:
        url = self.src_path
        if not url:
            if self.subproject.template is None:
                raise TypeError("Template not found")
            url = str(self.subproject.template.url)
        ref = self.resolved_vcs_ref
        result = Template(url=url, ref=ref, use_prereleases=self.use_prereleases)
        self._cleanup_hooks.append(result._cleanup)
        return result

    @cached_property
    def template_copy_root(self) -> Path:
        """Absolute path from where to start copying.

        It points to the cloned template local abspath + the rendered subdir, if any.
        """
        subdir = self._render_string(self.template.subdirectory) or ""
        return self.template.local_abspath / subdir

    # Main operations
    @as_operation("copy")
    def run_copy(self) -> None:
        """Generate a subproject from zero, ignoring what was in the folder.

        If `dst_path` was missing, it will be
        created. Otherwise, `src_path` be rendered
        directly into it, without worrying about evolving what was there
        already.

        See [generating a project][generating-a-project].
        """
        with suppress(AttributeError):
            # We might have switched operation context, ensure the cached property
            # is regenerated to re-render templates.
            del self.match_exclude

        self._check_unsafe("copy")
        self._print_message(self.template.message_before_copy)
        with Phase.use(Phase.PROMPT):
            self._ask()
        was_existing = self.subproject.local_abspath.exists()
        try:
            if not self.quiet:
                # TODO Unify printing tools
                print(
                    f"\nCopying from template version {self.template.version}",
                    file=sys.stderr,
                )
            with Phase.use(Phase.RENDER):
                self._render_template()
            if not self.quiet:
                # TODO Unify printing tools
                print("")  # padding space
            if not self.skip_tasks:
                with Phase.use(Phase.TASKS):
                    self._execute_tasks(self.template.tasks)
        except Exception:
            if not was_existing and self.cleanup_on_error:
                rmtree(self.subproject.local_abspath)
            raise
        self._print_message(self.template.message_after_copy)
        if not self.quiet:
            # TODO Unify printing tools
            print("")  # padding space

    @as_operation("copy")
    def run_recopy(self) -> None:
        """Update a subproject, keeping answers but discarding evolution."""
        if self.subproject.template is None:
            raise UserMessageError(
                "Cannot recopy because cannot obtain old template references "
                f"from `{self.subproject.answers_relpath}`."
            )
        with replace(self, src_path=self.subproject.template.url) as new_worker:
            new_worker.run_copy()

    def _print_template_update_info(self, subproject_template: Template) -> None:
        # TODO Unify printing tools
        if not self.quiet:
            if subproject_template.version == self.template.version:
                message = f"Keeping template version {self.template.version}"
            else:
                message = f"Updating to template version {self.template.version}"
            print(message, file=sys.stderr)

    @as_operation("update")
    def run_update(self) -> None:
        """Update a subproject that was already generated.

        See [updating a project][updating-a-project].
        """
        self._check_unsafe("update")
        # Check all you need is there
        if self.subproject.vcs != "git":
            raise UserMessageError(
                "Updating is only supported in git-tracked subprojects."
            )
        if self.subproject.is_dirty():
            raise UserMessageError(
                "Destination repository is dirty; cannot continue. "
                "Please commit or stash your local changes and retry."
            )
        if self.subproject.template is None or self.subproject.template.ref is None:
            raise UserMessageError(
                "Cannot update because cannot obtain old template references "
                f"from `{self.subproject.answers_relpath}`."
            )
        if self.template.commit is None:
            raise UserMessageError(
                "Updating is only supported in git-tracked templates."
            )
        if not self.subproject.template.version:
            raise UserMessageError(
                "Cannot update: version from last update not detected."
            )
        if not self.template.version:
            raise UserMessageError("Cannot update: version from template not detected.")
        if self.subproject.template.version > self.template.version:
            raise UserMessageError(
                f"You are downgrading from {self.subproject.template.version} to {self.template.version}. "
                "Downgrades are not supported."
            )
        if not self.overwrite:
            # Only git-tracked subprojects can be updated, so the user can
            # review the diff before committing; so we can safely avoid
            # asking for confirmation
            raise UserMessageError("Enable overwrite to update a subproject.")
        self._print_message(self.template.message_before_update)
        self._print_template_update_info(self.subproject.template)
        with suppress(AttributeError):
            # We might have switched operation context, ensure the cached property
            # is regenerated to re-render templates.
            del self.match_exclude

        self._apply_update()
        self._print_message(self.template.message_after_update)

    def _apply_update(self) -> None:  # noqa: C901
        git = get_git()
        subproject_top = Path(
            git(
                "-C",
                self.subproject.local_abspath,
                "rev-parse",
                "--show-toplevel",
            ).strip()
        )
        subproject_subdir = self.subproject.local_abspath.relative_to(subproject_top)

        with (
            TemporaryDirectory(
                prefix=f"{__name__}.old_copy.",
            ) as old_copy,
            TemporaryDirectory(
                prefix=f"{__name__}.new_copy.",
            ) as new_copy,
        ):
            # Copy old template into a temporary destination
            with replace(
                self,
                dst_path=old_copy / subproject_subdir,
                data=self.subproject.last_answers,
                defaults=True,
                quiet=True,
                src_path=self.subproject.template.url,  # type: ignore[union-attr]
                vcs_ref=self.subproject.template.commit,  # type: ignore[union-attr]
            ) as old_worker:
                # HACK: Remove the validator from a secret question when replaying
                # a fresh copy using the old template because only the default value
                # is available which may not pass validation.
                assert old_worker.template is not self.template
                for q in old_worker.template.questions_data.values():
                    if q.get("secret", False):
                        q.pop("validator", None)
                old_worker.run_copy()
            # Run pre-migration tasks
            with Phase.use(Phase.MIGRATE):
                self._execute_tasks(
                    self.template.migration_tasks("before", self.subproject.template)  # type: ignore[arg-type]
                )
            # Create a Git tree object from the current (possibly dirty) index
            # and keep the object reference.
            with local.cwd(subproject_top):
                subproject_head = git("write-tree").strip()
            with local.cwd(old_copy):
                self._git_initialize_repo()
                # Configure borrowing Git objects from the real destination.
                set_git_alternates(subproject_top)
                # Save a list of files that were intentionally removed in the generated
                # project to avoid recreating them during the update.
                # Files listed in `skip_if_exists` should only be skipped if they exist.
                # They should even be recreated if deleted intentionally.
                files_removed = git(
                    "diff-tree",
                    "-r",
                    "--diff-filter=D",
                    "--name-only",
                    "HEAD",
                    subproject_head,
                ).splitlines()
                exclude_plus_removed = list(
                    set(self.exclude).union(
                        map(
                            escape_git_path,
                            map(
                                normalize_git_path,
                                (
                                    path
                                    for path in files_removed
                                    if not self.match_skip(path)
                                ),
                            ),
                        )
                    )
                )
            # Clear last answers cache to load possible answers migration, if skip_answered flag is not set
            if self.skip_answered is False:
                self.answers = AnswersMap(external=self._external_data())
                with suppress(AttributeError):
                    del self.subproject.last_answers
            # Do a normal update in final destination
            with replace(
                self,
                # Don't regenerate intentionally deleted paths
                exclude=exclude_plus_removed,
                # Files can change due to the historical diff, and those
                # changes are not detected in this process, so it's better to
                # say nothing than lie.
                # TODO
                quiet=True,
            ) as current_worker:
                current_worker.run_copy()
                self.answers = current_worker.answers
                self.answers.external = self._external_data()
            # Render with the same answers in an empty dir to avoid pollution
            with replace(
                self,
                dst_path=new_copy / subproject_subdir,
                data={
                    k: v
                    for k, v in self.answers.combined.items()
                    if k not in self.answers.hidden
                },
                defaults=True,
                quiet=True,
                src_path=self.subproject.template.url,  # type: ignore[union-attr]
                exclude=exclude_plus_removed,
                vcs_ref=self.resolved_vcs_ref,
            ) as new_worker:
                new_worker.run_copy()
            with local.cwd(new_copy):
                self._git_initialize_repo()
                new_copy_head = git("rev-parse", "HEAD").strip()
            # Extract diff between temporary destination and real destination
            # with some special handling of newly added files in both the project
            # and the template.
            with local.cwd(old_copy):
                # Configure borrowing Git objects from the real destination and
                # temporary destination of the new template.
                set_git_alternates(subproject_top, Path(new_copy))
                # Create an empty file in the temporary destination when the
                # same file was added in *both* the project and the temporary
                # destination of the new template. With this minor change, the
                # diff between the temporary destination and the real
                # destination for such files will use the "update file mode"
                # instead of the "new file mode" which avoids deleting the file
                # content previously added in the project.
                diff_added_cmd = git[
                    "diff-tree", "-r", "--diff-filter=A", "--name-only"
                ]
                for filename in (
                    set(diff_added_cmd("HEAD", subproject_head).splitlines())
                ) & set(diff_added_cmd("HEAD", new_copy_head).splitlines()):
                    f = Path(filename)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.touch((subproject_top / filename).stat().st_mode)
                    git("add", "--force", filename)
                self._git_commit("add new empty files")
                # Extract diff between temporary destination and real
                # destination
                diff_cmd = git[
                    "diff-tree",
                    f"--unified={self.context_lines}",
                    "HEAD",
                    subproject_head,
                ]
                try:
                    diff = diff_cmd("--inter-hunk-context=-1")
                except ProcessExecutionError:
                    print(
                        colors.warn
                        | "Make sure Git >= 2.24 is installed to improve updates.",
                        file=sys.stderr,
                    )
                    diff = diff_cmd("--inter-hunk-context=0")
            compared = dircmp(old_copy, new_copy)
            # Try to apply cached diff into final destination
            with local.cwd(subproject_top):
                apply_cmd = git["apply", "--reject", "--exclude", self.answers_relpath]
                ignored_files = git["status", "--ignored", "--porcelain"]()
                # returns "!! file1\n !! file2\n"
                # extra_exclude will contain: ["file1", file2"]
                extra_exclude = [
                    filename.split("!! ").pop()
                    for filename in ignored_files.splitlines()
                ]
                for skip_pattern in chain(
                    self.skip_if_exists, self.template.skip_if_exists, extra_exclude
                ):
                    apply_cmd = apply_cmd["--exclude", skip_pattern]
                (apply_cmd << diff)(retcode=None)
                if self.conflict == "inline":
                    conflicted = []
                    old_path = Path(old_copy)
                    new_path = Path(new_copy)
                    status = git("status", "--porcelain").strip().splitlines()
                    for line in status:
                        # Filter merge rejections (part 1/2)
                        if not line.startswith("?? "):
                            continue
                        # Remove "?? " prefix
                        fname = line[3:]
                        # Normalize name
                        fname = normalize_git_path(fname)
                        # Filter merge rejections (part 2/2)
                        if not fname.endswith(".rej"):
                            continue
                        # Remove ".rej" suffix
                        fname = fname[:-4]
                        # Undo possible non-rejected chunks
                        git("checkout", "--", fname)
                        # 3-way-merge the file directly
                        git(
                            "merge-file",
                            "-L",
                            "before updating",
                            "-L",
                            "last update",
                            "-L",
                            "after updating",
                            fname,
                            old_path / fname,
                            new_path / fname,
                            retcode=None,
                        )
                        # Remove rejection witness
                        Path(f"{fname}.rej").unlink()
                        # The 3-way merge might have resolved conflicts automatically,
                        # so we need to check if the file contains conflict markers
                        # before storing the file name for marking it as unmerged after the loop.
                        with Path(fname).open("rb") as conflicts_candidate:
                            if any(
                                line.rstrip()
                                in {
                                    b"<<<<<<< before updating",
                                    b">>>>>>> after updating",
                                }
                                for line in conflicts_candidate
                            ):
                                conflicted.append(fname)
                    # We ran `git merge-file` outside of a regular merge operation,
                    # which means no merge conflict is recorded in the index.
                    # Only the usual stage 0 is recorded, with the hash of the current version.
                    # We therefore update the index with the missing stages:
                    # 1 = current (before updating), 2 = base (last update), 3 = other (after updating).
                    # See this SO post: https://stackoverflow.com/questions/79309642/
                    # and Git docs: https://git-scm.com/docs/git-update-index#_using_index_info.
                    if conflicted:
                        input_lines = []
                        for line in (
                            git("ls-files", "--stage", *conflicted).strip().splitlines()
                        ):
                            perms_sha_mode, path = line.split("\t")
                            perms, sha, _ = perms_sha_mode.split()
                            input_lines.append(f"0 {'0' * 40}\t{path}")
                            input_lines.append(f"{perms} {sha} 2\t{path}")
                            with suppress(ProcessExecutionError):
                                # The following command will fail
                                # if the file did not exist in the previous version.
                                old_sha = git(
                                    "hash-object",
                                    "-w",
                                    old_path / normalize_git_path(path),
                                ).strip()
                                input_lines.append(f"{perms} {old_sha} 1\t{path}")
                            with suppress(ProcessExecutionError):
                                # The following command will fail
                                # if the file was deleted in the latest version.
                                new_sha = git(
                                    "hash-object",
                                    "-w",
                                    new_path / normalize_git_path(path),
                                ).strip()
                                input_lines.append(f"{perms} {new_sha} 3\t{path}")
                        (
                            git["update-index", "--index-info"]
                            << "\n".join(input_lines)
                        )()
            # Trigger recursive removal of deleted files in last template version
            _remove_old_files(subproject_top, compared)

        # Run post-migration tasks
        with Phase.use(Phase.MIGRATE):
            self._execute_tasks(
                self.template.migration_tasks("after", self.subproject.template)  # type: ignore[arg-type]
            )

    def _git_initialize_repo(self) -> None:
        """Initialize a git repository in the current directory."""
        git = get_git()
        git("init", retcode=None)
        git("add", ".")
        self._git_commit()

    def _git_commit(self, message: str = "dumb commit") -> None:
        git = get_git()
        # 1st commit could fail if any pre-commit hook reformats code
        # 2nd commit uses --no-verify to disable pre-commit-like checks
        git(
            "commit",
            "--allow-empty",
            "-am",
            f"{message} 1",
            "--no-gpg-sign",
            retcode=None,
        )
        git(
            "commit",
            "--allow-empty",
            "-am",
            f"{message} 2",
            "--no-gpg-sign",
            "--no-verify",
        )


def run_copy(
    src_path: str,
    dst_path: StrOrPath = ".",
    data: AnyByStrDict | None = None,
    **kwargs: Any,
) -> Worker:
    """Copy a template to a destination, from zero.

    This is a shortcut for [run_copy][copier.main.Worker.run_copy].

    See [Worker][copier.main.Worker] fields to understand this function's args.
    """
    if data is not None:
        kwargs["data"] = data
    with Worker(src_path=src_path, dst_path=Path(dst_path), **kwargs) as worker:
        worker.run_copy()
    return worker


def run_recopy(
    dst_path: StrOrPath = ".", data: AnyByStrDict | None = None, **kwargs: Any
) -> Worker:
    """Update a subproject from its template, discarding subproject evolution.

    This is a shortcut for [run_recopy][copier.main.Worker.run_recopy].

    See [Worker][copier.main.Worker] fields to understand this function's args.
    """
    if data is not None:
        kwargs["data"] = data
    with Worker(dst_path=Path(dst_path), **kwargs) as worker:
        worker.run_recopy()
    return worker


def run_update(
    dst_path: StrOrPath = ".",
    data: AnyByStrDict | None = None,
    **kwargs: Any,
) -> Worker:
    """Update a subproject, from its template.

    This is a shortcut for [run_update][copier.main.Worker.run_update].

    See [Worker][copier.main.Worker] fields to understand this function's args.
    """
    if data is not None:
        kwargs["data"] = data
    with Worker(dst_path=Path(dst_path), **kwargs) as worker:
        worker.run_update()
    return worker


def _remove_old_files(prefix: Path, cmp: dircmp[str], rm_common: bool = False) -> None:
    """Remove files and directories only found in "old" template.

    This is an internal helper method used to process a comparison of 2
    directories, where the left one is considered the "old" one, and the
    right one is the "new" one.

    Then, it will recursively try to remove anything that is only in the old
    directory.

    Args:
        prefix:
            Where we start removing. It can be different from the directories
            being compared.
        cmp:
            The comparison result.
        rm_common:
            Should we remove common files and directories?
    """
    # Gather files and dirs to remove
    to_rm = []
    subdirs = {}
    with suppress(NotADirectoryError, FileNotFoundError):
        to_rm = cmp.left_only
        if rm_common:
            to_rm += cmp.common_files + cmp.common_dirs
        subdirs = cmp.subdirs
    # Remove files found only in old template copy
    for name in to_rm:
        target = prefix / name
        if target.is_file():
            target.unlink()
        else:
            # Recurse in dirs totally removed in latest template
            _remove_old_files(target, dircmp(Path(cmp.left, name), target), True)
            # Remove subdir if it ends empty
            with suppress(OSError):
                target.rmdir()  # Raises if dir not empty
    # Recurse
    for key, value in subdirs.items():
        subdir = prefix / key
        _remove_old_files(subdir, value)
        # Remove subdir if it ends empty
        with suppress(OSError):
            subdir.rmdir()  # Raises if dir not empty
