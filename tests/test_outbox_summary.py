"""An operator can inspect the queue without copying contacts into logs."""

import asyncio
import sys

import pytest

from sellbird import cli, config, state


def test_outbox_summary_shows_all_counts_without_changing_the_queue(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "outbox.db"
    monkeypatch.setattr(state, "DB_PATH", db_path)
    cfg = tmp_path / "sellbird.yaml"
    cfg.write_text("channels: {email: {require_approval: true}}", encoding="utf-8")
    monkeypatch.setattr(config, "_find_config_file", lambda: cfg)
    monkeypatch.setattr(cli, "_carry_over_from_harvey", lambda: pytest.fail("summary ran migration"))

    async def seed():
        manager = state.StateManager()
        await manager.init_db()
        await manager.set_setting("sending_paused", "private pause reason")
        for status, count in {"pending_review": 51, "approved": 11, "sending": 1, "sent": 2}.items():
            for i in range(count):
                await manager.add_outbox_item(
                    prospect_id=f"{status}-{i}", to_email="editor@example.test",
                    subject="private subject", body="private body",
                    send_at="2026-01-01T00:00:00", status=status,
                )

    asyncio.run(seed())
    before = db_path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["sellbird", "outbox", "--summary"])
    cli.main()
    output = capsys.readouterr().out
    for line in (
        "Approval required: yes", "Sending paused: yes",
        "Awaiting approval: 51", "Approved: 11", "Sending: 1", "Sent: 2",
    ):
        assert line in output
    for private in ("private", "editor@example.test"):
        assert private not in output
    assert db_path.read_bytes() == before


@pytest.mark.parametrize("flags", [["--approve-all"], ["--approve", "draft"], ["--reject", "draft"]])
def test_outbox_summary_rejects_mutation_flags(monkeypatch, flags, capsys):
    monkeypatch.setattr(sys, "argv", ["sellbird", "outbox", "--summary", *flags])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
    assert "--summary cannot be combined" in capsys.readouterr().err


def test_outbox_summary_does_not_initialize_a_missing_database(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "not-initialized.db"
    monkeypatch.setattr(state, "DB_PATH", db_path)
    monkeypatch.setattr(sys, "argv", ["sellbird", "outbox", "--summary"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    assert "No initialized Outbox database" in capsys.readouterr().out
    assert not db_path.exists()


def test_outbox_summary_keeps_invalid_config_out_of_output(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "outbox #1.db"
    monkeypatch.setattr(state, "DB_PATH", db_path)
    cfg = tmp_path / "sellbird.yaml"
    cfg.write_text("broken: [private-config-marker", encoding="utf-8")
    monkeypatch.setattr(config, "_find_config_file", lambda: cfg)
    asyncio.run(state.StateManager().init_db())
    monkeypatch.setattr(sys, "argv", ["sellbird", "outbox", "--summary"])
    cli.main()
    result = capsys.readouterr()
    assert "Approval required: unknown" in result.out
    assert "Awaiting approval: 0" in result.out
    assert "private-config-marker" not in result.out + result.err
