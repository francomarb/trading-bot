"""Focused tests for live preflight approval semantics."""

from scripts.preflight import _strategy_graduation_approved


class TestStrategyGraduationApproval:
    def test_accepts_explicit_truthy_values(self, monkeypatch):
        for value in ("yes", "true", "1", " YES "):
            monkeypatch.setenv("STRATEGY_GRADUATION_APPROVED", value)
            assert _strategy_graduation_approved() is True

    def test_rejects_missing_or_unapproved_value(self, monkeypatch):
        monkeypatch.delenv("STRATEGY_GRADUATION_APPROVED", raising=False)
        assert _strategy_graduation_approved() is False

        monkeypatch.setenv("STRATEGY_GRADUATION_APPROVED", "no")
        assert _strategy_graduation_approved() is False

    def test_legacy_gonogo_flag_is_not_accepted(self, monkeypatch):
        monkeypatch.delenv("STRATEGY_GRADUATION_APPROVED", raising=False)
        monkeypatch.setenv("GONOGO_APPROVED", "yes")
        assert _strategy_graduation_approved() is False
