#!/usr/bin/env python

from dataclasses import fields
import json
import os
import sys
import git
import litellm
from pathlib import Path
from prompt_toolkit.enums import EditingMode

from aider import urls
from aider import models
from aider.analytics import Analytics
from aider.coders import Coder
from aider.coders.base_coder import UnknownEditFormat
from aider.commands import Commands, SwitchCoder
from aider.format_settings import format_settings, scrub_sensitive_info
from aider.history import ChatSummary
from aider.main import check_and_load_imports, check_config_files_for_yes, check_gitignore, get_git_root, guessed_wrong_repo, is_first_run_of_new_version, load_dotenv_files, parse_lint_cmds, register_litellm_models, register_models, sanity_check_repo, setup_git
from aider.models import ModelSettings
from aider.io import InputOutput
from aider.args import get_parser
from aider.repo import GitRepo
from aider.report import report_uncaught_exceptions

def main(argv=None, input=None, output=None, force_git_root=None, return_coder=False):
    report_uncaught_exceptions()

    if argv is None:
        argv = sys.argv[1:]

    if force_git_root:
        git_root = force_git_root
    else:
        git_root = get_git_root()

    conf_fname = Path(".aider.conf.yml")

    default_config_files = []
    try:
        default_config_files += [conf_fname.resolve()]  # CWD
    except OSError:
        pass

    if git_root:
        git_conf = Path(git_root) / conf_fname  # git root
        if git_conf not in default_config_files:
            default_config_files.append(git_conf)
    default_config_files.append(Path.home() / conf_fname)  # homedir
    default_config_files = list(map(str, default_config_files))

    parser = get_parser(default_config_files, git_root)
    try:
        args, unknown = parser.parse_known_args(argv)
    except AttributeError as e:
        if all(word in str(e) for word in ["bool", "object", "has", "no", "attribute", "strip"]):
            if check_config_files_for_yes(default_config_files):
                return 1
        raise e

    if args.verbose:
        print("Config files search order, if no --config:")
        for file in default_config_files:
            exists = "(exists)" if Path(file).exists() else ""
            print(f"  - {file} {exists}")

    default_config_files.reverse()

    parser = get_parser(default_config_files, git_root)

    args, unknown = parser.parse_known_args(argv)

    # Load the .env file specified in the arguments
    loaded_dotenvs = load_dotenv_files(git_root, args.env_file, args.encoding)

    # Parse again to include any arguments that might have been defined in .env
    args = parser.parse_args(argv)

    if args.analytics_disable:
        analytics = Analytics(permanently_disable=True)
        print("Analytics have been permanently disabled.")

    if not args.verify_ssl:
        import httpx

        os.environ["SSL_VERIFY"] = ""
        litellm._load_litellm()
        litellm._lazy_module.client_session = httpx.Client(verify=False)
        litellm._lazy_module.aclient_session = httpx.AsyncClient(verify=False)

    if args.timeout:
        litellm._load_litellm()
        litellm._lazy_module.request_timeout = args.timeout

    if args.dark_mode:
        args.user_input_color = "#32FF32"
        args.tool_error_color = "#FF3333"
        args.tool_warning_color = "#FFFF00"
        args.assistant_output_color = "#00FFFF"
        args.code_theme = "monokai"

    if args.light_mode:
        args.user_input_color = "green"
        args.tool_error_color = "red"
        args.tool_warning_color = "#FFA500"
        args.assistant_output_color = "blue"
        args.code_theme = "default"

    if return_coder and args.yes_always is None:
        args.yes_always = True

    editing_mode = EditingMode.VI if args.vim else EditingMode.EMACS

    def get_io(pretty):
        return InputOutput(
            pretty,
            args.yes_always,
            args.input_history_file,
            args.chat_history_file,
            input=input,
            output=output,
            user_input_color=args.user_input_color,
            tool_output_color=args.tool_output_color,
            tool_warning_color=args.tool_warning_color,
            tool_error_color=args.tool_error_color,
            completion_menu_color=args.completion_menu_color,
            completion_menu_bg_color=args.completion_menu_bg_color,
            completion_menu_current_color=args.completion_menu_current_color,
            completion_menu_current_bg_color=args.completion_menu_current_bg_color,
            assistant_output_color=args.assistant_output_color,
            code_theme=args.code_theme,
            dry_run=args.dry_run,
            encoding=args.encoding,
            llm_history_file=args.llm_history_file,
            editingmode=editing_mode,
            fancy_input=args.fancy_input,
            multiline_mode=args.multiline,
        )

    io = get_io(args.pretty)
    try:
        io.rule()
    except UnicodeEncodeError as err:
        if not io.pretty:
            raise err
        io = get_io(False)
        io.tool_warning("Terminal does not support pretty output (UnicodeDecodeError)")

    # Process any environment variables set via --set-env
    if args.set_env:
        for env_setting in args.set_env:
            try:
                name, value = env_setting.split("=", 1)
                os.environ[name.strip()] = value.strip()
            except ValueError:
                io.tool_error(f"Invalid --set-env format: {env_setting}")
                io.tool_output("Format should be: ENV_VAR_NAME=value")
                return 1

    # Process any API keys set via --api-key
    if args.api_key:
        for api_setting in args.api_key:
            try:
                provider, key = api_setting.split("=", 1)
                env_var = f"{provider.strip().upper()}_API_KEY"
                os.environ[env_var] = key.strip()
            except ValueError:
                io.tool_error(f"Invalid --api-key format: {api_setting}")
                io.tool_output("Format should be: provider=key")
                return 1

    if args.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = args.anthropic_api_key

    if args.openai_api_key:
        os.environ["OPENAI_API_KEY"] = args.openai_api_key
    if args.openai_api_base:
        os.environ["OPENAI_API_BASE"] = args.openai_api_base
    if args.openai_api_version:
        io.tool_warning(
            "--openai-api-version is deprecated, use --set-env OPENAI_API_VERSION=<value>"
        )
        os.environ["OPENAI_API_VERSION"] = args.openai_api_version
    if args.openai_api_type:
        io.tool_warning("--openai-api-type is deprecated, use --set-env OPENAI_API_TYPE=<value>")
        os.environ["OPENAI_API_TYPE"] = args.openai_api_type
    if args.openai_organization_id:
        io.tool_warning(
            "--openai-organization-id is deprecated, use --set-env OPENAI_ORGANIZATION=<value>"
        )
        os.environ["OPENAI_ORGANIZATION"] = args.openai_organization_id

    analytics = Analytics(logfile=args.analytics_log, permanently_disable=args.analytics_disable)
    if args.analytics is not False:
        if analytics.need_to_ask(args.analytics):
            io.tool_output(
                "Aider respects your privacy and never collects your code, chat messages, keys or"
                " personal info."
            )
            io.tool_output(f"For more info: {urls.analytics}")
            disable = not io.confirm_ask(
                "Allow collection of anonymous analytics to help improve aider?"
            )

            analytics.asked_opt_in = True
            if disable:
                analytics.disable(permanently=True)
                io.tool_output("Analytics have been permanently disabled.")

            analytics.save_data()
            io.tool_output()

        # This is a no-op if the user has opted out
        analytics.enable()

    analytics.event("launched")

    if args.verbose:
        for fname in loaded_dotenvs:
            io.tool_output(f"Loaded {fname}")

    all_files = args.files + (args.file or [])
    fnames = [str(Path(fn).resolve()) for fn in all_files]
    read_only_fnames = []
    for fn in args.read or []:
        path = Path(fn).expanduser().resolve()
        if path.is_dir():
            read_only_fnames.extend(str(f) for f in path.rglob("*") if f.is_file())
        else:
            read_only_fnames.append(str(path))

    if len(all_files) > 1:
        good = True
        for fname in all_files:
            if Path(fname).is_dir():
                io.tool_error(f"{fname} is a directory, not provided alone.")
                good = False
        if not good:
            io.tool_output(
                "Provide either a single directory of a git repo, or a list of one or more files."
            )
            analytics.event("exit", reason="Invalid directory input")
            return 1

    git_dname = None
    if len(all_files) == 1:
        if Path(all_files[0]).is_dir():
            if args.git:
                git_dname = str(Path(all_files[0]).resolve())
                fnames = []
            else:
                io.tool_error(f"{all_files[0]} is a directory, but --no-git selected.")
                analytics.event("exit", reason="Directory with --no-git")
                return 1

    # We can't know the git repo for sure until after parsing the args.
    # If we guessed wrong, reparse because that changes things like
    # the location of the config.yml and history files.
    if args.git and not force_git_root:
        right_repo_root = guessed_wrong_repo(io, git_root, fnames, git_dname)
        if right_repo_root:
            analytics.event("exit", reason="Recursing with correct repo")
            return main(argv, input, output, right_repo_root, return_coder=return_coder)

    if args.list_models:
        models.print_matching_models(io, args.list_models)
        analytics.event("exit", reason="Listed models")
        return 0

    if args.git:
        git_root = setup_git(git_root, io)
        if args.gitignore:
            check_gitignore(git_root, io)

    if args.verbose:
        show = format_settings(parser, args)
        io.tool_output(show)

    cmd_line = " ".join(sys.argv)
    cmd_line = scrub_sensitive_info(args, cmd_line)
    io.tool_output(cmd_line, log_only=True)

    is_first_run = is_first_run_of_new_version(io, verbose=args.verbose)
    check_and_load_imports(io, is_first_run, verbose=args.verbose)

    register_models(git_root, args.model_settings_file, io, verbose=args.verbose)
    register_litellm_models(git_root, args.model_metadata_file, io, verbose=args.verbose)

    # Process any command line aliases
    if args.alias:
        for alias_def in args.alias:
            # Split on first colon only
            parts = alias_def.split(":", 1)
            if len(parts) != 2:
                io.tool_error(f"Invalid alias format: {alias_def}")
                io.tool_output("Format should be: alias:model-name")
                analytics.event("exit", reason="Invalid alias format error")
                return 1
            alias, model = parts
            models.MODEL_ALIASES[alias.strip()] = model.strip()

    if not args.model:
        args.model = "gpt-4o-2024-08-06"
        if os.environ.get("ANTHROPIC_API_KEY"):
            args.model = "claude-3-5-sonnet-20241022"

    main_model = models.Model(
        args.model,
        weak_model=args.weak_model,
        editor_model=args.editor_model,
        editor_edit_format=args.editor_edit_format,
    )

    if args.copy_paste and args.edit_format is None:
        if main_model.edit_format in ("diff", "whole"):
            main_model.edit_format = "editor-" + main_model.edit_format

    if args.verbose:
        io.tool_output("Model metadata:")
        io.tool_output(json.dumps(main_model.info, indent=4))

        io.tool_output("Model settings:")
        for attr in sorted(fields(ModelSettings), key=lambda x: x.name):
            val = getattr(main_model, attr.name)
            val = json.dumps(val, indent=4)
            io.tool_output(f"{attr.name}: {val}")

    lint_cmds = parse_lint_cmds(args.lint_cmd, io)
    if lint_cmds is None:
        analytics.event("exit", reason="Invalid lint command format")
        return 1

    if args.show_model_warnings:
        problem = models.sanity_check_models(io, main_model)
        if problem:
            analytics.event("model warning", main_model=main_model)
            io.tool_output("You can skip this check with --no-show-model-warnings")

            try:
                io.offer_url(urls.model_warnings, "Open documentation url for more info?")
                io.tool_output()
            except KeyboardInterrupt:
                analytics.event("exit", reason="Keyboard interrupt during model warnings")
                return 1

    repo = None
    if args.git:
        try:
            repo = GitRepo(
                io,
                fnames,
                git_dname,
                args.aiderignore,
                models=main_model.commit_message_models(),
                attribute_author=args.attribute_author,
                attribute_committer=args.attribute_committer,
                attribute_commit_message_author=args.attribute_commit_message_author,
                attribute_commit_message_committer=args.attribute_commit_message_committer,
                commit_prompt=args.commit_prompt,
                subtree_only=args.subtree_only,
            )
        except FileNotFoundError:
            pass

    if not args.skip_sanity_check_repo:
        if not sanity_check_repo(repo, io):
            analytics.event("exit", reason="Repository sanity check failed")
            return 1

    if repo:
        analytics.event("repo", num_files=len(repo.get_tracked_files()))
    else:
        analytics.event("no-repo")

    commands = Commands(
        io,
        None,
        verify_ssl=args.verify_ssl,
        args=args,
        parser=parser,
        verbose=args.verbose,
        editor=args.editor,
    )

    summarizer = ChatSummary(
        [main_model.weak_model, main_model],
        args.max_chat_history_tokens or main_model.max_chat_history_tokens,
    )

    if args.cache_prompts and args.map_refresh == "auto":
        args.map_refresh = "files"

    if not main_model.streaming:
        if args.stream:
            io.tool_warning(
                f"Warning: Streaming is not supported by {main_model.name}. Disabling streaming."
            )
        args.stream = False

    try:
        coder = Coder.create(
            main_model=main_model,
            edit_format=args.edit_format,
            io=io,
            repo=repo,
            fnames=fnames,
            read_only_fnames=read_only_fnames,
            show_diffs=args.show_diffs,
            auto_commits=args.auto_commits,
            dirty_commits=args.dirty_commits,
            dry_run=args.dry_run,
            map_tokens=args.map_tokens,
            verbose=args.verbose,
            stream=args.stream,
            use_git=args.git,
            restore_chat_history=args.restore_chat_history,
            auto_lint=args.auto_lint,
            auto_test=args.auto_test,
            lint_cmds=lint_cmds,
            test_cmd=args.test_cmd,
            commands=commands,
            summarizer=summarizer,
            analytics=analytics,
            map_refresh=args.map_refresh,
            cache_prompts=args.cache_prompts,
            map_mul_no_files=args.map_multiplier_no_files,
            num_cache_warming_pings=args.cache_keepalive_pings,
            suggest_shell_commands=args.suggest_shell_commands,
            chat_language=args.chat_language,
            detect_urls=args.detect_urls,
            auto_copy_context=args.copy_paste,
        )
    except UnknownEditFormat as err:
        io.tool_error(str(err))
        io.offer_url(urls.edit_formats, "Open documentation about edit formats?")
        analytics.event("exit", reason="Unknown edit format")
        return 1
    except ValueError as err:
        io.tool_error(str(err))
        analytics.event("exit", reason="ValueError during coder creation")
        return 1

    if return_coder:
        analytics.event("exit", reason="Returning coder object")
        return coder

    ignores = []
    if git_root:
        ignores.append(str(Path(git_root) / ".gitignore"))
    if args.aiderignore:
        ignores.append(args.aiderignore)

    if git_root and Path.cwd().resolve() != Path(git_root).resolve():
        io.tool_warning(
            "Note: in-chat filenames are always relative to the git working dir, not the current"
            " working dir."
        )

        io.tool_output(f"Cur working dir: {Path.cwd()}")
        io.tool_output(f"Git working dir: {git_root}")

    if args.load:
        commands.cmd_load(args.load)

    if args.message:
        io.add_to_input_history(args.message)
        io.tool_output()
        try:
            coder.run(with_message=args.message)
        except SwitchCoder:
            pass
        analytics.event("exit", reason="Completed --message")

    if args.message_file:
        try:
            message_from_file = io.read_text(args.message_file)
            io.tool_output()
            coder.run(with_message=message_from_file)
        except FileNotFoundError:
            io.tool_error(f"Message file not found: {args.message_file}")
            analytics.event("exit", reason="Message file not found")
            return 1
        except IOError as e:
            io.tool_error(f"Error reading message file: {e}")
            analytics.event("exit", reason="Message file IO error")
            return 1

        analytics.event("exit", reason="Completed --message-file")

    analytics.event("cli session", main_model=main_model, edit_format=main_model.edit_format)

    previous_hash = repo.head.commit.hexsha

    def is_complete() -> bool:
        if not repo:
            return True

        history_file = Path('.aider.chat.history.md')
        if history_file.exists():
            with open(history_file) as f:
                for line in reversed(f.readlines()):
                    if args.stop_token in line:
                        return True
                    
        current_hash = repo.head.commit.hexsha

        if current_hash == previous_hash:
            print("Not modified. Finalizing session...")
            return True

        previous_hash = current_hash

        return False

    iteration = 0

    # continuation_prompt = read_prompt_file(self.continuation_prompt_file)
    # if not continuation_prompt:
    #     sys.exit(1)

    while iteration < args.max_iterations:
        if is_complete():
            break

        iteration += 1

        try:
            coder.run(f"""Continue development.
            Respond with \"{args.stop_token}\" when requirements satisfied.""")
        except Exception as e:
            print(f"Error during iteration {iteration}: {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()
