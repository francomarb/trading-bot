"""Tests for the disposable option-stop replacement diagnostic store."""

from datetime import datetime, timedelta, timezone

from engine.option_stop_audit import OptionStopReplaceAuditStore


NOW = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)


class TestOptionStopReplaceAuditStore:
    def test_appends_reads_and_prunes_json_evidence(self, tmp_path):
        store = OptionStopReplaceAuditStore(tmp_path / "audit.db")
        store.append(
            correlation_id="corr-1",
            recorded_at=NOW,
            record_type="decision_replace",
            strategy="spy_options_reversion",
            occ_symbol="SPY260702C00724000",
            order_id="old-stop",
            payload={"desired_stop_price": 19.55},
        )
        store.append(
            correlation_id="corr-1",
            recorded_at=NOW + timedelta(milliseconds=80),
            record_type="stream_fill",
            strategy="spy_options_reversion",
            occ_symbol="SPY260702C00724000",
            order_id="new-stop",
            payload={"qty": 2, "price": 23.0},
        )

        records = store.read_records(correlation_id="corr-1")

        assert [row["record_type"] for row in records] == [
            "decision_replace",
            "stream_fill",
        ]
        assert records[1]["payload"]["price"] == 23.0
        assert store.prune_before(NOW + timedelta(days=1)) == 2
        assert store.read_records() == []
        store.close()
