#!/usr/bin/env python

import os
import sys
import git
import yaml
from pathlib import Path
from typing import List, Optional

from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput


class AiderOrchestrator:
    def __init__(
        self,
        requirements_file: str,
        continuation_prompt_file: str,
        commit_prompt_file: str,
        max_iterations: int = 10,
        stop_token: str = "DONE",
    ):
        self.requirements_file = Path(requirements_file)
        self.continuation_prompt_file = Path(continuation_prompt_file)
        self.commit_prompt_file = Path(commit_prompt_file)
        self.max_iterations = max_iterations
        self.stop_token = stop_token
        self.repo = git.Repo(search_parent_directories=True)
        self.previous_hash = self.repo.head.commit.hexsha

        # Base configuration that matches shell script args
        self.base_config = {
            "analytics_disable": True,
            "auto_commits": True,
            "auto_lint": True,
            "auto_test": False,
            "check_update": False,
            "stream": False,
            "yes_always": True,
        }

        # Read paths relative to repo root
        self.read_files = [
            "CONVENTIONS.md",
            "packages/prompts/CONVENTIONS.md",
            "packages/prompts/README.md",
            "README.md",
            "TESTING.md",
        ]
        self.read_files = [str(Path(self.repo.working_tree_dir) / f) for f in self.read_files]

    def load_config(self) -> dict:
        """Load and merge .aider.conf.yml with base config"""
        config_path = Path.home() / ".aider.conf.yml"
        if config_path.exists():
            with open(config_path) as f:
                user_config = yaml.safe_load(f)
            return {**self.base_config, **(user_config or {})}
        return self.base_config

    def is_complete(self) -> bool:
        """Check if the orchestration should stop"""
        history_file = Path(".aider.chat.history.md")
        if history_file.exists():
            with open(history_file) as f:
                if self.stop_token in f.read():
                    return True

        current_hash = self.repo.head.commit.hexsha
        if current_hash == self.previous_hash:
            print("Not modified. Finalizing session...")
            return True

        self.previous_hash = current_hash
        return False

    def read_prompt_file(self, file_path: Path) -> Optional[str]:
        """Read content from a prompt file"""
        try:
            with open(file_path) as f:
                return f.read()
        except FileNotFoundError:
            print(f"Warning: Prompt file not found: {file_path}")
            return None

    def create_coder(self, chat_mode: str) -> Coder:
        """Create and configure a Coder instance"""
        config = self.load_config()
        
        # Configure IO with non-interactive settings
        io = InputOutput(
            pretty=False,
            yes=True,
            input_history_file=".aider.input.history",
            chat_history_file=".aider.chat.history.md",
        )

        # Create model with base configuration
        model = Model("gpt-4-turbo")  # Or configure from config/env

        return Coder.create(
            main_model=model,
            io=io,
            fnames=[],  # Files will be added via read_only_fnames
            read_only_fnames=self.read_files,
            auto_commits=config["auto_commits"],
            auto_lint=config["auto_lint"],
            auto_test=config["auto_test"],
            stream=config["stream"],
            chat_mode=chat_mode,
        )

    def run(self):
        """Main orchestration loop"""
        # Initial architecture phase
        requirements = self.read_prompt_file(self.requirements_file)
        if not requirements:
            sys.exit(1)

        architect_coder = self.create_coder(chat_mode="architect")
        architect_coder.run(requirements)

        # Implementation phase
        continuation_prompt = self.read_prompt_file(self.continuation_prompt_file)
        if not continuation_prompt:
            sys.exit(1)

        iteration = 0
        while iteration < self.max_iterations:
            if self.is_complete():
                break

            iteration += 1
            print(f"Starting iteration {iteration}")

            try:
                coder = self.create_coder(chat_mode="code")
                coder.run(continuation_prompt)
            except Exception as e:
                print(f"Error during iteration {iteration}: {e}")
                sys.exit(1)


def main():
    orchestrator = AiderOrchestrator(
        requirements_file=os.environ.get("REQUIREMENTS_FILE", "requirements.txt"),
        continuation_prompt_file=os.environ.get("CONTINUATION_PROMPT_FILE", "continuation.txt"),
        commit_prompt_file=os.environ.get("COMMIT_PROMPT_FILE", "commit_prompt.txt"),
        max_iterations=int(os.environ.get("MAX_ITERATIONS", "10")),
        stop_token=os.environ.get("STOP_TOKEN", "DONE"),
    )
    orchestrator.run()


if __name__ == "__main__":
    main()
