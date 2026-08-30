from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from kaggle_core.common import OpsError, event, read_json, sha256_file, write_json_atomic
from kaggle_core.decisions import check_dryness, check_submission_gate, promote_candidate, record_metric
from kaggle_core.release import build_postmortem, build_release, verify_release
from kaggle_core.runner import _docker_command, run_experiment
from kaggle_core.schema import append_event_checked, load_events, validate_event
from kaggle_core.workspace import (
    bootstrap_workspace,
    create_or_update_idea,
    ingest_data,
    migrate_v1_workspace,
    validate_workspace,
)


def metric(value: float, *, source: str = "local_cv", group: str = "cv-v1") -> dict[str, object]:
    return {
        "name": "score",
        "value": value,
        "direction": "higher",
        "source": source,
        "split": "validation",
        "scope": "overall",
        "comparable_group": group,
    }


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        bootstrap_workspace(
            self.workspace,
            slug="generic-contest",
            platform="manual",
            problem_type="custom",
            track_id="primary",
            phase_id="active",
            metric="score",
            direction="higher",
            eps=0.001,
            submission_limit=5,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure_contract(self) -> None:
        competition = read_json(self.workspace / "competition.json")
        competition["input_contract"].update(
            {
                "required_globs": ["*.csv"],
                "tabular_checks": [
                    {
                        "glob": "*.csv",
                        "format": "csv",
                        "required_columns": ["id", "target", "event_time"],
                        "primary_key": ["id"],
                        "min_rows": 2,
                        "missing_policy": "forbid",
                    }
                ],
                "temporal_checks": [
                    {"glob": "*.csv", "column": "event_time", "format": "csv"}
                ],
            }
        )
        competition["output_contract"].update(
            {
                "required_artifacts": ["prediction.csv"],
                "tabular_checks": [
                    {
                        "glob": "prediction.csv",
                        "format": "csv",
                        "exact_columns": ["id", "prediction"],
                        "primary_key": ["id"],
                        "min_rows": 2,
                    }
                ],
            }
        )
        competition["rules"]["max_business_date"] = "2026-01-31"
        competition["rules"].update(
            {
                "external_data_policy": "declared",
                "network_policy": "declared",
                "model_license_policy": "declared",
            }
        )
        write_json_atomic(self.workspace / "competition.json", competition)
        write_json_atomic(self.workspace / "rules" / "rules.json", competition["rules"])

    def ingest_fixture(self) -> dict[str, object]:
        self.configure_contract()
        data = self.root / "input" / "nested"
        data.mkdir(parents=True)
        source = data / "train.csv"
        source.write_text(
            "id,target,event_time\n1,0,2026-01-01\n2,1,2026-01-15\n",
            encoding="utf-8",
        )
        before = sha256_file(source)
        manifest = ingest_data(self.workspace, self.root / "input")
        self.assertEqual(before, sha256_file(source), "ingest must not modify raw input")
        self.assertTrue(manifest["valid"])
        self.assertEqual(manifest["file_count"], 1)
        return manifest

    def run_fixture(self, snapshot_id: str, run_id: str = "run-e2e") -> dict[str, object]:
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        helper = source / "make_prediction.py"
        helper.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "run_dir = Path(os.environ['KAGGLE_SKILL_RUN_DIR'])\n"
            "(run_dir / 'prediction.csv').write_text('id,prediction\\n1,0.25\\n2,0.75\\n', encoding='utf-8')\n"
            "(run_dir / 'stable.bin').write_bytes(b'stable-component-v1')\n",
            encoding="utf-8",
        )
        command = subprocess.list2cmdline([sys.executable, str(helper)])
        return run_experiment(
            self.workspace,
            track_id="primary",
            phase_id="active",
            data_snapshot_id=snapshot_id,
            experiment_family="baseline",
            idea_id="idea-baseline",
            command=command,
            run_id=run_id,
            source_root=source,
            artifact_paths=[Path("prediction.csv")],
        )

    def approval(
        self,
        candidate_id: str,
        artifact_sha256: str,
        intent: str,
        *,
        expired: bool = False,
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        approved_at = now - timedelta(hours=2 if expired else 1)
        expires_at = now - timedelta(hours=1) if expired else now + timedelta(hours=1)
        return {
            "approved": True,
            "candidate_id": candidate_id,
            "artifact_sha256": artifact_sha256,
            "intent": intent,
            "approver": "human-reviewer",
            "request_id": f"request-{candidate_id}",
            "approved_at": approved_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

    def gate_report(
        self,
        manifest: dict[str, object],
        candidate_id: str,
        intent: str,
        *,
        metric_passed: bool = True,
        ensemble: bool = False,
        diversity: bool | None = None,
        expired: bool = False,
    ) -> Path:
        artifact = manifest["artifacts"][0]
        artifact_path = Path(artifact["source_path"])
        gates: dict[str, bool] = {
            "format": True,
            "compliance": True,
            "artifact_integrity": True,
            "leakage": True,
            "temporal_integrity": True,
            "training_inference_boundary": True,
            "prohibited_shortcuts": True,
            "artifact_freshness": True,
            "metric": metric_passed,
        }
        if diversity is not None:
            gates["diversity"] = diversity
        if intent == "final":
            gates.update({"cold_start": True, "release": True, "final_deliverable": True})
        report: dict[str, object] = {
            "schema_version": 2,
            "candidate_id": candidate_id,
            "track_id": "primary",
            "phase_id": "active",
            "data_snapshot_id": manifest["data_snapshot_id"],
            "experiment_family": "baseline",
            "run_id": manifest["run_id"],
            "intent": intent,
            "artifact_path": artifact_path.as_posix(),
            "artifact_sha256": artifact["sha256"],
            "ensemble": ensemble,
            "automated_gates": gates,
            "human_approval": self.approval(
                candidate_id, str(artifact["sha256"]), intent, expired=expired
            ),
        }
        if intent == "probe":
            report["probe"] = {
                "information_hypothesis": "Public feedback can distinguish two validation designs.",
                "controlled_difference": "Only the validation split changes.",
                "rollback_candidate_id": "baseline-anchor",
            }
        path = self.root / f"{candidate_id}.json"
        write_json_atomic(path, report)
        return path


class StructureAndIngestTests(WorkspaceCase):
    def test_bootstrap_is_generic_and_state_is_bounded(self) -> None:
        result = validate_workspace(self.workspace, deep=True)
        self.assertTrue(result["ok"], result["errors"])
        self.assertLessEqual(len((self.workspace / "STATE.md").read_text(encoding="utf-8").splitlines()), 200)
        self.assertTrue(list((self.workspace / "reports" / "archive" / "state").glob("*.md")))
        competition = read_json(self.workspace / "competition.json")
        self.assertNotIn("market", json.dumps(competition).lower())
        self.assertNotIn("stock", json.dumps(competition).lower())

    def test_open_ended_platforms_and_problem_types(self) -> None:
        for index, problem_type in enumerate(
            ["classification", "object-detection", "forecasting", "text-generation", "simulation"]
        ):
            workspace = self.root / f"profile-{index}"
            bootstrap_workspace(
                workspace,
                slug=f"contest-{index}",
                platform="custom-evaluator",
                problem_type=problem_type,
            )
            self.assertTrue(validate_workspace(workspace)["ok"])

    def test_recursive_tabular_and_temporal_ingest_is_read_only(self) -> None:
        manifest = self.ingest_fixture()
        self.assertEqual(manifest["tabular_details"][0]["rows"], 2)
        self.assertEqual(manifest["temporal_details"][0]["violations"], 0)
        manifest_path = (
            self.workspace / "data" / "manifests" / f"{manifest['data_snapshot_id']}.json"
        )
        before_hash = sha256_file(manifest_path)
        before_events = len(load_events(self.workspace))
        repeated = ingest_data(self.workspace, self.root / "input")
        self.assertEqual(repeated["data_snapshot_id"], manifest["data_snapshot_id"])
        self.assertEqual(sha256_file(manifest_path), before_hash)
        self.assertEqual(len(load_events(self.workspace)), before_events)

    def test_temporal_and_missing_policy_reject_bad_input(self) -> None:
        self.configure_contract()
        data = self.root / "bad-input"
        data.mkdir()
        source = data / "train.csv"
        source.write_text(
            "id,target,event_time\n1,,2026-01-01\n2,1,2026-02-01\n",
            encoding="utf-8",
        )
        before = sha256_file(source)
        with self.assertRaises(OpsError) as caught:
            ingest_data(self.workspace, data)
        self.assertIn("forbidden missing values", str(caught.exception))
        self.assertIn("exceed", str(caught.exception))
        self.assertEqual(before, sha256_file(source))

    def test_ingest_detects_validator_hook_mutation(self) -> None:
        data = self.root / "hook-input"
        data.mkdir()
        source = data / "payload.txt"
        source.write_text("original", encoding="utf-8")
        hook = self.workspace / "validators" / "mutating_hook.py"
        hook.write_text(
            "def validate(source, workspace, contract):\n"
            "    (source / 'payload.txt').write_text('mutated', encoding='utf-8')\n"
            "    return {'errors': [], 'warnings': [], 'details': {}}\n",
            encoding="utf-8",
        )
        competition = read_json(self.workspace / "competition.json")
        competition["input_contract"]["validator_hooks"] = [
            {"path": "validators/mutating_hook.py", "function": "validate"}
        ]
        write_json_atomic(self.workspace / "competition.json", competition)
        with self.assertRaises(OpsError) as caught:
            ingest_data(self.workspace, data)
        self.assertIn("modified while validation hooks", str(caught.exception))
        manifest_paths = list((self.workspace / "data" / "manifests").glob("*.json"))
        self.assertEqual(len(manifest_paths), 1)
        manifest = read_json(manifest_paths[0])
        self.assertFalse(manifest["valid"])
        self.assertTrue(manifest["source_mutation_detected"])

    def test_strict_schema_and_illegal_status(self) -> None:
        competition = read_json(self.workspace / "competition.json")
        competition["unexpected"] = True
        write_json_atomic(self.workspace / "competition.json", competition)
        self.assertFalse(validate_workspace(self.workspace)["ok"])
        invalid = event(
            "run_planned",
            {"status": "imported"},
            track_id="primary",
            phase_id="active",
            data_snapshot_id="snapshot",
            run_id="run-invalid",
            idea_id="idea-baseline",
            experiment_family="baseline",
        )
        self.assertTrue(any("invalid run status" in item for item in validate_event(invalid)))

    def test_concurrent_duplicate_run_append_is_atomic(self) -> None:
        barrier = threading.Barrier(8)
        outcomes: list[str] = []
        outcome_lock = threading.Lock()

        def append_same_run() -> None:
            item = event(
                "run_planned",
                {"status": "planned"},
                track_id="primary",
                phase_id="active",
                data_snapshot_id="snapshot-concurrent",
                run_id="same-run",
                idea_id="idea-baseline",
                experiment_family="concurrency",
            )
            barrier.wait()
            try:
                append_event_checked(self.workspace, item)
                result = "ok"
            except OpsError:
                result = "rejected"
            with outcome_lock:
                outcomes.append(result)

        threads = [threading.Thread(target=append_same_run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("rejected"), 7)
        self.assertTrue(validate_workspace(self.workspace)["ok"])


class RunAndDecisionTests(WorkspaceCase):
    def test_run_resume_identity_and_manifest_seal(self) -> None:
        snapshot = self.ingest_fixture()
        manifest = self.run_fixture(str(snapshot["data_snapshot_id"]), "run-resume")
        self.assertEqual(manifest["status"], "completed")
        self.assertTrue((self.workspace / "runs" / "run-resume" / "run-manifest.sha256.json").is_file())
        source = self.root / "source"
        helper = source / "make_prediction.py"
        command = subprocess.list2cmdline([sys.executable, str(helper)])
        resumed = run_experiment(
            self.workspace,
            track_id="primary",
            phase_id="active",
            data_snapshot_id=str(snapshot["data_snapshot_id"]),
            experiment_family="baseline",
            idea_id="idea-baseline",
            command=command,
            run_id="run-resume",
            source_root=source,
            artifact_paths=[Path("prediction.csv")],
            resume=True,
        )
        self.assertTrue(resumed["resume_skipped"])
        with self.assertRaises(OpsError):
            run_experiment(
                self.workspace,
                track_id="primary",
                phase_id="active",
                data_snapshot_id=str(snapshot["data_snapshot_id"]),
                experiment_family="baseline",
                idea_id="idea-baseline",
                command=command + " --changed",
                run_id="run-resume",
                source_root=source,
                artifact_paths=[Path("prediction.csv")],
                resume=True,
            )
        with self.assertRaises(OpsError):
            self.run_fixture(str(snapshot["data_snapshot_id"]), "run-resume")

    def test_cold_start_hashes_and_controlled_byte_invariant(self) -> None:
        snapshot = self.ingest_fixture()
        first = self.run_fixture(str(snapshot["data_snapshot_id"]), "cold-start-a")
        reference = self.workspace / "stable-reference.bin"
        reference.write_bytes(b"stable-component-v1")
        source = self.root / "source"
        helper = source / "make_prediction.py"
        command = subprocess.list2cmdline([sys.executable, str(helper)])
        second = run_experiment(
            self.workspace,
            track_id="primary",
            phase_id="active",
            data_snapshot_id=str(snapshot["data_snapshot_id"]),
            experiment_family="baseline",
            idea_id="idea-baseline",
            command=command,
            run_id="cold-start-b",
            source_root=source,
            artifact_paths=[Path("prediction.csv")],
            invariants=[(Path("stable.bin"), Path("stable-reference.bin"))],
        )
        self.assertEqual(first["artifacts"][0]["sha256"], second["artifacts"][0]["sha256"])
        self.assertTrue(second["controlled_differences"][0]["passed"])
        reference.write_bytes(b"stable-component-v2")
        with self.assertRaises(OpsError):
            run_experiment(
                self.workspace,
                track_id="primary",
                phase_id="active",
                data_snapshot_id=str(snapshot["data_snapshot_id"]),
                experiment_family="baseline",
                idea_id="idea-baseline",
                command=command,
                run_id="cold-start-b",
                source_root=source,
                artifact_paths=[Path("prediction.csv")],
                invariants=[(Path("stable.bin"), Path("stable-reference.bin"))],
                resume=True,
            )
        reference.write_bytes(b"stable-component-v1")
        bad_reference = self.workspace / "bad-reference.bin"
        bad_reference.write_bytes(b"changed-component")
        with self.assertRaises(OpsError):
            run_experiment(
                self.workspace,
                track_id="primary",
                phase_id="active",
                data_snapshot_id=str(snapshot["data_snapshot_id"]),
                experiment_family="baseline",
                idea_id="idea-baseline",
                command=command,
                run_id="controlled-difference-fail",
                source_root=source,
                artifact_paths=[Path("prediction.csv")],
                invariants=[(Path("stable.bin"), Path("bad-reference.bin"))],
            )
        failed_manifest = read_json(
            self.workspace / "runs" / "controlled-difference-fail" / "run-manifest.json"
        )
        self.assertEqual(failed_manifest["status"], "invalid")

    def test_run_detects_raw_data_mutation(self) -> None:
        snapshot = self.ingest_fixture()
        source = self.root / "mutator-source"
        source.mkdir()
        helper = source / "mutate_input.py"
        helper.write_text(
            "import json, os\n"
            "from pathlib import Path\n"
            "manifest = json.loads(Path(os.environ['KAGGLE_SKILL_DATA_MANIFEST']).read_text(encoding='utf-8'))\n"
            "target = Path(manifest['source']) / manifest['files'][0]['path']\n"
            "target.write_text('corrupted', encoding='utf-8')\n",
            encoding="utf-8",
        )
        command = subprocess.list2cmdline([sys.executable, str(helper)])
        with self.assertRaises(OpsError):
            run_experiment(
                self.workspace,
                track_id="primary",
                phase_id="active",
                data_snapshot_id=str(snapshot["data_snapshot_id"]),
                experiment_family="baseline",
                idea_id="idea-baseline",
                command=command,
                run_id="run-mutates-data",
                source_root=source,
            )
        manifest = read_json(self.workspace / "runs" / "run-mutates-data" / "run-manifest.json")
        self.assertEqual(manifest["status"], "invalid")
        self.assertFalse(manifest["data_integrity_after"]["passed"])

    def test_gate_intent_differences_diversity_and_approval(self) -> None:
        snapshot = self.ingest_fixture()
        manifest = self.run_fixture(str(snapshot["data_snapshot_id"]), "run-gates")
        probe = check_submission_gate(
            self.workspace,
            self.gate_report(manifest, "probe-1", "probe", metric_passed=False),
        )
        self.assertTrue(probe["ready_for_human_submission"], probe["errors"])

        candidate = check_submission_gate(
            self.workspace,
            self.gate_report(manifest, "candidate-no-metric", "candidate", metric_passed=False),
        )
        self.assertFalse(candidate["ready_for_human_submission"])

        single = check_submission_gate(
            self.workspace,
            self.gate_report(manifest, "candidate-single", "candidate", ensemble=False),
        )
        self.assertTrue(single["ready_for_human_submission"], single["errors"])

        ensemble = check_submission_gate(
            self.workspace,
            self.gate_report(manifest, "candidate-ensemble", "candidate", ensemble=True),
        )
        self.assertFalse(ensemble["ready_for_human_submission"])
        self.assertTrue(any("diversity" in item for item in ensemble["errors"]))

        final_path = self.gate_report(manifest, "final-expired", "final", expired=True)
        final = check_submission_gate(self.workspace, final_path)
        self.assertFalse(final["ready_for_human_submission"])
        self.assertTrue(any("expired" in item for item in final["errors"]))

        incomplete_final_path = self.gate_report(manifest, "final-incomplete", "final")
        incomplete_final = read_json(incomplete_final_path)
        del incomplete_final["automated_gates"]["cold_start"]
        write_json_atomic(incomplete_final_path, incomplete_final)
        incomplete = check_submission_gate(self.workspace, incomplete_final_path)
        self.assertFalse(incomplete["ready_for_human_submission"])
        self.assertTrue(any("cold_start" in item for item in incomplete["errors"]))

        wrong_family_path = self.gate_report(manifest, "candidate-wrong-family", "candidate")
        wrong_family = read_json(wrong_family_path)
        wrong_family["experiment_family"] = "unrelated-family"
        write_json_atomic(wrong_family_path, wrong_family)
        mismatch = check_submission_gate(self.workspace, wrong_family_path)
        self.assertFalse(mismatch["ready_for_human_submission"])
        self.assertTrue(any("experiment_family" in item for item in mismatch["errors"]))

        run_manifest_path = self.workspace / "runs" / "run-gates" / "run-manifest.json"
        run_manifest_path.write_text(
            run_manifest_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        tampered = check_submission_gate(
            self.workspace,
            self.gate_report(manifest, "candidate-tampered-run", "candidate"),
        )
        self.assertFalse(tampered["ready_for_human_submission"])
        self.assertTrue(any("seal mismatch" in item for item in tampered["errors"]))

    def test_candidate_hash_binding_metric_and_explicit_promotion(self) -> None:
        snapshot = self.ingest_fixture()
        manifest = self.run_fixture(str(snapshot["data_snapshot_id"]), "run-promote")
        report_path = self.gate_report(manifest, "candidate-good", "candidate")
        gate = check_submission_gate(self.workspace, report_path)
        self.assertTrue(gate["ready_for_human_submission"], gate["errors"])
        renewed_report = read_json(report_path)
        renewed_report["human_approval"] = self.approval(
            "candidate-good", str(manifest["artifacts"][0]["sha256"]), "candidate"
        )
        renewed_report["human_approval"]["request_id"] = "request-candidate-good-renewed"
        write_json_atomic(report_path, renewed_report)
        renewed_gate = check_submission_gate(self.workspace, report_path)
        self.assertTrue(renewed_gate["ready_for_human_submission"], renewed_gate["errors"])
        approvals = [
            item
            for item in load_events(self.workspace)
            if item.get("event_type") == "approval_recorded"
            and item.get("payload", {}).get("candidate_id") == "candidate-good"
        ]
        self.assertEqual(len(approvals), 2)
        with self.assertRaises(OpsError):
            promote_candidate(self.workspace, candidate_id="candidate-good", reason="too early")
        record_metric(
            self.workspace,
            run_id="run-promote",
            candidate_id="candidate-good",
            metric=metric(0.8123, source="public_lb", group="public-v1"),
        )
        champion = promote_candidate(
            self.workspace, candidate_id="candidate-good", reason="approved public anchor"
        )
        self.assertEqual(champion["champion_candidate_id"], "candidate-good")
        self.assertEqual(champion["online_anchor_candidate_id"], "candidate-good")
        state = (self.workspace / "STATE.md").read_text(encoding="utf-8")
        self.assertIn("candidate-good", state)
        self.assertIn("rollback=none", state)
        with self.assertRaises(OpsError):
            promote_candidate(
                self.workspace, candidate_id="candidate-good", reason="duplicate promotion"
            )
        champions = read_json(self.workspace / "champions.json")
        lane = champions["lanes"][next(iter(champions["lanes"]))]
        self.assertNotEqual(lane["rollback_candidate_id"], lane["champion_candidate_id"])
        append_event_checked(
            self.workspace,
            event(
                "candidate_prepared",
                {
                    "candidate_id": "candidate-unpromoted",
                    "intent": "candidate",
                    "artifact_path": manifest["artifacts"][0]["source_path"],
                    "artifact_sha256": manifest["artifacts"][0]["sha256"],
                    "decision": "pending",
                },
                track_id="primary",
                phase_id="active",
                data_snapshot_id=str(snapshot["data_snapshot_id"]),
                run_id="run-promote",
                experiment_family="baseline",
            ),
        )
        tampered_champions = read_json(self.workspace / "champions.json")
        tampered_lane = tampered_champions["lanes"][next(iter(tampered_champions["lanes"]))]
        tampered_lane["champion_candidate_id"] = "candidate-unpromoted"
        write_json_atomic(self.workspace / "champions.json", tampered_champions)
        validation = validate_workspace(self.workspace, deep=True)
        self.assertFalse(validation["ok"])
        self.assertTrue(any("latest promotion" in item for item in validation["errors"]))
        write_json_atomic(self.workspace / "champions.json", champions)

        altered = read_json(report_path)
        altered_artifact = Path(altered["artifact_path"])
        altered_artifact.write_text("id,prediction\n1,0.99\n", encoding="utf-8")
        changed_hash = sha256_file(altered_artifact)
        altered["artifact_sha256"] = changed_hash
        altered["human_approval"]["artifact_sha256"] = changed_hash
        write_json_atomic(report_path, altered)
        rebound = check_submission_gate(self.workspace, report_path)
        self.assertFalse(rebound["ready_for_human_submission"])
        self.assertTrue(any("already bound" in item for item in rebound["errors"]))

    def _append_run_metric(
        self,
        *,
        run_id: str,
        snapshot: str,
        family: str,
        value: float,
        source: str = "local_cv",
        track: str = "primary",
        idea_id: str = "idea-baseline",
    ) -> None:
        common = {
            "track_id": track,
            "phase_id": "active",
            "data_snapshot_id": snapshot,
            "run_id": run_id,
            "idea_id": idea_id,
            "experiment_family": family,
        }
        append_event_checked(self.workspace, event("run_planned", {"status": "planned"}, **common))
        append_event_checked(self.workspace, event("run_started", {"status": "running"}, **common))
        append_event_checked(self.workspace, event("run_completed", {"status": "completed"}, **common))
        append_event_checked(
            self.workspace,
            event("validation_recorded", {"status": "completed", "passed": True}, **common),
        )
        append_event_checked(
            self.workspace,
            event("metric_recorded", {"metric": metric(value, source=source)}, **common),
        )

    def test_dryness_comparability_float_threshold_and_flag_clear(self) -> None:
        create_or_update_idea(
            self.workspace,
            idea_id="idea-baseline",
            track_id="primary",
            phase_id="active",
            experiment_family="baseline",
            priority="high",
            status="done",
            hypothesis="Baseline established.",
        )
        competition = read_json(self.workspace / "competition.json")
        competition["tracks"].append(
            {
                "id": "secondary",
                "name": "secondary",
                "problem_type": "custom",
                "metrics": [
                    {
                        "name": "score",
                        "direction": "higher",
                        "primary": True,
                        "eps": 0.001,
                        "weight": 1.0,
                        "metadata": {},
                    }
                ],
                "metadata": {},
            }
        )
        write_json_atomic(self.workspace / "competition.json", competition)
        create_or_update_idea(
            self.workspace,
            idea_id="idea-secondary",
            track_id="secondary",
            phase_id="active",
            experiment_family="models",
            priority="low",
            status="done",
            hypothesis="Independent secondary track.",
        )
        for index, score in enumerate([0.5, 0.5002, 0.5004, 0.5008, 0.501]):
            self._append_run_metric(
                run_id=f"dry-{index}", snapshot="snapshot-a", family="models", value=score
            )
        self._append_run_metric(
            run_id="different-date", snapshot="snapshot-b", family="models", value=0.9
        )
        self._append_run_metric(
            run_id="different-source",
            snapshot="snapshot-a",
            family="models",
            value=0.9,
            source="local_proxy",
        )
        self._append_run_metric(
            run_id="different-track",
            snapshot="snapshot-a",
            family="models",
            value=0.99,
            track="secondary",
            idea_id="idea-secondary",
        )
        common = {
            "track_id": "primary",
            "phase_id": "active",
            "data_snapshot_id": "snapshot-a",
            "run_id": "dry-4",
            "idea_id": "idea-baseline",
            "experiment_family": "models",
        }
        append_event_checked(
            self.workspace,
            event("metric_recorded", {"metric": metric(0.501)}, **common),
        )
        result = check_dryness(
            self.workspace,
            track_id="primary",
            phase_id="active",
            data_snapshot_id="snapshot-a",
            experiment_family="models",
            metric_name="score",
            metric_source="local_cv",
            comparable_group="cv-v1",
        )
        self.assertTrue(result["dry"])
        self.assertEqual(result["comparable_runs"], 5)
        self.assertAlmostEqual(result["rolling_best_gain"], 0.001)
        flags = list((self.workspace / "flags").glob("DRY_*.flag"))
        self.assertEqual(len(flags), 1)

        create_or_update_idea(
            self.workspace,
            idea_id="idea-new",
            track_id="primary",
            phase_id="active",
            experiment_family="models",
            priority="high",
            status="open",
            hypothesis="Test a genuinely different representation.",
        )
        cleared = check_dryness(
            self.workspace,
            track_id="primary",
            phase_id="active",
            data_snapshot_id="snapshot-a",
            experiment_family="models",
            metric_name="score",
            metric_source="local_cv",
            comparable_group="cv-v1",
        )
        self.assertFalse(cleared["dry"])
        self.assertFalse(list((self.workspace / "flags").glob("DRY_*.flag")))

    def test_repeated_failure_root_cause_triggers_reflection(self) -> None:
        for index in range(3):
            common = {
                "track_id": "primary",
                "phase_id": "active",
                "data_snapshot_id": "snapshot-failure",
                "run_id": f"failure-{index}",
                "idea_id": "idea-baseline",
                "experiment_family": "failure-family",
            }
            append_event_checked(self.workspace, event("run_planned", {"status": "planned"}, **common))
            append_event_checked(self.workspace, event("run_started", {"status": "running"}, **common))
            append_event_checked(
                self.workspace,
                event(
                    "run_failed",
                    {"status": "failed", "failure_reason": "out-of-memory"},
                    **common,
                ),
            )
        result = check_dryness(
            self.workspace,
            track_id="primary",
            phase_id="active",
            data_snapshot_id="snapshot-failure",
            experiment_family="failure-family",
            metric_name="score",
            metric_source="local_cv",
            comparable_group="cv-v1",
        )
        self.assertTrue(result["dry"])
        self.assertTrue(result["repeated_failure"])

    def test_feedback_baseline_must_match_full_comparability_context(self) -> None:
        snapshot_a = self.ingest_fixture()
        run_a = self.run_fixture(str(snapshot_a["data_snapshot_id"]), "run-baseline-a")
        artifact_a = run_a["artifacts"][0]
        append_event_checked(
            self.workspace,
            event(
                "candidate_prepared",
                {
                    "candidate_id": "candidate-a",
                    "intent": "candidate",
                    "artifact_path": artifact_a["source_path"],
                    "artifact_sha256": artifact_a["sha256"],
                    "decision": "pending",
                },
                track_id="primary",
                phase_id="active",
                data_snapshot_id=str(snapshot_a["data_snapshot_id"]),
                run_id="run-baseline-a",
                experiment_family="baseline",
            ),
        )
        record_metric(
            self.workspace,
            run_id="run-baseline-a",
            candidate_id="candidate-a",
            metric=metric(0.5, source="public_lb", group="public-v1"),
        )

        data_file = self.root / "input" / "nested" / "train.csv"
        data_file.write_text(
            "id,target,event_time\n1,0,2026-01-01\n2,1,2026-01-15\n3,0,2026-01-20\n",
            encoding="utf-8",
        )
        snapshot_b = ingest_data(self.workspace, self.root / "input")
        run_b = self.run_fixture(str(snapshot_b["data_snapshot_id"]), "run-baseline-b")
        artifact_b = run_b["artifacts"][0]
        append_event_checked(
            self.workspace,
            event(
                "candidate_prepared",
                {
                    "candidate_id": "candidate-b",
                    "intent": "candidate",
                    "artifact_path": artifact_b["source_path"],
                    "artifact_sha256": artifact_b["sha256"],
                    "decision": "pending",
                },
                track_id="primary",
                phase_id="active",
                data_snapshot_id=str(snapshot_b["data_snapshot_id"]),
                run_id="run-baseline-b",
                experiment_family="baseline",
            ),
        )
        with self.assertRaises(OpsError) as caught:
            record_metric(
                self.workspace,
                run_id="run-baseline-b",
                candidate_id="candidate-b",
                baseline_candidate_id="candidate-a",
                metric=metric(0.6, source="public_lb", group="public-v1"),
            )
        self.assertIn("data_snapshot_id differs", str(caught.exception))


class IntegrationAndMigrationTests(WorkspaceCase):
    def test_complete_generic_flow_and_deterministic_release(self) -> None:
        snapshot = self.ingest_fixture()
        manifest = self.run_fixture(str(snapshot["data_snapshot_id"]), "run-full")
        self.assertTrue(validate_workspace(self.workspace, deep=True)["ok"])
        gate = check_submission_gate(
            self.workspace, self.gate_report(manifest, "candidate-full", "candidate")
        )
        self.assertTrue(gate["ready_for_human_submission"])
        record_metric(
            self.workspace,
            run_id="run-full",
            candidate_id="candidate-full",
            metric=metric(0.75, source="public_lb", group="public-v1"),
        )
        promote_candidate(self.workspace, candidate_id="candidate-full", reason="integration test")

        release_source = self.root / "release-source"
        release_source.mkdir()
        (release_source / "model.txt").write_text("deterministic-model\n", encoding="utf-8")
        first = build_release(
            self.workspace, source=release_source, output=Path("release/first.zip")
        )
        second = build_release(
            self.workspace, source=release_source, output=Path("release/second.zip")
        )
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        verified = verify_release(
            self.workspace,
            archive_path=Path(first["archive"]),
            expected_members=first["members"],
        )
        self.assertTrue(verified["passed"], verified["errors"])
        with zipfile.ZipFile(Path(first["archive"]), "a") as archive:
            archive.comment = b"metadata-tamper"
        tampered = verify_release(
            self.workspace,
            archive_path=Path(first["archive"]),
            expected_members=first["members"],
            expected_archive_sha256=first["archive_sha256"],
            expected_member_set_sha256=first["member_set_sha256"],
        )
        self.assertFalse(tampered["passed"])
        self.assertTrue(any("archive SHA256" in item for item in tampered["errors"]))
        postmortem = build_postmortem(
            self.workspace,
            final_result="completed",
            final_rank="top-10",
            reusable_components=["generic validator hook"],
        )
        self.assertEqual(postmortem["submission_usage"]["active"]["used"], 1)
        self.assertTrue(validate_workspace(self.workspace, deep=True)["ok"])

    def test_v1_migration_preserves_522_records_and_184_statuses(self) -> None:
        source = self.root / "legacy"
        source.mkdir()
        (source / "STATE.md").write_text(
            "# State\nCompetition slug: legacy-contest\nMetric: score\nDirection: higher\n",
            encoding="utf-8",
        )
        (source / "ideas_backlog.md").write_text("# Ideas\n", encoding="utf-8")
        with (source / "experiment_ledger.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for index in range(518):
                if index in {100, 200, 300, 400}:
                    handle.write("\n")
                handle.write(
                    json.dumps(
                        {
                            "run_id": f"legacy-{index}",
                            "status": f"legacy-status-{index % 184}",
                            "task": "Task1" if index % 2 == 0 else "Task2",
                            "score": index / 1000,
                        }
                    )
                    + "\n"
                )
        source_hashes = {
            path.name: sha256_file(path)
            for path in [
                source / "STATE.md",
                source / "ideas_backlog.md",
                source / "experiment_ledger.jsonl",
            ]
        }
        destination = self.root / "migrated"
        report = migrate_v1_workspace(source, destination, platform="legacy-manual")
        self.assertEqual(report["records_imported"], 522)
        self.assertEqual(report["json_records_imported"], 518)
        self.assertEqual(report["blank_lines_imported"], 4)
        self.assertEqual(report["unique_legacy_statuses"], 184)
        self.assertTrue(report["source_unchanged"])
        self.assertFalse(report["champion_inference_performed"])
        self.assertEqual(
            source_hashes,
            {
                path.name: sha256_file(path)
                for path in [
                    source / "STATE.md",
                    source / "ideas_backlog.md",
                    source / "experiment_ledger.jsonl",
                ]
            },
        )
        imported = [item for item in load_events(destination) if item["event_type"] == "legacy_record_imported"]
        self.assertEqual(len(imported), 522)
        self.assertEqual(sum(item.get("track_id") == "Task1" for item in imported), 259)
        self.assertEqual(sum(item.get("track_id") == "Task2" for item in imported), 259)
        self.assertEqual(imported[0]["payload"]["legacy_payload"]["run_id"], "legacy-0")
        self.assertTrue((destination / "reports" / "archive" / "v1" / "STATE.md").is_file())
        self.assertTrue(validate_workspace(destination)["ok"])


class CliSurfaceTests(unittest.TestCase):
    def test_unified_cli_exposes_all_v2_commands(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "kaggle_ops.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        for command in [
            "bootstrap",
            "migrate",
            "ingest",
            "run",
            "validate",
            "status",
            "dryness",
            "gate",
            "feedback",
            "promote",
            "release",
            "postmortem",
        ]:
            self.assertIn(command, completed.stdout)

    def test_container_command_enforces_offline_read_only_inputs(self) -> None:
        root = Path("C:/competition-test")
        command = _docker_command(
            container={"image": "example@sha256:123", "image_id": "sha256:123"},
            command="python /workspace/source/train.py",
            run_dir=root / "run",
            data_root=root / "input",
            snapshot_path=root / "snapshot.json",
            source_root=root / "source",
            config_path=root / "config.json",
            run_id="run-container",
            seed=42,
        )
        rendered = " ".join(command)
        self.assertIn("--network none", rendered)
        self.assertIn("--read-only", rendered)
        self.assertIn("target=/workspace/input,readonly", rendered)
        self.assertIn("target=/workspace/source,readonly", rendered)
        self.assertIn("target=/workspace/run", rendered)


if __name__ == "__main__":
    unittest.main()
