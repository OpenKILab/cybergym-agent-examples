#!/usr/bin/env python3
"""CyberGym platform adapter for the black-box DoGNAVY full-pipeline image.

generate_task -> privileged container -> wait -> validate trajectory -> agent_id.
CyberGym is imported lazily so CLI help only requires simple-parsing.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4

from simple_parsing import ArgumentParser

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = (
    "registry.h.pjlab.org.cn/ailab-safevlagent-safevlagent_cpu/"
    "yangzhonghao:cybergym-dognavy"
)
EXPORT_GRACE_SECONDS = 300
TIMEOUT_EXIT_CODES = (124, 137, 143)
PASSTHROUGH_ENVS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_MODEL",
    "MODEL",
    "ANTHROPIC_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "DSC_MAX_WALL_CLOCK",
    "DSC_MAX_RESTARTS",
)


class DognavyCleanupError(RuntimeError):
    """The container may still be using its mounted task directory."""


@dataclass
class DognavyArgs:
    model: str
    """Model identifier passed to the image as MODEL, e.g. glm-5.3."""

    log_dir: Path
    """Directory in which per-run logs are stored."""

    tmp_dir: Path
    """Directory in which generated CyberGym task files are stored."""

    image_name: str = DEFAULT_IMAGE
    """Prebuilt DoGNAVY image, or the local cybergym/dognavy:latest tag."""

    timeout: int = 14400
    """Agent budget in seconds; the host allows 300 extra seconds for export."""

    container_name: str | None = None
    """Optional fixed container name; a unique name is used by default."""

    remove_tmp: bool = True
    """Remove generated task files after the container has been removed."""


@dataclass
class TaskArgs:
    task_id: str
    """CyberGym task ID, e.g. arvo:1068."""

    data_dir: Path
    """Directory containing CyberGym task data."""

    server: str
    """CyberGym execution server URL (records PoC submissions)."""

    difficulty: str = "level1"
    """CyberGym task difficulty; validated by TaskDifficulty at runtime."""

    mask_map_path: Path | None = None
    """Optional task ID mask map; must match the execution server's map."""


def validate_output(log_dir: Path) -> bool:
    """Validate exported JSONL, not the server's official vul/fix verdict."""
    trajectory = log_dir / "trajectory.jsonl"
    event_count = 0
    try:
        with trajectory.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    logger.warning("Trajectory line %s is not a JSON object", line_number)
                    return False
                if event.get("type") == "error":
                    logger.warning("Trajectory contains an error on line %s", line_number)
                    return False
                event_count += 1
    except (OSError, ValueError) as error:
        # ValueError also covers JSONDecodeError and UnicodeDecodeError.
        logger.warning("Invalid trajectory %s: %s", trajectory, error)
        return False
    if not event_count:
        logger.warning("Trajectory contains no events: %s", trajectory)
        return False
    return True


def _container_environment(model: str, timeout: int) -> dict[str, str]:
    if not model.strip():
        raise ValueError("model must not be empty")
    if timeout < 1:
        raise ValueError("timeout must be at least 1 second")
    environment = os.environ.copy()
    # Do not mutate the host environment or let an inherited MODEL override CLI.
    environment["MODEL"] = model
    budget = environment.get("DSC_MAX_WALL_CLOCK") or str(timeout)
    try:
        wall_clock = int(budget)
    except ValueError:
        raise ValueError("DSC_MAX_WALL_CLOCK must be a positive integer in seconds") from None
    if wall_clock < 1:
        raise ValueError("DSC_MAX_WALL_CLOCK must be a positive integer in seconds")
    environment["DSC_MAX_WALL_CLOCK"] = str(min(wall_clock, timeout))
    return environment


def _remove_container(cid_file: Path, console) -> None:
    """Remove only the container whose ID Docker recorded for this invocation."""
    try:
        if not cid_file.exists():
            # In particular, a conflicting --name must not remove someone else's run.
            return
        container_id = cid_file.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise DognavyCleanupError(f"Invalid Docker container ID file: {cid_file}")
        result = subprocess.run(
            ["docker", "rm", "--force", container_id],
            stdout=console,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise DognavyCleanupError(f"Could not remove DoGNAVY container {container_id}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise DognavyCleanupError("Could not confirm DoGNAVY container cleanup") from error


def run_dognavy(
    *,
    model: str,
    image_name: str,
    container_name: str,
    log_dir: Path,
    input_dir: Path,
    timeout: int,
) -> int:
    """Run the original image entrypoint and always clean up its container."""
    environment = _container_environment(model, timeout)
    # Keep the ownership record outside the directories writable by the image.
    with tempfile.TemporaryDirectory(prefix="dognavy-") as control_dir:
        cid_file = Path(control_dir) / "container.cid"
        command = [
            "docker", "run", "--name", container_name,
            "--cidfile", str(cid_file),
            "--privileged",
            "-v", f"{input_dir.resolve()}:/workspace",
            "-v", f"{log_dir.resolve()}:/logs",
        ]
        for key in PASSTHROUGH_ENVS:
            if environment.get(key):
                # Docker reads the value from its process environment. Credentials
                # never appear in command arguments or the adapter's log messages.
                command.extend(["-e", key])
        command.append(image_name)
        logger.info("Running DoGNAVY container %s with model %s", container_name, model)
        with (log_dir / "console.log").open("wb") as console:
            try:
                result = subprocess.run(
                    command,
                    env=environment,
                    stdout=console,
                    stderr=subprocess.STDOUT,
                    timeout=timeout + EXPORT_GRACE_SECONDS,
                    check=False,
                )
                logger.info("DoGNAVY container %s exited with code %s", container_name, result.returncode)
                return result.returncode
            finally:
                # Killing a timed-out Docker CLI does not stop the daemon's container.
                _remove_container(cid_file, console)


def _json_default(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)


def run_with_configs(dognavy_args: DognavyArgs, task_args: TaskArgs) -> str | None:
    """Generate a task and return its agent ID when an evaluable trace exists."""
    _container_environment(dognavy_args.model, dognavy_args.timeout)
    if dognavy_args.tmp_dir.resolve() == dognavy_args.log_dir.resolve():
        raise ValueError("tmp_dir and log_dir must be different directories")

    from cybergym.task.gen_task import generate_task
    from cybergym.task.types import TaskConfig, TaskDifficulty

    difficulty = TaskDifficulty(task_args.difficulty)
    agent_id = uuid4().hex
    task_name = task_args.task_id.replace(":", "_").replace("/", "_").replace("\\", "_")
    sub_dir = f"{task_name}-{agent_id}"
    input_dir = dognavy_args.tmp_dir.resolve() / sub_dir
    log_dir = dognavy_args.log_dir.resolve() / sub_dir
    log_dir.mkdir(parents=True)
    input_dir.mkdir(parents=True)
    remove_tmp = dognavy_args.remove_tmp

    try:
        task = generate_task(TaskConfig(
            task_id=task_args.task_id,
            out_dir=input_dir,
            data_dir=task_args.data_dir,
            server=task_args.server,
            difficulty=difficulty,
            agent_id=agent_id,
            mask_map_path=task_args.mask_map_path,
        ))
        with (log_dir / "args.json").open("w", encoding="utf-8") as stream:
            json.dump(
                {"agent": f"dognavy:{dognavy_args.model}", "task": task,
                 "agent_args": dognavy_args, "task_args": task_args},
                stream, indent=2, default=_json_default,
            )
            stream.write("\n")

        exit_code = None
        try:
            exit_code = run_dognavy(
                model=dognavy_args.model,
                image_name=dognavy_args.image_name,
                container_name=dognavy_args.container_name or f"dognavy-{agent_id}",
                log_dir=log_dir,
                input_dir=input_dir,
                timeout=dognavy_args.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("DoGNAVY exceeded its budget and export grace period")
        except DognavyCleanupError as error:
            remove_tmp = False
            logger.error("%s; retaining task files at %s", error, input_dir)
            return None
        except OSError as error:
            logger.error("Could not run DoGNAVY: %s", error)
            return None

        if exit_code in TIMEOUT_EXIT_CODES or exit_code is None:
            # A bounded benchmark can have already submitted a PoC. Preserve the
            # reference adapter's ability to query that run's official verdict.
            logger.warning("DoGNAVY timed out; checking exported trajectory (exit code %s)", exit_code)
        elif exit_code != 0:
            logger.error("DoGNAVY failed with exit code %s; see %s", exit_code, log_dir / "console.log")
            return None
        return agent_id if validate_output(log_dir) else None
    finally:
        if remove_tmp:
            try:
                shutil.rmtree(input_dir)
            except OSError as error:
                logger.warning("Could not remove temporary task directory %s: %s", input_dir, error)


def main(raw_args=None) -> str | None:
    parser = ArgumentParser()
    parser.add_arguments(DognavyArgs, dest="dognavy_args")
    parser.add_arguments(TaskArgs, dest="task_args")
    args = parser.parse_args(raw_args)
    agent_id = run_with_configs(args.dognavy_args, args.task_args)
    if agent_id:
        print(agent_id)
    return agent_id


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    sys.exit(0 if main() else 1)
