def test_package_import():
    import network_report

    assert network_report.__version__ == "0.1.0"


def test_template_exists():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert (root / "config" / "Template-Daily-Report.example.xlsx").exists()
    assert (root / "config" / "Template-Daily-WAG.example.md").exists()
