#!/usr/bin/env python

import sys
import git
import yaml
from pathlib import Path

from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput
from aider.args import get_parser

class AiderWorker:
    def __init__(self):
        self.repo = None
        self.previous_hash = None
        self.base_config = {
            "analytics_disable": True,
            "auto_commits": True,
            "auto_lint": True,
            "auto_test": False,
            "check_update": False,
            "stream": False,
            "yes_always": True,
        }

        parser = get_parser([".aider.conf.yml"], None)
        self.args = parser.parse_args()
        
        try:
            self.repo = git.Repo(search_parent_directories=True)
            self.previous_hash = self.repo.head.commit.hexsha
        except git.exc.InvalidGitRepositoryError:
            print("Not a git repository")

        self.read_files = []
        if self.args.read:
            self.read_files = [str(Path(self.args.read[i])) for i in range(len(self.args.read))]
            
    def load_config(self) -> dict:
        config_path = Path.home() / ".aider.conf.yml"
        if config_path.exists():
            with open(config_path) as f:
                user_config = yaml.safe_load(f)

            return {
                **self.base_config, 
                **(user_config or {})
            }

        return self.base_config

    def is_complete(self) -> bool:
        if not self.repo:
            return True

        stop_token=self.args.stop_token or '__AIDER_END__'
        history_file = Path('.aider.chat.history.md')
        if history_file.exists():
            with open(history_file) as f:
                for line in reversed(f.readlines()):
                    if stop_token in line:
                        return True
                    
        current_hash = self.repo.head.commit.hexsha
        if current_hash == self.previous_hash:
            print("Not modified. Finalizing session...")
            return True

        self.previous_hash = current_hash

        return False

    def create_coder(self, chat_mode: str) -> Coder:
        config = self.load_config()
        
        io = InputOutput(
            pretty=config['pretty'] or False,
            yes=self.args.yes,
            input_history_file='.aider.input.history',
            chat_history_file='.aider.chat.history.md',
        )

        model = Model(
            self.args.model or config['model']
        )

        return Coder.create(
            main_model=model,
            io=io,
            files=self.args.files,
            read_only_fnames=self.read_files,
            auto_commits=self.args.auto_commits,
            auto_lint=self.args.auto_lint,
            auto_test=self.args.auto_test,
            stream=self.args.stream,
            chat_mode=chat_mode,
        )

    def run(self):
        chat_mode=self.args.chat_mode or "code"
        coder = self.create_coder(chat_mode) 

        if self.args.message:
            message=self.args.message

        if self.args.message_file:
            with open(self.args.message_file) as f:
                message = f.read()

        # continuation_prompt = self.read_prompt_file(self.continuation_prompt_file)
        # if not continuation_prompt:
        #     sys.exit(1)

        iteration = 0
        while iteration < self.max_iterations:
            if self.is_complete():
                break

            iteration += 1

            try:
                coder.run(message)
            except Exception as e:
                print(f"Error during iteration {iteration}: {e}")
                sys.exit(1)

def main():
    worker = AiderWorker()
    worker.run()

if __name__ == "__main__":
    main()
