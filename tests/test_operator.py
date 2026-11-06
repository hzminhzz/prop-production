import json


def test_readonly_operator_surfaces_redact_account_and_cannot_clear_halt(tmp_path, capsys):
    from prop_production.cli import main
    from prop_production.journal import Journal
    path = tmp_path / "account.sqlite"
    with Journal(path, "private-account-do-not-log", initialize=True) as journal:
        journal.halt("MANUAL", 1)
    for command in ("status", "portfolio", "risk", "broker", "intent"):
        assert main([command, "--journal", str(path), "--now-ms", "1000"]) == 0
        output = capsys.readouterr().out
        assert "private-account-do-not-log" not in output
        result = json.loads(output)
        assert result["trading_authorized"] is False
    before = Journal.read(path)["sequence"]
    main(["status", "--journal", str(path)])
    capsys.readouterr()
    assert Journal.read(path)["sequence"] == before
    assert main(["halt", "--journal", str(path), "--now-ms", "1001"]) == 0
    capsys.readouterr()
    assert "MANUAL_HALT" in Journal.read(path)["halts"]


def test_paper_evidence_chain_distinguishes_observation_from_follower_pnl(tmp_path):
    from prop_production.paper import ObservationJournal
    path = tmp_path / "observations.sqlite"
    log = ObservationJournal(path, initialize=True)
    evidence = {"schema": "read_only_source_observation_v1", "native_tape_sha": "a" * 64,
                "source_receipts": 12, "market_receipts": 4, "collateral_receipts": 2,
                "source_nav_attested": False, "trade_volume_complete": False,
                "native_observer_health": "DEGRADED_OBSERVER_ONLY", "upstream_alive": True,
                "blockers": ["NAV_SCOPE_UNKNOWN"]}
    log.append(evidence, 1000)
    log.append(evidence, 2000)
    report = log.report(3000)
    assert report["verdict"] == "BLOCKED_PAPER_ADMISSION"
    assert report["read_only_collection"] == "EVIDENCE_COLLECTED"
    assert report["observations"] == 2
    assert report["source_state"] == "QUIET"
    assert report["independently_matched_follower_pnl"] == "UNKNOWN"
    assert report["synthetic_model_pnl"] == "NOT_COMPUTED"
    assert report["paper_days_qualified"] == 0
    log.close()
    assert ObservationJournal.read(path, 3000)["observations"] == 2


def test_empty_paper_evidence_reports_collection_separately_from_admission(tmp_path):
    from prop_production.paper import ObservationJournal

    path = tmp_path / "empty.sqlite"
    log = ObservationJournal(path, initialize=True)
    report = log.report(3000)
    assert report["read_only_collection"] == "NO_OBSERVATIONS"
    assert report["verdict"] == "BLOCKED_PAPER_ADMISSION"
    assert report["broker_mutations"] == 0
    assert report["trading_authorized"] is False
    log.close()
