import unittest
from datetime import date

from pydantic import ValidationError

from app.models import (
    EvaluationResult, ExtractedTask, ProjectUpdate, RiskItem, RiskReport, Severity,
    StatusReport, Health, DeadlineFindings,
)


class ModelContracts(unittest.TestCase):
    def test_extracted_task_defaults(self):
        t = ExtractedTask(title="Login page")
        self.assertIsNone(t.status)
        self.assertIsNone(t.owner)

    def test_short_title_rejected(self):
        with self.assertRaises(ValidationError):
            ExtractedTask(title="x")

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValidationError):
            ExtractedTask(title="Login page", status="finished")

    def test_project_update_roundtrip(self):
        u = ProjectUpdate(project_id=1, tasks=[ExtractedTask(title="Payment API", deadline=date(2026, 10, 20))])
        self.assertEqual(ProjectUpdate.model_validate_json(u.model_dump_json()), u)

    def test_critical_blocker_property(self):
        r = RiskReport(items=[RiskItem(description="Payment API blocked", severity=Severity.critical, is_blocker=True)])
        self.assertTrue(r.is_critical_blocker)
        r2 = RiskReport(items=[RiskItem(description="Minor delay on QA", severity=Severity.low)])
        self.assertFalse(r2.is_critical_blocker)

    def test_critical_but_not_blocker_is_not_critical_blocker(self):
        r = RiskReport(items=[RiskItem(description="Budget concern raised", severity=Severity.critical, is_blocker=False)])
        self.assertFalse(r.is_critical_blocker)

    def test_evaluation_requires_claims_when_ungrounded(self):
        with self.assertRaises(ValidationError):
            EvaluationResult(is_grounded=False, revision_needed=True)

    def test_evaluation_forces_revision(self):
        e = EvaluationResult(is_grounded=False, unsupported_claims=["Task 7 is complete"], revision_needed=False)
        self.assertTrue(e.revision_needed)

    def test_status_report_needs_real_summary(self):
        with self.assertRaises(ValidationError):
            StatusReport(summary="ok", overall_health=Health.on_track)

    def test_deadline_findings_negative_tasks_rejected(self):
        with self.assertRaises(ValidationError):
            DeadlineFindings(deadline=date(2026, 10, 30), days_remaining=5, open_task_count=-1, at_risk=False, summary="fine")


if __name__ == "__main__":
    unittest.main()
