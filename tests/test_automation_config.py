from __future__ import annotations

from talos.automation_config import AutomationConfig


class TestAutomationConfigDefaults:
    def test_edge_threshold_cents(self) -> None:
        cfg = AutomationConfig()
        assert cfg.edge_threshold_cents == 1.5

    def test_stability_seconds(self) -> None:
        cfg = AutomationConfig()
        assert cfg.stability_seconds == 5.0

    def test_staleness_grace_seconds(self) -> None:
        cfg = AutomationConfig()
        assert cfg.staleness_grace_seconds == 5.0

    def test_rejection_cooldown_seconds(self) -> None:
        cfg = AutomationConfig()
        assert cfg.rejection_cooldown_seconds == 30.0

    def test_unit_size(self) -> None:
        cfg = AutomationConfig()
        assert cfg.unit_size == 10

    def test_enabled_off_by_default(self) -> None:
        cfg = AutomationConfig()
        assert cfg.enabled is False


class TestAutomationConfigCustom:
    def test_custom_values_override_defaults(self) -> None:
        cfg = AutomationConfig(
            edge_threshold_cents=3.0,
            stability_seconds=10.0,
            staleness_grace_seconds=8.0,
            rejection_cooldown_seconds=60.0,
            unit_size=25,
            enabled=True,
        )
        assert cfg.edge_threshold_cents == 3.0
        assert cfg.stability_seconds == 10.0
        assert cfg.staleness_grace_seconds == 8.0
        assert cfg.rejection_cooldown_seconds == 60.0
        assert cfg.unit_size == 25
        assert cfg.enabled is True

    def test_partial_override(self) -> None:
        cfg = AutomationConfig(edge_threshold_cents=5.0, enabled=True)
        assert cfg.edge_threshold_cents == 5.0
        assert cfg.enabled is True
        # remaining fields keep defaults
        assert cfg.stability_seconds == 5.0
        assert cfg.staleness_grace_seconds == 5.0
        assert cfg.rejection_cooldown_seconds == 30.0
        assert cfg.unit_size == 10
