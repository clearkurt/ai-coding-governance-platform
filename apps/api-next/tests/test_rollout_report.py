import sys
from argparse import Namespace
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import rollout_report


@pytest.mark.parametrize(
    "arguments",
    [["report", "--window-hours", "0"], ["report", "--stale-hours", "169"], ["report", "--team", "not-a-uuid"]],
)
def test_rollout_report_rejects_invalid_arguments(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit) as error:
        rollout_report.parser().parse_args()
    assert error.value.code == 2


def test_rollout_report_failure_is_stable_and_non_sensitive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail(_settings, _arguments):
        raise SQLAlchemyError("postgresql://user:secret@private-db/source")

    monkeypatch.setattr(sys, "argv", ["report"])
    monkeypatch.setattr(rollout_report, "validate_configuration", lambda: (True, []))
    monkeypatch.setattr(rollout_report, "Settings", lambda: SimpleNamespace())
    monkeypatch.setattr(rollout_report, "run_report", fail)

    assert rollout_report.main() == 1
    assert capsys.readouterr().out == '{"error": "rollout report unavailable"}\n'


def test_rollout_report_success_is_stable_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def succeed(_settings, arguments: Namespace):
        return {"rollout_mode": "allowlist", "window_hours": arguments.window_hours, "tasks_by_status": {}}

    monkeypatch.setattr(sys, "argv", ["report", "--window-hours", "12"])
    monkeypatch.setattr(rollout_report, "validate_configuration", lambda: (True, []))
    monkeypatch.setattr(rollout_report, "Settings", lambda: SimpleNamespace())
    monkeypatch.setattr(rollout_report, "run_report", succeed)

    assert rollout_report.main() == 0
    assert capsys.readouterr().out == '{"rollout_mode":"allowlist","tasks_by_status":{},"window_hours":12}\n'
