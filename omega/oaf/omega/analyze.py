#!/usr/bin/env python
"""
Executes analysis and creates assertions.
"""
import fnmatch
import argparse
import logging
import os
import shlex
import subprocess  # nosec: B404
import tempfile
from datetime import datetime, timedelta

from dotenv import dotenv_values

from assertion.utils import get_package_url_with_version, is_command_available
from security import safe_command

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")


class AnalysisRunner:
    """
    Executes analysis and creates assertions.
    """

    def __init__(self, package_url: str, docker_container: str, repository: str, signer: str, work_directory: str | None):
        """Initialize a new Analysis Runner."""
        required_commands = [
            ["python", "-V"],
            ["dotnet", "--info"],
            ["RecursiveExtractor", "--help"],
            ["docker", "--help"],
            ["docker", "image", "inspect", docker_container],
            ["oss-find-source", "--help"],
        ]

        self.docker_cmdline = None

        for command in required_commands:
            if not is_command_available(command):
                raise EnvironmentError(f"Required command {command} is not available.")

        if not os.path.isfile(".env"):
            raise EnvironmentError("Missing .env file.")
        self.env = dotenv_values(".env")

        self.docker_container = docker_container
        self.package_url = get_package_url_with_version(package_url)
        self.repository = repository
        self.signer = signer

        # Set up the work directory (default: temporary, or provided by the user)
        if work_directory:
            self.work_directory = work_directory
            self.work_directory_name = self.work_directory
        else:
            self.work_directory = tempfile.TemporaryDirectory(  # pylint: disable=consider-using-with
                prefix="omega-", ignore_cleanup_errors=True
            )
            self.work_directory_name = self.work_directory.name
        logging.debug("Output (work) directory: %s", self.work_directory_name)

        os.makedirs(self.work_directory_name, exist_ok=True)

        _wd = self.work_directory if isinstance(self.work_directory, str) else self.work_directory.name
        if os.listdir(_wd):
            logging.fatal("Output directory (%s) must be empty.", self.work_directory_name)
            raise EnvironmentError("Output directory must be empty.")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Clean up after ourselves."""
        if isinstance(self.work_directory, tempfile.TemporaryDirectory):
            try:
                self.work_directory.cleanup()
            except Exception:  # pylint: disable=broad-except
                logging.warning("We were unable to clean up the directory: %s", self.work_directory_name)

    def execute_docker_container(self):
        """Runs the Omega docker container with specific arguments."""
        logging.info("Running Omega analysis toolchain")
        cmd = [
            "docker",
            "run",
            "--rm",
            "-t",
            "-v",
            f"{self.work_directory_name}:/opt/export",
            "--env-file",
            ".env",
            self.docker_container,
            str(self.package_url),
        ]

        # Limit CPU usage if OMEGA_DOCKER_CPUS is set
        if os.environ.get('OMEGA_DOCKER_CPUS'):
            cmd.insert(cmd.index('-t'), f'--cpus={os.environ.get("OMEGA_DOCKER_CPUS")}')

        # Write the command to a file so we can capture it later
        self.docker_cmdline = shlex.join(cmd)
        with open(f"{self.work_directory_name}/top-execute-cmd.txt", "w", encoding="utf-8") as f:
            f.write(self.docker_cmdline)

        logging.debug("Running command: %s", cmd)
        with safe_command.run(subprocess.Popen, cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            universal_newlines=True
        ) as res:
            for line in iter(res.stdout.readline, ""):
                logging.debug(line.rstrip())

            res.stdout.close()

            if res.wait() != 0:
                raise RuntimeError(f"Error running docker container: {res.stderr}")

    def _execute_assertion_noexcept(self, **kwargs):
        try:
            if 'input-file' in kwargs and not kwargs.get('input-file'):
                logging.warning("Skipping assertion because input file is not set.")
                return
            self._execute_assertion(**kwargs)
        except Exception as msg:
            logging.error("Error executing assertion: %s", msg)

    def _execute_assertion(self, **kwargs):
        """Executes a single assertion."""
        logging.info("Running assertion %s", kwargs.get("assertion"))
        cmd = ["python", "oaf.py", "--verbose", "generate"]

        if "expiration" not in kwargs:
            kwargs["expiration"] = datetime.strftime(
                datetime.now() + timedelta(days=2 * 365), "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        else:
            cmd.append(f"--expiration={kwargs['expiration']}")

        if kwargs.get('signer'):
            cmd.append(f"--signer={kwargs['signer']}")

        for key, value in kwargs.items():
            cmd.append(f"--{key}")
            if value is None:
                cmd.append("")
            else:
                cmd.append(str(value))

        logging.debug("Running command: %s", cmd)
        _env = os.environ.copy()
        _env.update(self.env)

        res = safe_command.run(subprocess.run, cmd, check=False, capture_output=True, encoding="utf-8", env=_env
        )

        if res.returncode != 0:
            logging.debug("Error Code: %d", res.returncode)
            logging.debug("Output:\n%s", res.stdout)
            logging.debug("Error:\n%s", res.stderr)

    def find_output_file(self, filename: str) -> str:
        """Finds the first file in the output directory that matches the given filename/glob."""
        for root, _, files in os.walk(self.work_directory_name):
            if filename in files:
                return os.path.join(root, filename)

            for file in files:
                if fnmatch.fnmatch(file, filename):
                    return os.path.join(root, file)

        for root, _, files in os.walk(self.work_directory_name):
            if filename in files:
                return os.path.join(root, filename)

        return None

    def execute_assertions(self):
        """Execute all assertions."""
        # Scorecards
        self._execute_assertion_noexcept(
            **{
                "assertion": "SecurityScorecard",
                "subject": self.package_url,
                "repository": self.repository,
                "signer": self.signer
            }
        )

        # Security Advisories
        self._execute_assertion_noexcept(
            **{
                "assertion": "SecurityAdvisory",
                "subject": self.package_url,
                "repository": self.repository,
                "signer": self.signer
            }
        )

        # Reproducibility
        self._execute_assertion_noexcept(
            **{
                "assertion": "Reproducible",
                "subject": self.package_url,
                "repository": self.repository,
                "signer": self.signer
            }
        )

        # Static Analyzers (SARIF)
        for _filename in ['tool-semgrep.sarif', 'tool-devskim.sarif', 'tool-codeql-basic.javascript.sarif', 'tool-snyk-code.sarif']:
            self._execute_assertion_noexcept(
                **{
                    "assertion": "SecurityToolFinding",
                    "subject": self.package_url,
                    "input-file": self.find_output_file(_filename),
                    "repository": self.repository,
                    "signer": self.signer,
                    "extra-args": "include_evidence=false"
                }
            )

        # Programming Language
        self._execute_assertion_noexcept(
            **{
                "assertion": "ProgrammingLanguage",
                "subject": self.package_url,
                "input-file": self.find_output_file("tool-application-inspector.json"),
                "repository": self.repository,
                "signer": self.signer
            }
        )

        # Characteristics
        self._execute_assertion_noexcept(
            **{
                "assertion": "Characteristic",
                "subject": self.package_url,
                "input-file": self.find_output_file("tool-application-inspector.json"),
                "repository": self.repository,
                "signer": self.signer
            }
        )

        # Cryptographic Implementations
        self._execute_assertion_noexcept(
            **{
                "assertion": "CryptoImplementation",
                "subject": self.package_url,
                "input-file": self.find_output_file("tool-oss-detect-cryptography.txt"),
                "repository": self.repository,
                "signer": self.signer
            }
        )

        # Cryptographic Implementations
        self._execute_assertion_noexcept(
            **{
                "assertion": "ClamAV",
                "subject": self.package_url,
                "input-file": self.find_output_file("tool-clamscan.txt"),
                "repository": self.repository,
                "signer": self.signer
            }
        )

        # Metadata
        self._execute_assertion_noexcept(
            **{
                "assertion": "Metadata",
                "subject": self.package_url,
                "input-file": self.find_output_file("tool-metadata-native.json"),
                "repository": self.repository,
                "signer": self.signer
            }
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-url", required=True)
    parser.add_argument(
        "--toolchain-container", required=False, default="openssf/omega-toolshed:latest"
    )
    parser.add_argument('--work-directory', required=False, help='Use a specific working directory instead of a temporary one.')
    parser.add_argument(
        "--repository", required=True
    )
    parser.add_argument(
        "--signer", required=False
    )
    args = parser.parse_args()

    logging.info("Starting analysis runner")
    runner = AnalysisRunner(args.package_url, args.toolchain_container, args.repository, args.signer, args.work_directory)
    runner.execute_docker_container()
    runner.execute_assertions()
