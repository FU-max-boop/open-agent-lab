from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from harbor.models.job.config import RetryConfig
from harbor.trial.hooks import TrialEvent

from benchmarks.terminal_bench import paired_results as paired
from benchmarks.terminal_bench import pilot_launcher as launcher
from benchmarks.terminal_bench.experiment_contract import (
    DEEPSEEK_PROVIDER_CONTROL_SOURCES,
    ZAI_PROVIDER_CONTROL_SOURCES,
    canonical_json,
)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _slot(root: Path, ordinal: int, provider: str = "deepseek") -> launcher.PilotSlot:
    lock = {"ordinal": ordinal}
    authorizations = root / "authorizations"
    _private_directory(authorizations)
    run = SimpleNamespace(
        run_dir=root,
        binding={
            "preflight_sha256": "sha256:" + "1" * 64,
            "replication_id": "screen-v1",
        },
        preflight={"sourceRevision": "a" * 40},
    )
    prepared = SimpleNamespace(
        run_dir=root,
        binding=run.binding,
        job_dir=root / "jobs" / provider,
        entry={"configSha256": "sha256:" + "2" * 64},
        compose_path=root / "compose.yaml",
        compose_sha256="sha256:" + "3" * 64,
    )
    return launcher.PilotSlot(
        ordinal,
        "screen-v1",
        provider,
        paired._PROVIDERS[provider]["model"],
        paired._TASKS[(ordinal - 1) % len(paired._TASKS)],
        "control-v1" if ordinal % 2 else "verify-instruction-v1",
        paired._digest(lock),
        run,
        prepared,
    )


def _identity(provider: str) -> dict[str, object]:
    return {"provider": provider, "model": paired._PROVIDERS[provider]["model"]}


def _plan(root: Path, count: int, provider: str = "deepseek") -> launcher.PilotPlan:
    slots = tuple(_slot(root, ordinal, provider) for ordinal in range(1, count + 1))
    value = {
        "schemaVersion": 1,
        "proofClass": "test-plan",
        "providerControlIdentities": {
            item: _identity(item) for item in sorted({slot.provider for slot in slots})
        },
        "slots": [slot.public() for slot in slots],
    }
    return launcher.PilotPlan(slots, value, paired._digest(value))


def _job() -> SimpleNamespace:
    return SimpleNamespace(id=UUID(int=7), config=SimpleNamespace())


def _result(ordinal: int) -> SimpleNamespace:
    return SimpleNamespace(trial_name=f"trial-{ordinal:02d}")


def _provider_control(provider: str, replication: str) -> dict[str, object]:
    observed = datetime(2026, 8, 25, tzinfo=timezone.utc)
    if replication == "mirror-v1":
        observed += timedelta(minutes=10)

    def stamp(value: datetime) -> str:
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    control: dict[str, object] = {
        "controlClass": "provider_hard_spend_cap_usd",
        "scope": "campaign",
        "observedAt": stamp(observed),
        "expiresAt": stamp(observed + timedelta(hours=3)),
        "evidenceSha256": "sha256:" + ("4" if provider == "deepseek" else "5") * 64,
        "sourceUrls": dict(DEEPSEEK_PROVIDER_CONTROL_SOURCES),
        "assertedBy": replication,
        "limitUsd": 2,
    }
    if provider == "zai":
        control = {
            "controlClass": "coding_plan_subscription_quota_no_balance_deduction",
            "scope": "campaign",
            "observedAt": stamp(observed),
            "expiresAt": stamp(observed + timedelta(hours=3)),
            "evidenceSha256": "sha256:" + "5" * 64,
            "sourceUrls": dict(ZAI_PROVIDER_CONTROL_SOURCES),
            "assertedBy": replication,
            "baseUrl": "https://api.z.ai/api/v1",
            "protocol": "openai_responses",
            "plan": "zai_coding_plan",
            "noBalanceDeduction": True,
            "quotaSnapshot": {
                "fiveHour": {
                    "remainingPercent": 80 if replication == "screen-v1" else 70,
                    "resetsAt": stamp(observed + timedelta(hours=4)),
                },
                "weekly": {
                    "remainingPercent": 60 if replication == "screen-v1" else 50,
                    "resetsAt": stamp(observed + timedelta(days=6)),
                },
            },
        }
    return control


class PilotLauncherTest(unittest.TestCase):
    def test_plan_freezes_four_logical_jobs_and_all_40_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            screen = parent / "screen"
            mirror = parent / "mirror"
            screen.mkdir()
            mirror.mkdir()
            runs: dict[tuple[Path, str], SimpleNamespace] = {}
            for replication, root in (("screen-v1", screen), ("mirror-v1", mirror)):
                preflight = {
                    "replicationId": replication,
                    "sourceRevision": "a" * 40,
                }
                binding = {"preflight_sha256": paired._digest(preflight)}
                for provider in launcher._PROVIDER_SEQUENCE:
                    order = (
                        ["control-v1", "verify-instruction-v1"]
                        if (replication, provider)
                        in {("screen-v1", "deepseek"), ("mirror-v1", "zai")}
                        else ["verify-instruction-v1", "control-v1"]
                    )
                    locks = [
                        {"task": task, "variant": variant}
                        for task in paired._TASKS
                        for variant in order
                    ]
                    prepared = SimpleNamespace(
                        entry={
                            "armOrder": order,
                            "configSha256": paired._digest(
                                {"replication": replication, "provider": provider}
                            ),
                        },
                        expected_trial_locks=lambda locks=locks: locks,
                    )
                    run = SimpleNamespace(
                        run_dir=root,
                        provider=provider,
                        model=paired._PROVIDERS[provider]["model"],
                        preflight=preflight,
                        binding=binding,
                        pilot_job=lambda prepared=prepared: prepared,
                    )
                    runs[(root.resolve(), provider)] = run
                    auth = root / "authorizations"
                    _private_directory(auth)
                    receipt = {
                        "provider": provider,
                        "model": run.model,
                        "sourceRevision": preflight["sourceRevision"],
                        "preflightSha256": binding["preflight_sha256"],
                        "providerCredentialSha256": "sha256:"
                        + ("6" if provider == "deepseek" else "7") * 64,
                        "providerControl": _provider_control(provider, replication),
                        "verification": "operator_attested",
                        "benchmarkStartAuthorized": True,
                    }
                    path = auth / f"{provider}.json"
                    path.write_bytes(canonical_json(receipt))
                    os.chmod(path, 0o600)

            with patch.object(
                launcher._paired.LiveRouteRun,
                "open",
                side_effect=lambda root, provider: runs[(root.resolve(), provider)],
            ):
                plan = launcher.build_plan(screen, mirror)
                receipt_path = mirror / "authorizations" / "zai.json"
                receipt = json.loads(receipt_path.read_text())
                receipt["providerCredentialSha256"] = "sha256:" + "8" * 64
                receipt_path.write_bytes(canonical_json(receipt))
                with self.assertRaisesRegex(
                    paired.IntegrityError, "authorization differs across roots"
                ):
                    launcher.build_plan(screen, mirror)

        self.assertEqual(len(plan.slots), 40)
        self.assertEqual(
            [
                (
                    plan.slots[index].ordinal,
                    plan.slots[index].replication,
                    plan.slots[index].provider,
                )
                for index in (0, 9, 10, 19, 20, 29, 30, 39)
            ],
            [
                (1, "screen-v1", "deepseek"),
                (10, "screen-v1", "deepseek"),
                (11, "screen-v1", "zai"),
                (20, "screen-v1", "zai"),
                (21, "mirror-v1", "deepseek"),
                (30, "mirror-v1", "deepseek"),
                (31, "mirror-v1", "zai"),
                (40, "mirror-v1", "zai"),
            ],
        )
        self.assertEqual(plan.value["taskOrder"], list(paired._TASKS))
        self.assertEqual(
            set(plan.value["providerControlIdentities"]), {"deepseek", "zai"}
        )
        self.assertEqual(
            plan.value["providerControlIdentities"]["zai"]["model"],
            paired._PROVIDERS["zai"]["model"],
        )

    def test_checkpoint_is_required_before_the_next_trial_and_caps_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = _plan(root, 21)

            def validate(slot, _job, result):
                lock = {"ordinal": slot.ordinal}
                return {
                    "telemetryComplete": True,
                    "relayPublicationGate": {"ok": True},
                    "provider": slot.provider,
                    "model": slot.model,
                    "replication": slot.replication,
                    "providerControlIdentity": _identity(slot.provider),
                    "task": slot.task,
                    "variant": slot.variant,
                    "tokens": {"output_tokens": 50_000},
                    "lock": lock,
                    "trialId": f"id-{slot.ordinal}",
                    "trialName": result.trial_name,
                    "chainHead": "sha256:" + f"{slot.ordinal:064x}",
                }

            controller = launcher.CampaignController(plan, validate)
            with controller:
                first = controller.before_create(plan.slots[0], _job())
                self.assertTrue(first.startswith("sha256:"))
                with self.assertRaisesRegex(paired.IntegrityError, "previous ordinal"):
                    controller.before_create(plan.slots[1], _job())
                for slot in plan.slots[:20]:
                    if slot.ordinal > 1:
                        controller.before_create(slot, _job())
                    controller.after_result(slot, _job(), _result(slot.ordinal))
                with self.assertRaisesRegex(
                    paired.IntegrityError, "output token limit"
                ):
                    controller.before_create(plan.slots[20], _job())
            self.assertFalse(controller._admission_path(plan.slots[20]).exists())
            stop = json.loads((controller.root / "stop.json").read_text())
            self.assertEqual(stop["reason"], "deepseek_output_token_limit_reached")

    def test_exact_provider_and_campaign_caps_complete_the_final_slot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            providers = (
                ["deepseek"] * 10 + ["zai"] * 10 + ["deepseek"] * 10 + ["zai"] * 10
            )
            slots = tuple(
                _slot(root, ordinal, provider)
                for ordinal, provider in enumerate(providers, start=1)
            )
            value = {
                "schemaVersion": 1,
                "proofClass": "test-plan",
                "providerControlIdentities": {
                    provider: _identity(provider)
                    for provider in launcher._PROVIDER_SEQUENCE
                },
                "slots": [slot.public() for slot in slots],
            }
            plan = launcher.PilotPlan(slots, value, paired._digest(value))

            def validate(slot, _job, result):
                return {
                    "telemetryComplete": True,
                    "relayPublicationGate": {"ok": True},
                    "provider": slot.provider,
                    "model": slot.model,
                    "replication": slot.replication,
                    "providerControlIdentity": _identity(slot.provider),
                    "task": slot.task,
                    "variant": slot.variant,
                    "tokens": {"output_tokens": 50_000},
                    "lock": {"ordinal": slot.ordinal},
                    "trialId": f"id-{slot.ordinal}",
                    "trialName": result.trial_name,
                    "chainHead": "sha256:" + f"{slot.ordinal:064x}",
                }

            controller = launcher.CampaignController(plan, validate)
            with controller:
                for slot in slots:
                    controller.before_create(slot, _job())
                    controller.after_result(slot, _job(), _result(slot.ordinal))
                complete = controller.complete()

            self.assertEqual(
                complete["providerOutputTokens"],
                {"deepseek": 1_000_000, "zai": 1_000_000},
            )
            self.assertEqual(complete["campaignOutputTokens"], 2_000_000)
            self.assertFalse((controller.root / "stop.json").exists())

    def test_checkpoint_binds_the_sealed_attempt_to_the_planned_slot(self) -> None:
        mutations = {
            "provider": "zai",
            "model": "wrong-model",
            "replication": "mirror-v1",
            "task": paired._TASKS[1],
            "variant": "verify-instruction-v1",
            "providerControlIdentity": {"provider": "deepseek", "model": "wrong"},
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as raw:
                plan = _plan(Path(raw), 1)
                slot = plan.slots[0]

                def validate(
                    _slot,
                    _job,
                    result,
                    planned_slot=slot,
                    mutation_field=field,
                    mutation_value=replacement,
                ):
                    attempt = {
                        "telemetryComplete": True,
                        "relayPublicationGate": {"ok": True},
                        "provider": planned_slot.provider,
                        "model": planned_slot.model,
                        "replication": planned_slot.replication,
                        "providerControlIdentity": _identity(planned_slot.provider),
                        "task": planned_slot.task,
                        "variant": planned_slot.variant,
                        "tokens": {"output_tokens": 1},
                        "lock": {"ordinal": planned_slot.ordinal},
                        "trialId": "trial-id",
                        "trialName": result.trial_name,
                        "chainHead": "sha256:" + "4" * 64,
                    }
                    attempt[mutation_field] = mutation_value
                    return attempt

                controller = launcher.CampaignController(plan, validate)
                with controller:
                    controller.before_create(slot, _job())
                    with self.assertRaisesRegex(
                        paired.IntegrityError, "complete sealed checkpoint"
                    ):
                        controller.after_result(slot, _job(), _result(1))
                self.assertFalse(controller._checkpoint_path(1).exists())

    def test_admission_without_a_claim_is_resumable_but_claimed_slot_is_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = _plan(root, 2)
            first = launcher.CampaignController(plan)
            with first:
                admission = first.before_create(plan.slots[0], _job())
            resumed = launcher.CampaignController(plan)
            with resumed:
                self.assertEqual(
                    resumed.before_create(plan.slots[0], _job()), admission
                )
            resumed._claim_path(plan.slots[0]).write_text("claimed")
            stopped = launcher.CampaignController(plan)
            with (
                stopped,
                self.assertRaisesRegex(paired.IntegrityError, "cannot be rerun"),
            ):
                stopped.before_create(plan.slots[0], _job())

    def test_completed_trial_is_checkpointed_on_crash_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = _plan(root, 1)
            slot = plan.slots[0]

            def validate(validated_slot, _job, result):
                return {
                    "telemetryComplete": True,
                    "relayPublicationGate": {"ok": True},
                    "provider": validated_slot.provider,
                    "model": validated_slot.model,
                    "replication": validated_slot.replication,
                    "providerControlIdentity": _identity(validated_slot.provider),
                    "task": validated_slot.task,
                    "variant": validated_slot.variant,
                    "tokens": {"output_tokens": 7},
                    "lock": {"ordinal": 1},
                    "trialId": "recovered-id",
                    "trialName": result.trial_name,
                    "chainHead": "sha256:" + "4" * 64,
                }

            controller = launcher.CampaignController(plan, validate)
            job = _job()
            with controller:
                controller.before_create(slot, job)
            controller._claim_path(slot).write_text("claimed")
            config = SimpleNamespace(
                task=SimpleNamespace(path=root / "tasks" / slot.task.rsplit("/", 1)[1]),
                agent=SimpleNamespace(name=paired._VARIANTS[slot.variant]["name"]),
            )
            job._existing_trial_configs = [config]
            job._existing_trial_results = [_result(1)]
            resumed = launcher.CampaignController(plan, validate)
            with resumed:
                resumed.reconcile_job(plan.slots, job)
            checkpoint = json.loads(resumed._checkpoint_path(1).read_text())
            self.assertEqual(checkpoint["outputTokens"], 7)
            self.assertEqual(checkpoint["trialId"], "recovered-id")

    def test_queue_serializes_trial_create_and_never_retries(self) -> None:
        events: list[str] = []
        configs = [SimpleNamespace(marker=1), SimpleNamespace(marker=2)]
        slots = [SimpleNamespace(ordinal=1), SimpleNamespace(ordinal=2)]

        class Controller:
            def slot_for_config(self, _slots, config):
                return slots[config.marker - 1]

            def before_create(self, slot, _job):
                events.append(f"before-{slot.ordinal}")
                return "sha256:" + "1" * 64

            def after_result(self, slot, _job, _result):
                events.append(f"after-{slot.ordinal}")

        class Trial:
            def __init__(self, marker, config):
                self.marker = marker
                self.config = config

            def add_hook(self, *_):
                return None

            async def run(self):
                events.append(f"run-{self.marker}")
                return SimpleNamespace(trial_name=f"trial-{self.marker}")

        async def factory(config):
            events.append(f"create-{config.marker}")
            config.agent = SimpleNamespace(n_concurrent=None)
            return Trial(config.marker, config)

        job = SimpleNamespace(
            config=SimpleNamespace(retry=RetryConfig(max_retries=0)),
            _trial_queue=SimpleNamespace(_hooks={event: [] for event in TrialEvent}),
        )
        queue = launcher.SequentialTrialQueue(
            job, tuple(slots), Controller(), trial_factory=factory
        )

        async def run() -> None:
            await asyncio.gather(*queue.submit_batch(configs))

        asyncio.run(run())
        self.assertEqual(
            events,
            [
                "before-1",
                "create-1",
                "run-1",
                "after-1",
                "before-2",
                "create-2",
                "run-2",
                "after-2",
            ],
        )

    def test_campaign_dispatches_each_job_from_its_prepared_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            screen = parent / "screen"
            mirror = parent / "mirror"
            for root in (screen, mirror):
                (root / "source").mkdir(parents=True)
            groups = tuple(
                tuple(
                    SimpleNamespace(
                        run=SimpleNamespace(run_dir=root, binding={}),
                        prepared=SimpleNamespace(run_dir=root, binding={}),
                    )
                    for _ in range(10)
                )
                for root in (screen, screen, mirror, mirror)
            )
            plan = SimpleNamespace(groups=lambda: groups)

            class Controller:
                lock_descriptor = 17

                def __init__(self, _plan):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    pass

                def complete(self):
                    return {"completedTrials": 40}

            runner = AsyncMock()
            with (
                patch.object(launcher, "build_plan", return_value=plan),
                patch.object(launcher, "CampaignController", Controller),
                patch.object(launcher, "_run_group_process", runner),
            ):
                result = asyncio.run(launcher.run_campaign(screen, mirror))

        self.assertEqual(result, {"completedTrials": 40})
        self.assertEqual(
            [call.args[3] for call in runner.await_args_list],
            [
                (screen / "source").resolve(),
                (screen / "source").resolve(),
                (mirror / "source").resolve(),
                (mirror / "source").resolve(),
            ],
        )
        self.assertEqual(
            [call.args[2] for call in runner.await_args_list], [0, 1, 2, 3]
        )
        self.assertEqual({call.args[4] for call in runner.await_args_list}, {17})

    def test_group_subprocess_binds_python_and_environment_to_one_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            screen = parent / "screen"
            mirror = parent / "mirror"
            source = mirror / "source"
            source.mkdir(parents=True)
            process = SimpleNamespace(wait=AsyncMock(return_value=0))
            create = AsyncMock(return_value=process)
            with patch.object(asyncio, "create_subprocess_exec", create):
                asyncio.run(launcher._run_group_process(screen, mirror, 2, source, 19))
            args = create.await_args.args
            kwargs = create.await_args.kwargs

        self.assertEqual(
            args[1:],
            (
                "-m",
                "benchmarks.terminal_bench.pilot_launcher",
                str(screen),
                str(mirror),
                "--group-index",
                "2",
                "--lock-fd",
                "19",
            ),
        )
        self.assertEqual(kwargs["cwd"], source)
        self.assertEqual(kwargs["env"]["OPEN_AGENT_LAB_REPO_ROOT"], str(source))
        self.assertEqual(kwargs["env"]["PYTHONPATH"], str(source))
        self.assertEqual(kwargs["env"]["PYTHONSAFEPATH"], "1")
        self.assertEqual(kwargs["pass_fds"], (19,))

        failed = SimpleNamespace(wait=AsyncMock(return_value=7))
        with (
            patch.object(
                asyncio, "create_subprocess_exec", AsyncMock(return_value=failed)
            ),
            self.assertRaisesRegex(paired.IntegrityError, "logical pilot job 3"),
        ):
            asyncio.run(launcher._run_group_process(screen, mirror, 2, source, 19))

    def test_mirror_group_rejects_screen_identity_binding_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw).resolve()
            screen = parent / "screen"
            mirror = parent / "mirror"
            for root in (screen, mirror):
                (root / "source").mkdir(parents=True)

            def plan(slot):
                groups = tuple((slot,) for _ in range(4))
                return SimpleNamespace(groups=lambda: groups)

            run = SimpleNamespace(run_dir=mirror, binding={"root": "mirror"})
            prepared = SimpleNamespace(
                run_dir=mirror, binding={"root": "mirror"}, config={}
            )
            valid = SimpleNamespace(
                replication="mirror-v1",
                provider="deepseek",
                run=run,
                prepared=prepared,
            )
            mutations = {
                "screen group": SimpleNamespace(
                    **{**vars(valid), "replication": "screen-v1"}
                ),
                "screen binding": SimpleNamespace(
                    **{
                        **vars(valid),
                        "prepared": SimpleNamespace(
                            run_dir=mirror,
                            binding={"root": "screen"},
                            config={},
                        ),
                    }
                ),
                "screen config": SimpleNamespace(
                    **{
                        **vars(valid),
                        "prepared": SimpleNamespace(
                            run_dir=screen,
                            binding=run.binding,
                            config={},
                        ),
                    }
                ),
            }
            create = AsyncMock()
            for label, slot in mutations.items():
                with (
                    self.subTest(label=label),
                    patch.object(launcher, "build_plan", return_value=plan(slot)),
                    patch.object(launcher.Job, "create", create),
                    self.assertRaisesRegex(
                        paired.IntegrityError, "identity drifted|spans prepared roots"
                    ),
                ):
                    asyncio.run(launcher._run_group(screen, mirror, 2, -1))
            create.assert_not_awaited()

    def test_wrong_child_module_or_environment_fails_before_job_create(self) -> None:
        module_root = Path(launcher.__file__).resolve().parents[2]
        slot = SimpleNamespace(replication="mirror-v1", provider="deepseek")
        groups = tuple((slot,) for _ in range(4))
        plan = SimpleNamespace(groups=lambda: groups)
        create = AsyncMock()
        with tempfile.TemporaryDirectory() as raw:
            other = Path(raw).resolve()
            cases = ((other, other), (module_root, other))
            for source, configured in cases:
                with (
                    self.subTest(source=source, configured=configured),
                    patch.object(launcher, "build_plan", return_value=plan),
                    patch.object(launcher, "_group_source", return_value=source),
                    patch.object(launcher.Job, "create", create),
                    patch.dict(
                        os.environ, {"OPEN_AGENT_LAB_REPO_ROOT": str(configured)}
                    ),
                    self.assertRaisesRegex(
                        paired.IntegrityError, "loaded another prepared source"
                    ),
                ):
                    asyncio.run(launcher._run_group(other, other, 2, -1))
        create.assert_not_awaited()

    def test_mirror_source_failure_preserves_twenty_checkpoint_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / "source").mkdir()
            original = _plan(root, 40)
            slots = list(original.slots)
            for index in range(20, 30):
                slot = slots[index]
                slots[index] = launcher.PilotSlot(
                    slot.ordinal,
                    "mirror-v1",
                    slot.provider,
                    slot.model,
                    slot.task,
                    slot.variant,
                    slot.trial_lock_sha256,
                    slot.run,
                    slot.prepared,
                )
            plan = launcher.PilotPlan(tuple(slots), original.value, original.sha256)

            def validate(slot, _job, result):
                return {
                    "telemetryComplete": True,
                    "relayPublicationGate": {"ok": True},
                    "provider": slot.provider,
                    "model": slot.model,
                    "replication": slot.replication,
                    "providerControlIdentity": _identity(slot.provider),
                    "task": slot.task,
                    "variant": slot.variant,
                    "tokens": {"output_tokens": 1},
                    "lock": {"ordinal": slot.ordinal},
                    "trialId": f"id-{slot.ordinal}",
                    "trialName": result.trial_name,
                    "chainHead": "sha256:" + f"{slot.ordinal:064x}",
                }

            controller = launcher.CampaignController(plan, validate)
            with controller:
                for slot in plan.slots[:20]:
                    controller.before_create(slot, _job())
                    controller.after_result(slot, _job(), _result(slot.ordinal))

            create = AsyncMock()
            with (
                patch.object(launcher, "build_plan", return_value=plan),
                patch.object(launcher.Job, "create", create),
                patch.dict(
                    os.environ,
                    {"OPEN_AGENT_LAB_REPO_ROOT": str(root / "source")},
                ),
                self.assertRaisesRegex(
                    paired.IntegrityError, "loaded another prepared source"
                ),
            ):
                asyncio.run(launcher._run_group(root, root, 2, -1))

            ordinal_21 = plan.slots[20]
            resumed = launcher.CampaignController(plan, validate)
            prefix, _, _ = resumed._prefix()
            self.assertEqual(len(prefix), 20)
            self.assertFalse(resumed._admission_path(ordinal_21).exists())
            self.assertFalse(resumed._claim_path(ordinal_21).exists())
            self.assertFalse(resumed._checkpoint_path(21).exists())
            create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
