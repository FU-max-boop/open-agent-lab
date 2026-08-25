"""Run the frozen 40-trial pilot through one fail-closed sequential launcher."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import stat
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self

from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult
from harbor.trial.hooks import TrialEvent
from harbor.trial.queue import TrialQueue

from . import live_route_probe as _live_route_probe
from . import paired_results as _paired
from .experiment_contract import (
    EXPERIMENT_ID,
    SCORED_CAMPAIGN_OUTPUT_TOKEN_LIMIT,
    SCORED_PROVIDER_OUTPUT_TOKEN_LIMIT,
    SCORED_SLOT_OUTPUT_TOKEN_LIMIT,
    canonical_json,
    digest_bytes,
    is_digest,
    is_strict_int,
    provider_control_identity,
)

_SCHEMA_VERSION = 1
_PLAN_PROOF = "sequential-pilot-plan-v1"
_ADMISSION_PROOF = "sequential-pilot-trial-admission-v1"
_CHECKPOINT_PROOF = "sequential-pilot-trial-checkpoint-v1"
_STOP_PROOF = "sequential-pilot-stop-v1"
_COMPLETE_PROOF = "sequential-pilot-complete-v1"
_CAMPAIGN_DIR = "sequential-pilot-v1"
_PROVIDER_SEQUENCE = ("deepseek", "zai")
_REPLICATION_SEQUENCE = ("screen-v1", "mirror-v1")
_CHECKPOINT_FIELDS = {
    "schemaVersion",
    "proofClass",
    "experimentId",
    "planSha256",
    "ordinal",
    "replicationId",
    "provider",
    "model",
    "providerControlIdentitySha256",
    "task",
    "variant",
    "admissionSha256",
    "trialId",
    "trialName",
    "trialLockSha256",
    "relayChainHead",
    "outputTokens",
    "providerOutputTokens",
    "campaignOutputTokens",
    "decision",
    "reason",
    "completedAt",
}


@dataclass(frozen=True, slots=True)
class PilotSlot:
    ordinal: int
    replication: str
    provider: str
    model: str
    task: str
    variant: str
    trial_lock_sha256: str
    run: _paired.LiveRouteRun
    prepared: _paired.PreparedJob

    def public(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "replicationId": self.replication,
            "provider": self.provider,
            "model": self.model,
            "task": self.task,
            "variant": self.variant,
            "preflightSha256": self.run.binding["preflight_sha256"],
            "configSha256": self.prepared.entry["configSha256"],
            "trialLockSha256": self.trial_lock_sha256,
        }


@dataclass(frozen=True, slots=True)
class PilotPlan:
    slots: tuple[PilotSlot, ...]
    value: dict[str, object]
    sha256: str

    def groups(self) -> tuple[tuple[PilotSlot, ...], ...]:
        return tuple(
            tuple(self.slots[offset : offset + 10]) for offset in range(0, 40, 10)
        )

    def provider_identity(self, provider: str) -> dict[str, object]:
        identities = self.value.get("providerControlIdentities")
        if not isinstance(identities, dict) or not isinstance(
            identities.get(provider), dict
        ):
            raise _paired.IntegrityError("pilot plan lacks a provider identity")
        return identities[provider]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _load_canonical(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        info = path.lstat()
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise _paired.IntegrityError(f"{label} is unavailable or invalid") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not isinstance(value, dict)
        or raw != canonical_json(value)
    ):
        raise _paired.IntegrityError(f"{label} is not canonical")
    return value, raw


def _write_once(path: Path, value: dict[str, object]) -> bytes:
    raw = canonical_json(value)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError:
        existing, existing_raw = _load_canonical(path, path.name)
        if existing != value:
            raise _paired.IntegrityError(f"{path.name} already exists with drift")
        return existing_raw
    try:
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return raw


def _authorization_is_published(
    run: _paired.LiveRouteRun,
) -> dict[str, object]:
    path = run.run_dir / "authorizations" / f"{run.provider}.json"
    receipt, _ = _load_canonical(path, "pilot authorization receipt")
    info = path.lstat()
    if (
        stat.S_IMODE(info.st_mode) != 0o600
        or receipt.get("provider") != run.provider
        or receipt.get("model") != run.model
        or receipt.get("sourceRevision") != run.preflight["sourceRevision"]
        or receipt.get("preflightSha256") != run.binding["preflight_sha256"]
        or receipt.get("benchmarkStartAuthorized") is not True
        or receipt.get("verification") != "operator_attested"
    ):
        raise _paired.IntegrityError("pilot authorization receipt drifted")
    try:
        return provider_control_identity(
            receipt.get("providerControl"),
            run.provider,
            receipt.get("model"),
            receipt.get("providerCredentialSha256"),
        )
    except (TypeError, ValueError) as error:
        raise _paired.IntegrityError(
            "pilot authorization stable identity is invalid"
        ) from error


def _remember_provider_identity(
    identities: dict[str, dict[str, object]],
    provider: str,
    identity: dict[str, object],
) -> None:
    previous = identities.setdefault(provider, identity)
    if canonical_json(previous) != canonical_json(identity):
        raise _paired.IntegrityError(
            f"{provider} provider authorization differs across roots"
        )


def build_plan(screen: Path, mirror: Path) -> PilotPlan:
    roots = {
        "screen-v1": screen.expanduser().resolve(strict=True),
        "mirror-v1": mirror.expanduser().resolve(strict=True),
    }
    slots: list[PilotSlot] = []
    runs: list[dict[str, object]] = []
    provider_identities: dict[str, dict[str, object]] = {}
    source_revision: str | None = None
    ordinal = 1
    for replication in _REPLICATION_SEQUENCE:
        root = roots[replication]
        provider_runs = {
            provider: _paired.LiveRouteRun.open(root, provider)
            for provider in _PROVIDER_SEQUENCE
        }
        first = provider_runs[_PROVIDER_SEQUENCE[0]]
        if first.preflight.get("replicationId") != replication:
            raise _paired.IntegrityError("prepared roots are in the wrong order")
        revision = str(first.preflight["sourceRevision"])
        if source_revision is None:
            source_revision = revision
        elif source_revision != revision:
            raise _paired.IntegrityError("prepared roots use different revisions")
        runs.append(
            {
                "replicationId": replication,
                "preflightSha256": first.binding["preflight_sha256"],
                "sourceRevision": revision,
            }
        )
        for provider in _PROVIDER_SEQUENCE:
            run = provider_runs[provider]
            if run.preflight != first.preflight:
                raise _paired.IntegrityError("provider views disagree within a root")
            identity = _authorization_is_published(run)
            _remember_provider_identity(provider_identities, provider, identity)
            prepared = run.pilot_job()
            order = prepared.entry.get("armOrder")
            locks = prepared.expected_trial_locks()
            expected_pairs = [
                (task, variant) for task in _paired._TASKS for variant in order
            ]
            if len(locks) != 10 or len(expected_pairs) != 10:
                raise _paired.IntegrityError(
                    "prepared job is not five tasks by two arms"
                )
            for (task, variant), lock in zip(expected_pairs, locks, strict=True):
                slots.append(
                    PilotSlot(
                        ordinal,
                        replication,
                        provider,
                        run.model,
                        task,
                        variant,
                        _paired._digest(lock),
                        run,
                        prepared,
                    )
                )
                ordinal += 1
    if len(slots) != 40:
        raise _paired.IntegrityError("pilot plan must contain exactly 40 trials")
    value: dict[str, object] = {
        "schemaVersion": _SCHEMA_VERSION,
        "proofClass": _PLAN_PROOF,
        "experimentId": EXPERIMENT_ID,
        "sourceRevision": source_revision,
        "runs": runs,
        "taskOrder": list(_paired._TASKS),
        "armOrderByProvider": {
            replication: {
                provider: list(
                    next(
                        slot.prepared.entry["armOrder"]
                        for slot in slots
                        if slot.replication == replication and slot.provider == provider
                    )
                )
                for provider in _PROVIDER_SEQUENCE
            }
            for replication in _REPLICATION_SEQUENCE
        },
        "providerControlIdentities": provider_identities,
        "outputTokenLimits": {
            "slot": SCORED_SLOT_OUTPUT_TOKEN_LIMIT,
            "provider": SCORED_PROVIDER_OUTPUT_TOKEN_LIMIT,
            "campaign": SCORED_CAMPAIGN_OUTPUT_TOKEN_LIMIT,
        },
        "slots": [slot.public() for slot in slots],
    }
    return PilotPlan(tuple(slots), value, _paired._digest(value))


def _variant(config: TrialConfig) -> str:
    matches = [
        variant
        for variant, spec in _paired._VARIANTS.items()
        if config.agent.name == spec["name"]
    ]
    if len(matches) != 1:
        raise _paired.IntegrityError("scheduled agent variant drifted")
    return matches[0]


def _task(config: TrialConfig) -> str:
    if config.task.path is None:
        raise _paired.IntegrityError("scheduled task path is unavailable")
    matches = [
        task
        for task in _paired._TASKS
        if task.rsplit("/", 1)[1] == config.task.path.name
    ]
    if len(matches) != 1:
        raise _paired.IntegrityError("scheduled task identity drifted")
    return matches[0]


class CampaignController:
    """Persist admissions and sealed checkpoints for one immutable plan."""

    def __init__(
        self,
        plan: PilotPlan,
        attempt_validator: Callable[[PilotSlot, Job, TrialResult], dict[str, Any]]
        | None = None,
    ) -> None:
        self.plan = plan
        self.root = plan.slots[0].run.run_dir / _CAMPAIGN_DIR
        self.checkpoints = self.root / "checkpoints"
        self._attempt_validator = attempt_validator or self._validate_attempt
        self._lock_descriptor: int | None = None
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.checkpoints.mkdir(mode=0o700, exist_ok=True)
        for path in (self.root, self.checkpoints):
            if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise _paired.IntegrityError("scheduler directories must be private")
        _write_once(self.root / "plan.json", plan.value)

    def __enter__(self) -> Self:
        lock = self.root / "launcher.lock"
        self._lock_descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self._lock_descriptor)
            self._lock_descriptor = None
            raise _paired.IntegrityError(
                "another pilot launcher owns this plan"
            ) from error
        return self

    def __exit__(self, *_: object) -> None:
        if self._lock_descriptor is not None:
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
            os.close(self._lock_descriptor)
            self._lock_descriptor = None

    def slot_for_config(
        self, slots: tuple[PilotSlot, ...], config: TrialConfig
    ) -> PilotSlot:
        matches = [
            slot
            for slot in slots
            if slot.task == _task(config) and slot.variant == _variant(config)
        ]
        if len(matches) != 1:
            raise _paired.IntegrityError("Harbor trial is absent from the frozen plan")
        return matches[0]

    def _checkpoint_path(self, ordinal: int) -> Path:
        return self.checkpoints / f"{ordinal:02d}.json"

    def _admission_path(self, slot: PilotSlot) -> Path:
        return _live_route_probe.pilot_scheduler_admission_path(
            slot.run.run_dir, slot.provider, slot.trial_lock_sha256
        )

    def _claim_path(self, slot: PilotSlot) -> Path:
        return (
            slot.run.run_dir
            / "authorizations"
            / _paired.relay_claim_name(slot.provider, "pilot", slot.trial_lock_sha256)
        )

    @staticmethod
    def _decision(
        slot: PilotSlot, provider_tokens: int, campaign_tokens: int
    ) -> tuple[str, str | None]:
        if campaign_tokens > SCORED_CAMPAIGN_OUTPUT_TOKEN_LIMIT:
            return "stop", "campaign_output_token_limit_reached"
        if provider_tokens > SCORED_PROVIDER_OUTPUT_TOKEN_LIMIT:
            return "stop", f"{slot.provider}_output_token_limit_reached"
        if slot.ordinal == 40:
            return "complete", None
        return "go", None

    def _prefix(self) -> tuple[list[dict[str, Any]], dict[str, int], int]:
        values: list[dict[str, Any]] = []
        provider_totals = {provider: 0 for provider in _PROVIDER_SEQUENCE}
        campaign_total = 0
        previous_checkpoint: str | None = None
        gap = False
        for slot in self.plan.slots:
            path = self._checkpoint_path(slot.ordinal)
            if not path.exists():
                gap = True
                continue
            if gap:
                raise _paired.IntegrityError("scheduler checkpoints are not contiguous")
            value, raw = _load_canonical(path, "scheduler checkpoint")
            admission, admission_raw = _load_canonical(
                self._admission_path(slot), "scheduler admission"
            )
            output = value.get("outputTokens")
            if (
                not is_strict_int(output)
                or not 0 <= output <= SCORED_SLOT_OUTPUT_TOKEN_LIMIT
            ):
                raise _paired.IntegrityError("scheduler output accounting drifted")
            provider_totals[slot.provider] += output
            campaign_total += output
            decision, reason = self._decision(
                slot, provider_totals[slot.provider], campaign_total
            )
            expected = {
                "schemaVersion": _SCHEMA_VERSION,
                "proofClass": _CHECKPOINT_PROOF,
                "experimentId": EXPERIMENT_ID,
                "planSha256": self.plan.sha256,
                "ordinal": slot.ordinal,
                "replicationId": slot.replication,
                "provider": slot.provider,
                "model": slot.model,
                "providerControlIdentitySha256": _paired._digest(
                    self.plan.provider_identity(slot.provider)
                ),
                "task": slot.task,
                "variant": slot.variant,
                "admissionSha256": digest_bytes(admission_raw),
                "trialLockSha256": slot.trial_lock_sha256,
                "outputTokens": output,
                "providerOutputTokens": provider_totals[slot.provider],
                "campaignOutputTokens": campaign_total,
                "decision": decision,
                "reason": reason,
            }
            if (
                set(value) != _CHECKPOINT_FIELDS
                or any(value.get(key) != item for key, item in expected.items())
                or not is_digest(value.get("relayChainHead"))
                or not isinstance(value.get("trialId"), str)
                or not isinstance(value.get("trialName"), str)
                or not isinstance(value.get("completedAt"), str)
                or admission.get("previousCheckpointSha256") != previous_checkpoint
            ):
                raise _paired.IntegrityError("scheduler checkpoint drifted")
            values.append(value)
            previous_checkpoint = digest_bytes(raw)
            if decision != "go" and slot.ordinal != 40:
                gap = True
        return values, provider_totals, campaign_total

    def _stop(self, ordinal: int, reason: str) -> None:
        _write_once(
            self.root / "stop.json",
            {
                "schemaVersion": _SCHEMA_VERSION,
                "proofClass": _STOP_PROOF,
                "experimentId": EXPERIMENT_ID,
                "planSha256": self.plan.sha256,
                "ordinal": ordinal,
                "reason": reason,
                "stoppedAt": _utc_now(),
            },
        )

    def before_create(self, slot: PilotSlot, job: Job) -> str:
        if self._lock_descriptor is None:
            raise _paired.IntegrityError("pilot launcher lock is not held")
        if (self.root / "stop.json").exists() or (self.root / "complete.json").exists():
            raise _paired.IntegrityError("pilot campaign is already terminal")
        prefix, provider_totals, campaign_total = self._prefix()
        if slot.ordinal != len(prefix) + 1:
            raise _paired.IntegrityError("previous ordinal lacks a GO checkpoint")
        if (
            provider_totals[slot.provider] >= SCORED_PROVIDER_OUTPUT_TOKEN_LIMIT
            or campaign_total >= SCORED_CAMPAIGN_OUTPUT_TOKEN_LIMIT
        ):
            reason = (
                "campaign_output_token_limit_reached"
                if campaign_total >= SCORED_CAMPAIGN_OUTPUT_TOKEN_LIMIT
                else f"{slot.provider}_output_token_limit_reached"
            )
            self._stop(slot.ordinal, reason)
            raise _paired.IntegrityError("output token limit forbids the next trial")
        previous = (
            digest_bytes(self._checkpoint_path(slot.ordinal - 1).read_bytes())
            if slot.ordinal > 1
            else None
        )
        admission = {
            "schemaVersion": _SCHEMA_VERSION,
            "proofClass": _ADMISSION_PROOF,
            "experimentId": EXPERIMENT_ID,
            "planSha256": self.plan.sha256,
            "ordinal": slot.ordinal,
            "replicationId": slot.replication,
            "provider": slot.provider,
            "model": slot.model,
            "providerControlIdentitySha256": _paired._digest(
                self.plan.provider_identity(slot.provider)
            ),
            "preflightSha256": slot.run.binding["preflight_sha256"],
            "jobId": str(job.id),
            "jobDir": str(slot.prepared.job_dir),
            "trialLockSha256": slot.trial_lock_sha256,
            "previousCheckpointSha256": previous,
            "admittedAt": _utc_now(),
        }
        path = self._admission_path(slot)
        if path.exists():
            existing, raw = _load_canonical(path, "scheduler admission")
            comparable = {**admission, "admittedAt": existing.get("admittedAt")}
            if existing != comparable:
                raise _paired.IntegrityError("scheduler admission drifted")
            if self._claim_path(slot).exists():
                self._stop(slot.ordinal, "claimed_trial_has_no_checkpoint")
                raise _paired.IntegrityError("claimed trial cannot be rerun")
        else:
            raw = _write_once(path, admission)
        return digest_bytes(raw)

    def _validate_attempt(
        self, slot: PilotSlot, job: Job, result: TrialResult
    ) -> dict[str, Any]:
        trial_dir = slot.prepared.job_dir / result.trial_name
        return _paired._attempt(
            slot.run.run_dir,
            slot.prepared.job_dir,
            trial_dir,
            slot.provider,
            slot.model,
            slot.run.probe,
            slot.run.pilot,
            slot.run.preflight,
            _paired._digest(slot.run.preflight),
            set(_paired._TASKS),
            job.config,
            job.id,
            slot.prepared.compose_path,
            slot.prepared.compose_sha256,
        )

    def after_result(
        self, slot: PilotSlot, job: Job, result: TrialResult
    ) -> dict[str, object]:
        prefix, provider_totals, campaign_total = self._prefix()
        if slot.ordinal != len(prefix) + 1:
            raise _paired.IntegrityError("scheduler result arrived out of order")
        try:
            attempt = self._attempt_validator(slot, job, result)
            tokens = attempt.get("tokens")
            gate = attempt.get("relayPublicationGate")
            output = tokens.get("output_tokens") if isinstance(tokens, dict) else None
            if (
                attempt.get("telemetryComplete") is not True
                or not isinstance(gate, dict)
                or gate.get("ok") is not True
                or attempt.get("provider") != slot.provider
                or attempt.get("model") != slot.model
                or attempt.get("replication") != slot.replication
                or attempt.get("task") != slot.task
                or attempt.get("variant") != slot.variant
                or not _paired._same_json(
                    attempt.get("providerControlIdentity"),
                    self.plan.provider_identity(slot.provider),
                )
                or not is_strict_int(output)
                or not 0 <= output <= SCORED_SLOT_OUTPUT_TOKEN_LIMIT
                or _paired._digest(attempt.get("lock")) != slot.trial_lock_sha256
            ):
                raise _paired.IntegrityError("trial lacks a complete sealed checkpoint")
        except (OSError, TypeError, ValueError) as error:
            self._stop(slot.ordinal, "sealed_checkpoint_validation_failed")
            raise _paired.IntegrityError(
                "trial lacks a complete sealed checkpoint"
            ) from error
        provider_total = provider_totals[slot.provider] + output
        campaign_total += output
        decision, reason = self._decision(slot, provider_total, campaign_total)
        admission_raw = self._admission_path(slot).read_bytes()
        checkpoint: dict[str, object] = {
            "schemaVersion": _SCHEMA_VERSION,
            "proofClass": _CHECKPOINT_PROOF,
            "experimentId": EXPERIMENT_ID,
            "planSha256": self.plan.sha256,
            "ordinal": slot.ordinal,
            "replicationId": slot.replication,
            "provider": slot.provider,
            "model": slot.model,
            "providerControlIdentitySha256": _paired._digest(
                self.plan.provider_identity(slot.provider)
            ),
            "task": slot.task,
            "variant": slot.variant,
            "admissionSha256": digest_bytes(admission_raw),
            "trialId": str(attempt["trialId"]),
            "trialName": str(attempt["trialName"]),
            "trialLockSha256": slot.trial_lock_sha256,
            "relayChainHead": str(attempt["chainHead"]),
            "outputTokens": output,
            "providerOutputTokens": provider_total,
            "campaignOutputTokens": campaign_total,
            "decision": decision,
            "reason": reason,
            "completedAt": _utc_now(),
        }
        _write_once(self._checkpoint_path(slot.ordinal), checkpoint)
        if decision == "stop":
            self._stop(slot.ordinal, reason or "output_token_limit_reached")
        return checkpoint

    def reconcile_job(self, slots: tuple[PilotSlot, ...], job: Job) -> None:
        existing = sorted(
            zip(job._existing_trial_configs, job._existing_trial_results, strict=True),
            key=lambda pair: self.slot_for_config(slots, pair[0]).ordinal,
        )
        for config, result in existing:
            slot = self.slot_for_config(slots, config)
            checkpoint_path = self._checkpoint_path(slot.ordinal)
            if checkpoint_path.exists():
                attempt = self._attempt_validator(slot, job, result)
                checkpoint, _ = _load_canonical(checkpoint_path, "scheduler checkpoint")
                tokens = attempt.get("tokens")
                if (
                    not isinstance(tokens, dict)
                    or tokens.get("output_tokens") != checkpoint.get("outputTokens")
                    or attempt.get("chainHead") != checkpoint.get("relayChainHead")
                    or str(attempt.get("trialId")) != checkpoint.get("trialId")
                ):
                    raise _paired.IntegrityError("completed trial checkpoint drifted")
            else:
                if not self._admission_path(slot).exists():
                    self._stop(
                        slot.ordinal, "completed_trial_lacks_scheduler_admission"
                    )
                    raise _paired.IntegrityError(
                        "completed trial bypassed the launcher"
                    )
                self.after_result(slot, job, result)

    def complete(self) -> dict[str, object]:
        prefix, provider_totals, campaign_total = self._prefix()
        if len(prefix) != 40 or prefix[-1]["decision"] != "complete":
            raise _paired.IntegrityError("pilot campaign is not complete")
        value: dict[str, object] = {
            "schemaVersion": _SCHEMA_VERSION,
            "proofClass": _COMPLETE_PROOF,
            "experimentId": EXPERIMENT_ID,
            "planSha256": self.plan.sha256,
            "completedTrials": 40,
            "providerOutputTokens": provider_totals,
            "campaignOutputTokens": campaign_total,
            "finalCheckpointSha256": digest_bytes(
                self._checkpoint_path(40).read_bytes()
            ),
            "completedAt": _utc_now(),
        }
        _write_once(self.root / "complete.json", value)
        return value


class SequentialTrialQueue(TrialQueue):
    """One-shot Harbor queue that gates every Trial.create on the prior seal."""

    def __init__(
        self,
        job: Job,
        slots: tuple[PilotSlot, ...],
        controller: CampaignController,
        trial_factory: Callable[[TrialConfig], Awaitable[Any]] | None = None,
    ) -> None:
        hooks = {event: list(job._trial_queue._hooks[event]) for event in TrialEvent}
        super().__init__(1, retry_config=job.config.retry, hooks=hooks)
        if job.config.retry.max_retries != 0:
            raise _paired.IntegrityError("scored launcher forbids Harbor retries")
        self._job = job
        self._slots = slots
        self._controller = controller
        self._trial_factory = trial_factory
        self._failure: BaseException | None = None

    async def _create_trial(self, config: TrialConfig) -> Any:
        if self._trial_factory is not None:
            return await self._trial_factory(config)
        from harbor.trial.trial import Trial

        return await Trial.create(config)

    async def _ordered(
        self,
        previous: asyncio.Event,
        finished: asyncio.Event,
        slot: PilotSlot,
        config: TrialConfig,
    ) -> TrialResult:
        await previous.wait()
        if self._failure is not None:
            raise _paired.IntegrityError(
                "an earlier pilot trial failed"
            ) from self._failure
        try:
            admission_sha256 = self._controller.before_create(slot, self._job)
            with _live_route_probe.active_pilot_scheduler_admission(admission_sha256):
                trial = await self._create_trial(config)
                self._setup_hooks(trial)
                result = await trial.run()
            self._controller.after_result(slot, self._job, result)
            return result
        except BaseException as error:
            self._failure = error
            raise
        finally:
            finished.set()

    def submit_batch(
        self, configs: list[TrialConfig]
    ) -> list[Coroutine[Any, Any, TrialResult]]:
        selected = [
            self._controller.slot_for_config(self._slots, config) for config in configs
        ]
        if selected != sorted(selected, key=lambda slot: slot.ordinal):
            raise _paired.IntegrityError("Harbor trial order drifted")
        first = asyncio.Event()
        first.set()
        events = [first, *(asyncio.Event() for _ in selected)]
        return [
            self._ordered(events[index], events[index + 1], slot, config)
            for index, (slot, config) in enumerate(zip(selected, configs, strict=True))
        ]


async def run_campaign(screen: Path, mirror: Path) -> dict[str, object]:
    plan = build_plan(screen, mirror)
    controller = CampaignController(plan)
    with controller:
        for slots in plan.groups():
            prepared = slots[0].prepared
            config = JobConfig.model_validate(prepared.config)
            job = await Job.create(config)
            controller.reconcile_job(slots, job)
            if job._remaining_trial_configs:
                job._trial_queue = SequentialTrialQueue(job, slots, controller)
                await job.run()
            _paired._validate_job_completion(prepared.job_dir, prepared.config, 10)
        return controller.complete()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("screen", type=Path)
    parser.add_argument("mirror", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_campaign(arguments.screen, arguments.mirror))
    except (OSError, TypeError, ValueError) as error:
        print(f"STOP: {error}", file=os.sys.stderr)
        return 1
    print(canonical_json(result).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
