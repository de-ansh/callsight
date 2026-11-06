import json
from pathlib import Path

from callsight.cli import main

SAMPLE = str(Path(__file__).parents[3] / "examples" / "sample_call.txt")


def test_cli_text_report(capsys):
    assert main(["analyze", SAMPLE, "--competitor", "AcmeCall"]) == 0
    out = capsys.readouterr().out
    assert "[HIGH] No clear next step" in out
    assert "Competitor mentioned: AcmeCall" in out
    assert "[00:58] Priya:" in out  # evidence is shown with timestamp and speaker
    assert "Honestly our budget" in out


def test_cli_json_output(capsys):
    assert main(["analyze", SAMPLE, "--json", "--call-id", "c9"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["call_id"] == "c9"
    assert data["insights"]


def test_cli_reports_unparseable_input(tmp_path, capsys):
    bad = tmp_path / "bad.txt"
    bad.write_text("no timestamps here")
    assert main(["analyze", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err
