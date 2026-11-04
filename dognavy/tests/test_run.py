"""Host-side adapter tests; no Docker daemon, model API, or CyberGym data needed."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import Mock, patch

from dognavy import run


class TemporaryPaths(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)


class TrajectoryTests(TemporaryPaths):
    def test_accepts_json_objects_and_blank_lines(self):
        (self.root / "trajectory.jsonl").write_text(
            '\n{"type": "phase", "phase": "init"}\n\n'
            '{"type": "transcript", "text": "完成"}\n',
            encoding="utf-8",
        )
        self.assertTrue(run.validate_output(self.root))

    def test_rejects_missing_and_invalid_trajectories(self):
        self.assertFalse(run.validate_output(self.root))
        for contents in (
            b"",
            b" \n\t\n",
            b'{"type": "phase"}\n{"unfinished":',
            b'[{"type": "phase"}]\n',
            b"null\n",
            b'{"type": "phase"}\n{"type": "error"}\n',
            b'{"text": "\xff"}\n',
        ):
            with self.subTest(contents=contents):
                (self.root / "trajectory.jsonl").write_bytes(contents)
                self.assertFalse(run.validate_output(self.root))


class ContainerTests(TemporaryPaths):
    CID = "a" * 64

    def setUp(self):
        super().setUp()
        self.input_dir = self.root / "task input"
        self.log_dir = self.root / "task logs"
        self.input_dir.mkdir()
        self.log_dir.mkdir()

    def invoke(self, **kwargs):
        return run.run_dognavy(
            model="explicit-model", image_name="test/dognavy:latest",
            container_name="dognavy-test", log_dir=self.log_dir,
            input_dir=self.input_dir, timeout=120, **kwargs,
        )

    def docker_success(self, command, **kwargs):
        if command[:2] == ["docker", "run"]:
            Path(command[command.index("--cidfile") + 1]).write_text(self.CID)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def assert_removed_own_container(self, calls):
        removal = calls[-1]
        self.assertEqual(removal.args[0], ["docker", "rm", "--force", self.CID])
        self.assertGreater(removal.kwargs["timeout"], 0)
        self.assertLessEqual(removal.kwargs["timeout"], 60)

    def test_model_credentials_mounts_and_cleanup(self):
        environment = {
            "OPENAI_API_KEY": "secret-openai-value",
            "ANTHROPIC_API_KEY": "secret-anthropic-value",
            "OPENAI_COMPAT_BASE_URL": "https://model.invalid/v1",
            "OPENAI_COMPAT_MODEL": "shim-override",
            "MODEL": "inherited-model",
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "UNRELATED_SECRET": "do-not-forward",
        }
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(run.subprocess, "run", side_effect=self.docker_success) as docker, \
                self.assertLogs(run.logger, level="INFO") as logs:
            self.assertEqual(self.invoke(), 0)

        launch = docker.call_args_list[0]
        command = launch.args[0]
        forwarded = [command[i + 1] for i, item in enumerate(command) if item == "-e"]
        self.assertIn("--privileged", command)
        self.assertEqual(command[-1], "test/dognavy:latest")
        self.assertIn(f"{self.input_dir.resolve()}:/workspace", command)
        self.assertIn(f"{self.log_dir.resolve()}:/logs", command)
        self.assertIn("OPENAI_API_KEY", forwarded)
        self.assertIn("ANTHROPIC_API_KEY", forwarded)
        self.assertIn("OPENAI_COMPAT_BASE_URL", forwarded)
        self.assertIn("HTTPS_PROXY", forwarded)
        self.assertNotIn("UNRELATED_SECRET", forwarded)
        self.assertTrue(all("=" not in item for item in forwarded))
        self.assertEqual(launch.kwargs["env"]["MODEL"], "explicit-model")
        self.assertEqual(launch.kwargs["env"]["OPENAI_COMPAT_MODEL"], "shim-override")
        self.assertEqual(launch.kwargs["env"]["DSC_MAX_WALL_CLOCK"], "120")
        self.assertEqual(launch.kwargs["env"]["OPENAI_API_KEY"], "secret-openai-value")
        self.assertEqual(launch.kwargs["timeout"], 420)
        observable = repr(command) + "\n".join(logs.output)
        self.assertNotIn("secret-openai-value", observable)
        self.assertNotIn("secret-anthropic-value", observable)
        self.assert_removed_own_container(docker.call_args_list)
        self.assertTrue((self.log_dir / "console.log").is_file())

    def test_explicit_engine_budget_can_shorten_but_not_extend_timeout(self):
        for requested, expected in (("45", "45"), ("900", "120")):
            with self.subTest(requested=requested), \
                    patch.dict(os.environ, {"DSC_MAX_WALL_CLOCK": requested}, clear=True), \
                    patch.object(run.subprocess, "run", side_effect=self.docker_success) as docker:
                self.invoke()
                self.assertEqual(
                    docker.call_args_list[0].kwargs["env"]["DSC_MAX_WALL_CLOCK"], expected,
                )

    def test_timeout_and_interrupt_remove_container_before_propagating(self):
        for error in (subprocess.TimeoutExpired("docker", 420), KeyboardInterrupt()):
            def docker_run(command, **kwargs):
                if command[:2] == ["docker", "run"]:
                    self.docker_success(command, **kwargs)
                    raise error
                return self.docker_success(command, **kwargs)

            with self.subTest(error=type(error).__name__), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(run.subprocess, "run", side_effect=docker_run) as docker:
                with self.assertRaises(type(error)):
                    self.invoke()
                self.assert_removed_own_container(docker.call_args_list)

    def test_name_collision_does_not_remove_an_existing_container(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(run.subprocess, "run", return_value=subprocess.CompletedProcess([], 125)) as docker:
            self.assertEqual(self.invoke(), 125)
        self.assertEqual(docker.call_count, 1)

    def test_cleanup_failure_is_reported(self):
        def docker_run(command, **kwargs):
            if command[:2] == ["docker", "run"]:
                return self.docker_success(command, **kwargs)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="daemon unavailable")

        with patch.dict(os.environ, {}, clear=True), \
                patch.object(run.subprocess, "run", side_effect=docker_run):
            with self.assertRaises(run.DognavyCleanupError):
                self.invoke()


class FakeDifficulty(Enum):
    level1 = "level1"
    level2 = "level2"


class RunnerTests(TemporaryPaths):
    def setUp(self):
        super().setUp()
        self.agent = run.DognavyArgs(
            model="glm-5.3", log_dir=self.root / "logs", tmp_dir=self.root / "tmp",
        )
        self.task = run.TaskArgs(
            task_id="arvo:1068", data_dir=self.root / "data",
            server="http://server.invalid:8000", difficulty="level2",
            mask_map_path=self.root / "mask_map.json",
        )
        self.generate = Mock(return_value={"task_id": "arvo:1068", "description": "fixture"})
        self.task_config = Mock(side_effect=lambda **values: types.SimpleNamespace(**values))
        modules = {name: types.ModuleType(name) for name in (
            "cybergym", "cybergym.task", "cybergym.task.gen_task", "cybergym.task.types",
        )}
        modules["cybergym.task.gen_task"].generate_task = self.generate
        modules["cybergym.task.types"].TaskConfig = self.task_config
        modules["cybergym.task.types"].TaskDifficulty = FakeDifficulty
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def container_result(self, code=0, error=None, trajectory='{"type": "phase"}\n'):
        def execute(*, model, image_name, container_name, log_dir, input_dir, timeout):
            self.assertTrue(input_dir.is_dir())
            self.assertEqual(model, self.agent.model)
            (log_dir / "trajectory.jsonl").write_text(trajectory, encoding="utf-8")
            if error is not None:
                raise error
            return code
        return execute

    def test_task_identity_masking_metadata_and_temporary_cleanup(self):
        with patch.object(run, "run_dognavy", side_effect=self.container_result()) as container:
            agent_id = run.run_with_configs(dognavy_args=self.agent, task_args=self.task)

        self.assertRegex(agent_id, r"^[0-9a-f]{32}$")
        task_config = self.generate.call_args.args[0]
        self.assertEqual(task_config.agent_id, agent_id)
        self.assertEqual(task_config.task_id, self.task.task_id)
        self.assertEqual(task_config.mask_map_path, self.task.mask_map_path)
        self.assertEqual(task_config.difficulty, FakeDifficulty.level2)
        self.assertEqual(task_config.server, self.task.server)
        self.assertFalse(task_config.out_dir.exists())
        logs = self.agent.log_dir / f"arvo_1068-{agent_id}"
        metadata = json.loads((logs / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["agent"], "dognavy:glm-5.3")
        self.assertEqual(metadata["agent_args"]["model"], "glm-5.3")
        self.assertEqual(metadata["task_args"]["task_id"], self.task.task_id)
        self.assertEqual(metadata["task"], self.generate.return_value)
        container.assert_called_once()

    def test_retains_generated_task_when_requested(self):
        self.agent.remove_tmp = False
        with patch.object(run, "run_dognavy", side_effect=self.container_result()):
            self.assertIsNotNone(run.run_with_configs(self.agent, self.task))
        self.assertTrue(self.generate.call_args.args[0].out_dir.is_dir())

    def test_valid_partial_trajectory_keeps_agent_id_after_budget_expiry(self):
        for code, error in (
            (124, None), (137, None), (143, None),
            (None, subprocess.TimeoutExpired("docker", 14700)),
        ):
            with self.subTest(code=code, error=error), \
                    patch.object(run, "run_dognavy", side_effect=self.container_result(code, error)):
                self.assertIsNotNone(run.run_with_configs(self.agent, self.task))
                self.assertFalse(self.generate.call_args.args[0].out_dir.exists())

    def test_runtime_failure_or_invalid_trajectory_returns_no_agent_id(self):
        for code, trajectory in ((1, '{"type": "phase"}\n'), (0, "bad json"), (124, "")):
            with self.subTest(code=code, trajectory=trajectory), \
                    patch.object(run, "run_dognavy", side_effect=self.container_result(code, trajectory=trajectory)):
                self.assertIsNone(run.run_with_configs(self.agent, self.task))

    def test_generation_failure_still_removes_temporary_task(self):
        self.generate.side_effect = RuntimeError("task data unavailable")
        with patch.object(run, "run_dognavy") as container:
            with self.assertRaisesRegex(RuntimeError, "task data unavailable"):
                run.run_with_configs(self.agent, self.task)
        container.assert_not_called()
        self.assertFalse(self.generate.call_args.args[0].out_dir.exists())
        self.assertTrue(self.agent.log_dir.is_dir())

    def test_failed_container_cleanup_preserves_task_files(self):
        with patch.object(run, "run_dognavy", side_effect=run.DognavyCleanupError("daemon unavailable")):
            self.assertIsNone(run.run_with_configs(self.agent, self.task))
        self.assertTrue(self.generate.call_args.args[0].out_dir.is_dir())

    def test_rejects_invalid_configuration_before_task_generation(self):
        for field, value in (("timeout", 0), ("timeout", -1), ("model", ""), ("model", "   ")):
            original = getattr(self.agent, field)
            with self.subTest(field=field, value=value):
                setattr(self.agent, field, value)
                with self.assertRaises(ValueError):
                    run.run_with_configs(self.agent, self.task)
                self.generate.assert_not_called()
                setattr(self.agent, field, original)

    def test_rejects_invalid_engine_budget_before_task_generation(self):
        for budget in ("0", "-5", "1.5", "invalid"):
            with self.subTest(budget=budget), \
                    patch.dict(os.environ, {"DSC_MAX_WALL_CLOCK": budget}):
                with self.assertRaisesRegex(ValueError, "DSC_MAX_WALL_CLOCK"):
                    run.run_with_configs(self.agent, self.task)
                self.generate.assert_not_called()

    def test_rejects_shared_task_and_log_directory(self):
        self.agent.tmp_dir = self.agent.log_dir
        with self.assertRaises(ValueError):
            run.run_with_configs(self.agent, self.task)
        self.generate.assert_not_called()

    def test_cli_parses_standard_adapter_options_and_prints_agent_id(self):
        output = io.StringIO()
        with patch.object(run, "run_with_configs", return_value="a" * 32) as execute, \
                contextlib.redirect_stdout(output):
            result = run.main([
                "--model", "glm-5.3", "--log_dir", str(self.agent.log_dir),
                "--tmp_dir", str(self.agent.tmp_dir), "--task_id", "arvo:1068",
                "--data_dir", str(self.task.data_dir), "--server", self.task.server,
                "--mask_map_path", str(self.task.mask_map_path),
                "--difficulty", "level2", "--remove_tmp", "false", "--timeout", "60",
            ])
        self.assertEqual(result, "a" * 32)
        self.assertEqual(output.getvalue().strip(), result)
        agent, task = execute.call_args.args
        self.assertFalse(agent.remove_tmp)
        self.assertEqual(agent.timeout, 60)
        self.assertEqual(task.mask_map_path, self.task.mask_map_path)
        self.assertEqual(task.difficulty, "level2")


if __name__ == "__main__":
    unittest.main()
