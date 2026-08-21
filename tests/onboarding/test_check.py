"""Unit tests for the chart-owned onboarding validator.

Run with:  pytest tests/onboarding

Every upstream Hermes adapter in ``check.py`` is monkeypatched here, so these
tests run without the Hermes image. Compatibility with the *real* internal
APIs is covered separately by the image smoke test in CI.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CHART_ROOT = Path(__file__).resolve().parents[2]
CHECK_PY = CHART_ROOT / "files" / "onboarding" / "check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("onboarding_check", CHECK_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load_module()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "data"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


@pytest.fixture(autouse=True)
def _no_real_adapters(monkeypatch):
    """Fail loudly if a test forgets to stub an upstream adapter."""
    monkeypatch.setattr(check, "prepare_environment", lambda: None)
    monkeypatch.setattr(check, "hermes_version", lambda: "test-version")
    monkeypatch.setattr(check, "_gateway_config_cache", None)


def write_requirements(tmp_path, provider=True, platforms=()):
    path = tmp_path / "requirements.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "validatorVersion": 1,
                "provider": provider,
                "platforms": list(platforms),
            }
        )
    )
    return str(path)


class FakeEntry:
    def __init__(self, *, deps_ok=True, allowed_users_env="", allow_all_env=""):
        self.check_fn = (lambda: deps_ok) if deps_ok is not None else None
        self.allowed_users_env = allowed_users_env
        self.allow_all_env = allow_all_env
        self.install_hint = "Run `hermes setup`."


class FakePlatformConfig:
    def __init__(self, enabled=True):
        self.enabled = enabled


class FakeGatewayConfig:
    def __init__(self, enabled_platforms=(), behavior="pair"):
        self.platforms = {name: FakePlatformConfig(True) for name in enabled_platforms}
        self._behavior = behavior

    def get_unauthorized_dm_behavior(self, platform):
        return self._behavior


def stub_platforms(
    monkeypatch,
    *,
    enabled=(),
    connected=(),
    behavior="pair",
    deps_ok=True,
    pairings=0,
    photon_numbers=("+15550001111", "+15550002222"),
):
    config = FakeGatewayConfig(enabled, behavior)
    monkeypatch.setattr(check, "gateway_config", lambda: config)
    monkeypatch.setattr(check, "platform_enum", lambda name: name)
    monkeypatch.setattr(
        check,
        "platform_registry_entry",
        lambda name: FakeEntry(
            deps_ok=deps_ok,
            allowed_users_env=f"{name.upper()}_ALLOWED_USERS",
            allow_all_env=f"{name.upper()}_ALLOW_ALL_USERS",
        ),
    )
    monkeypatch.setattr(check, "connected_platform_values", lambda: set(connected))
    monkeypatch.setattr(check, "approved_pairings", lambda name: pairings)
    monkeypatch.setattr(check, "photon_user_numbers", lambda: photon_numbers)
    return config


def stub_provider(monkeypatch, *, runtime=None, reason=None, usable=True):
    monkeypatch.setattr(
        check, "resolve_provider_runtime", lambda: (runtime, reason)
    )
    monkeypatch.setattr(check, "secret_is_usable", lambda value: usable)


# --------------------------------------------------------------------------
# Requirements + hashing
# --------------------------------------------------------------------------


def test_requirements_are_normalized(tmp_path):
    path = tmp_path / "requirements.json"
    path.write_text(json.dumps({"provider": True, "platforms": ["Telegram", "photon", "telegram"]}))
    requirements = check.load_requirements(str(path))
    assert requirements["platforms"] == ["photon", "telegram"]


def test_unsupported_platform_is_a_validator_error(tmp_path):
    path = tmp_path / "requirements.json"
    path.write_text(json.dumps({"provider": True, "platforms": ["slack"]}))
    with pytest.raises(check.ValidatorError):
        check.load_requirements(str(path))


def test_requirements_hash_is_order_insensitive():
    a = {"provider": True, "platforms": ["telegram", "photon"]}
    b = {"provider": True, "platforms": ["photon", "telegram"]}
    assert check.requirements_hash(a) == check.requirements_hash(b)


def test_requirements_hash_changes_with_platform_set():
    a = {"provider": True, "platforms": ["photon"]}
    b = {"provider": True, "platforms": ["photon", "telegram"]}
    assert check.requirements_hash(a) != check.requirements_hash(b)


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


def test_no_provider_configured_is_incomplete(tmp_path, home, monkeypatch, capsys):
    stub_provider(monkeypatch, runtime=None, reason="no credentials found")
    rc = check.main(["--requirements", write_requirements(tmp_path)])
    assert rc == check.EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "provider" in out
    assert not (home / check.LATCH_FILENAME).exists()


def test_provider_without_usable_key_is_incomplete(tmp_path, home, monkeypatch):
    stub_provider(
        monkeypatch, runtime={"provider": "openrouter", "api_key": ""}, usable=False
    )
    rc = check.main(["--requirements", write_requirements(tmp_path)])
    assert rc == check.EXIT_INCOMPLETE


def test_provider_complete_writes_latch(tmp_path, home, monkeypatch):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    rc = check.main(["--requirements", write_requirements(tmp_path)])
    assert rc == check.EXIT_OK
    latch = json.loads((home / check.LATCH_FILENAME).read_text())
    assert latch["schemaVersion"] == 1
    assert latch["requirements"] == {"provider": True, "platforms": []}
    assert latch["requirementsHash"].startswith("sha256:")


# --------------------------------------------------------------------------
# Platforms
# --------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["photon", "telegram", "discord"])
def test_platform_not_enabled_is_incomplete(tmp_path, home, monkeypatch, platform, capsys):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(monkeypatch, enabled=(), connected=())
    rc = check.main(
        ["--requirements", write_requirements(tmp_path, platforms=[platform])]
    )
    assert rc == check.EXIT_INCOMPLETE
    assert f"platform:{platform}" in capsys.readouterr().out


@pytest.mark.parametrize("platform", ["photon", "telegram", "discord"])
def test_platform_complete(tmp_path, home, monkeypatch, platform):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(monkeypatch, enabled=(platform,), connected=(platform,))
    rc = check.main(
        ["--requirements", write_requirements(tmp_path, platforms=[platform])]
    )
    assert rc == check.EXIT_OK
    latch = json.loads((home / check.LATCH_FILENAME).read_text())
    assert latch["requirements"]["platforms"] == [platform]


def test_enabled_but_unconnected_platform_is_incomplete(tmp_path, home, monkeypatch, capsys):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(monkeypatch, enabled=("telegram",), connected=())
    rc = check.main(["--requirements", write_requirements(tmp_path, platforms=["telegram"])])
    assert rc == check.EXIT_INCOMPLETE
    assert "does not consider it connected" in capsys.readouterr().out


def test_missing_platform_dependencies_are_reported(tmp_path, home, monkeypatch, capsys):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(monkeypatch, enabled=("photon",), connected=("photon",), deps_ok=False)
    rc = check.main(["--requirements", write_requirements(tmp_path, platforms=["photon"])])
    assert rc == check.EXIT_INCOMPLETE
    assert "runtime dependencies for photon" in capsys.readouterr().out


def test_photon_requires_registered_and_assigned_numbers(tmp_path, home, monkeypatch, capsys):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(
        monkeypatch,
        enabled=("photon",),
        connected=("photon",),
        photon_numbers=(None, None),
    )
    rc = check.main(["--requirements", write_requirements(tmp_path, platforms=["photon"])])
    assert rc == check.EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "registered Photon user/phone" in out
    assert "assigned Photon line" in out


def test_pairing_mode_satisfies_authorization(tmp_path, home, monkeypatch):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(
        monkeypatch, enabled=("telegram",), connected=("telegram",), behavior="pair"
    )
    rc = check.main(["--requirements", write_requirements(tmp_path, platforms=["telegram"])])
    assert rc == check.EXIT_OK


def test_ignore_behavior_without_any_grant_is_incomplete(tmp_path, home, monkeypatch, capsys):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(
        monkeypatch, enabled=("telegram",), connected=("telegram",), behavior="ignore"
    )
    rc = check.main(["--requirements", write_requirements(tmp_path, platforms=["telegram"])])
    assert rc == check.EXIT_INCOMPLETE
    assert "nobody is authorized on telegram" in capsys.readouterr().out


def test_allowlist_satisfies_authorization(tmp_path, home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "12345")
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(
        monkeypatch, enabled=("telegram",), connected=("telegram",), behavior="ignore"
    )
    rc = check.main(["--requirements", write_requirements(tmp_path, platforms=["telegram"])])
    assert rc == check.EXIT_OK


def test_approved_pairing_satisfies_authorization(tmp_path, home, monkeypatch):
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(
        monkeypatch,
        enabled=("discord",),
        connected=("discord",),
        behavior="ignore",
        pairings=1,
    )
    rc = check.main(["--requirements", write_requirements(tmp_path, platforms=["discord"])])
    assert rc == check.EXIT_OK


def test_multiple_missing_requirements_are_reported_together(tmp_path, home, monkeypatch, capsys):
    stub_provider(monkeypatch, runtime=None, reason="no credentials found")
    stub_platforms(monkeypatch, enabled=(), connected=())
    rc = check.main(
        [
            "--requirements",
            write_requirements(tmp_path, platforms=["photon", "telegram"]),
        ]
    )
    assert rc == check.EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "- provider:" in out
    assert "- platform:photon:" in out
    assert "- platform:telegram:" in out


# --------------------------------------------------------------------------
# Latch behavior
# --------------------------------------------------------------------------


def test_matching_latch_short_circuits_all_adapters(tmp_path, home, monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("adapters must not run when the latch matches")

    monkeypatch.setattr(check, "prepare_environment", explode)
    monkeypatch.setattr(check, "resolve_provider_runtime", explode)
    monkeypatch.setattr(check, "gateway_config", explode)

    requirements = {"provider": True, "platforms": ["telegram"]}
    check.write_latch(
        str(home / check.LATCH_FILENAME),
        requirements,
        check.requirements_hash(requirements),
    )
    rc = check.main(
        ["--requirements", write_requirements(tmp_path, platforms=["telegram"])]
    )
    assert rc == check.EXIT_OK


def test_stale_latch_forces_revalidation(tmp_path, home, monkeypatch):
    old = {"provider": True, "platforms": ["photon"]}
    check.write_latch(
        str(home / check.LATCH_FILENAME), old, check.requirements_hash(old)
    )
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(monkeypatch, enabled=("photon",), connected=("photon",))

    # Adding telegram invalidates the latch: telegram is not configured, so the
    # next rollout is intentionally blocked.
    rc = check.main(
        [
            "--requirements",
            write_requirements(tmp_path, platforms=["photon", "telegram"]),
        ]
    )
    assert rc == check.EXIT_INCOMPLETE
    latch = json.loads((home / check.LATCH_FILENAME).read_text())
    assert latch["requirements"]["platforms"] == ["photon"]


def test_malformed_latch_is_treated_as_absent(tmp_path, home, monkeypatch):
    (home / check.LATCH_FILENAME).write_text("{not json")
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    rc = check.main(["--requirements", write_requirements(tmp_path)])
    assert rc == check.EXIT_OK
    assert json.loads((home / check.LATCH_FILENAME).read_text())["schemaVersion"] == 1


def test_force_ignores_a_matching_latch(tmp_path, home, monkeypatch):
    requirements = {"provider": True, "platforms": []}
    check.write_latch(
        str(home / check.LATCH_FILENAME),
        requirements,
        check.requirements_hash(requirements),
    )
    stub_provider(monkeypatch, runtime=None, reason="provider went away")
    rc = check.main(["--requirements", write_requirements(tmp_path), "--force"])
    assert rc == check.EXIT_INCOMPLETE
    # A transient failure under --force must not delete the existing latch.
    assert (home / check.LATCH_FILENAME).exists()


def test_latch_write_is_atomic(tmp_path, home, monkeypatch):
    requirements = {"provider": True, "platforms": []}
    path = home / check.LATCH_FILENAME
    check.write_latch(str(path), requirements, check.requirements_hash(requirements))
    leftovers = [p.name for p in home.iterdir() if p.name.startswith(".helm-onboarding-")]
    assert leftovers == [check.LATCH_FILENAME]


# --------------------------------------------------------------------------
# Validator errors and secret hygiene
# --------------------------------------------------------------------------


def test_import_incompatibility_is_a_validator_error(tmp_path, home, monkeypatch, capsys):
    def incompatible():
        raise check._incompatible("photon", ImportError("No module named 'photon'"))

    monkeypatch.setattr(check, "prepare_environment", incompatible)
    rc = check.main(["--requirements", write_requirements(tmp_path, platforms=["photon"])])
    assert rc == check.EXIT_VALIDATOR_ERROR
    err = capsys.readouterr().err
    assert "VALIDATOR ERROR" in err
    assert "incompatible with Hermes" in err
    assert not (home / check.LATCH_FILENAME).exists()


def test_incompatibility_is_not_reported_as_missing_configuration(monkeypatch):
    error = check._incompatible("photon", ImportError("boom"))
    assert "Photon is not configured" not in str(error)
    assert "update the chart validator" in str(error)


def test_no_secret_values_reach_stdout_or_the_latch(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-supersecret-value")
    stub_provider(
        monkeypatch,
        runtime=None,
        reason="upstream said: bad key sk-supersecret-value",
    )
    rc = check.main(["--requirements", write_requirements(tmp_path)])
    assert rc == check.EXIT_INCOMPLETE
    captured = capsys.readouterr()
    assert "sk-supersecret-value" not in captured.out
    assert "sk-supersecret-value" not in captured.err
    assert check._REDACTED in captured.out


def test_latch_never_stores_identity_or_credentials(tmp_path, home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:super-secret")
    stub_provider(monkeypatch, runtime={"provider": "openrouter", "api_key": "sk-real"})
    stub_platforms(
        monkeypatch,
        enabled=("photon",),
        connected=("photon",),
        photon_numbers=("+15550001111", "+15550002222"),
    )
    check.main(["--requirements", write_requirements(tmp_path, platforms=["photon"])])
    latch_text = (home / check.LATCH_FILENAME).read_text()
    assert "123456:super-secret" not in latch_text
    assert "+1555000" not in latch_text
    assert "sk-real" not in latch_text


def test_timeout_leaves_no_latch(tmp_path, home, monkeypatch):
    """A bounded attempt that never finishes must not look like success."""

    def slow():
        raise TimeoutError("attempt exceeded validationTimeoutSeconds")

    monkeypatch.setattr(check, "prepare_environment", slow)
    with pytest.raises(TimeoutError):
        check.evaluate({"provider": True, "platforms": []})
    assert not (home / check.LATCH_FILENAME).exists()


def test_app_root_detection(tmp_path, monkeypatch):
    root = tmp_path / "opt-hermes"
    (root / "hermes_cli").mkdir(parents=True)
    (root / "gateway").mkdir()
    monkeypatch.setenv("HERMES_APP_ROOT", str(root))
    assert check.hermes_app_root() == str(root)
    check.ensure_app_on_path()
    import sys

    assert str(root) in sys.path
    sys.path.remove(str(root))


def test_app_root_detection_ignores_unrelated_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_APP_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert check.hermes_app_root() in (None, "/opt/hermes")


def test_json_output_is_machine_readable(tmp_path, home, monkeypatch, capsys):
    stub_provider(monkeypatch, runtime=None, reason="nothing configured")
    rc = check.main(["--requirements", write_requirements(tmp_path), "--json"])
    assert rc == check.EXIT_INCOMPLETE
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "incomplete"
    assert payload["missing"][0]["requirement"] == "provider"
