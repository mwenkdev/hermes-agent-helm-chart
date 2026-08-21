#!/usr/bin/env python3
"""Onboarding gate validator for the Hermes Agent Helm chart.

Performs exactly ONE bounded, non-interactive check that the locally
persisted Hermes configuration satisfies the requirements declared by the
chart (``onboarding.requirements`` in values.yaml).  It never sends a model
prompt and never proves that a remote API is currently reachable -- it only
establishes that the local configuration selects a usable provider and that
each required messaging platform is locally configured.

This file is shipped inside the chart and mounted read-only into the
``onboarding-gate`` init container, which runs the *official* Hermes image.
Everything it imports from Hermes is an upstream internal API, so every such
import lives behind a narrow adapter that raises :class:`ValidatorError`
(exit code 20) instead of being misreported as "not configured".

Adapters traced against hermes-agent v2026.8.3 (hermes_cli 0.20.0):

* ``hermes_cli.env_loader.load_hermes_dotenv`` -- what ``gateway/run.py``
  itself calls before anything else, so ``$HERMES_HOME/.env`` (written by
  ``hermes photon setup`` and friends) has the same authority here as in the
  gateway.
* ``hermes_cli.runtime_provider.resolve_runtime_provider`` -- the shared
  provider resolution path used by the gateway, CLI and cron.
* ``hermes_cli.auth.has_usable_secret`` -- upstream's own "is this string a
  real credential" predicate (rejects "", "changeme", "placeholder", ...).
* ``gateway.config.load_gateway_config`` / ``GatewayConfig
  .get_connected_platforms`` -- platform enablement + credential resolution,
  including the ``_apply_env_overrides`` pass that auto-enables a platform
  from its token env var.
* ``gateway.platform_registry.platform_registry`` -- per-platform
  ``check_fn`` (runtime dependencies, e.g. the Photon spectrum-ts sidecar)
  and the ``allowed_users_env`` / ``allow_all_env`` / ``install_hint``
  metadata each platform plugin declares.
* ``gateway.pairing.PairingStore.list_approved`` -- approved pairing grants.
* ``plugins.platforms.photon.auth.load_user_numbers`` -- offline view of the
  registered operator number and the assigned Photon line.

Exit codes:

    0    complete (or a matching completion latch already exists)
    10   configuration incomplete -- operator action required
    20   validator/configuration error -- the chart validator needs attention

Exit code 124 is produced by the ``timeout`` in wait.sh, not by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

EXIT_OK = 0
EXIT_INCOMPLETE = 10
EXIT_VALIDATOR_ERROR = 20

#: Bumped when the latch document layout changes.
LATCH_SCHEMA_VERSION = 1
#: Bumped when the *meaning* of a check changes, so existing latches are
#: invalidated and the requirements are revalidated once.
VALIDATOR_VERSION = 1

LATCH_FILENAME = ".helm-onboarding-complete.json"

SUPPORTED_PLATFORMS = ("photon", "telegram", "discord")


class ValidatorError(RuntimeError):
    """The validator itself could not run (import/API incompatibility)."""


class Missing:
    """One unmet requirement, with operator-actionable guidance."""

    __slots__ = ("requirement", "detail")

    def __init__(self, requirement: str, detail: str) -> None:
        self.requirement = requirement
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Missing({self.requirement!r}, {self.detail!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Missing)
            and other.requirement == self.requirement
            and other.detail == self.detail
        )


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_SECRET_NAME_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "ACCOUNT",
    "PHONE",
    "URL",
)

_REDACTED = "***REDACTED***"

_SECRET_SHAPED = re.compile(
    r"\b(?:sk-|xoxb-|xoxp-|ghp_|gho_|github_pat_)[A-Za-z0-9_\-]{8,}"
)


def redact(value: Any) -> str:
    """Scrub credential material out of a message before it is logged.

    Upstream client exceptions routinely embed a bearer token or a URL with
    embedded credentials.  Anything that looks like the value of a
    secret-shaped environment variable, plus a few well-known token prefixes,
    is replaced wholesale.
    """
    text = str(value)
    for name, raw in os.environ.items():
        if not raw or len(raw) < 6:
            continue
        upper = name.upper()
        if not any(marker in upper for marker in _SECRET_NAME_MARKERS):
            continue
        if raw in text:
            text = text.replace(raw, _REDACTED)
    return _SECRET_SHAPED.sub(_REDACTED, text)


# --------------------------------------------------------------------------
# Requirements + latch
# --------------------------------------------------------------------------


def load_requirements(path: str) -> Dict[str, Any]:
    """Load and normalize the chart-rendered requirements document."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise ValidatorError(f"requirements document not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise ValidatorError(
            f"requirements document {path} is unreadable or not valid JSON: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ValidatorError(
            f"requirements document {path} must be a JSON object, got {type(raw).__name__}"
        )

    provider = raw.get("provider", False)
    if not isinstance(provider, bool):
        raise ValidatorError("requirements.provider must be a boolean")

    platforms_raw = raw.get("platforms", [])
    if not isinstance(platforms_raw, list):
        raise ValidatorError("requirements.platforms must be an array")

    platforms: List[str] = []
    for item in platforms_raw:
        if not isinstance(item, str) or not item.strip():
            raise ValidatorError("requirements.platforms entries must be non-empty strings")
        name = item.strip().lower()
        if name not in SUPPORTED_PLATFORMS:
            raise ValidatorError(
                f"unsupported platform requirement {name!r}; "
                f"this validator supports {', '.join(SUPPORTED_PLATFORMS)}"
            )
        if name not in platforms:
            platforms.append(name)

    return {
        "schemaVersion": LATCH_SCHEMA_VERSION,
        "validatorVersion": VALIDATOR_VERSION,
        "provider": provider,
        "platforms": sorted(platforms),
    }


def requirements_hash(requirements: Dict[str, Any]) -> str:
    """Deterministic hash of the normalized requirements document."""
    canonical = json.dumps(
        {
            "schemaVersion": LATCH_SCHEMA_VERSION,
            "validatorVersion": VALIDATOR_VERSION,
            "provider": bool(requirements["provider"]),
            "platforms": sorted(requirements["platforms"]),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hermes_home() -> str:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return home
    return os.path.join(os.path.expanduser("~"), ".hermes")


def default_latch_path() -> str:
    return os.path.join(hermes_home(), LATCH_FILENAME)


def read_latch(path: str) -> Optional[Dict[str, Any]]:
    """Return the latch document, or None when absent/malformed.

    A malformed or schema-mismatched latch is treated exactly like no latch:
    the requirements are revalidated.  It is never deleted here -- files on
    the PVC belong to the operator.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schemaVersion") != LATCH_SCHEMA_VERSION:
        return None
    if not isinstance(data.get("requirementsHash"), str):
        return None
    return data


def latch_matches(path: str, expected_hash: str) -> bool:
    latch = read_latch(path)
    return bool(latch and latch.get("requirementsHash") == expected_hash)


def write_latch(path: str, requirements: Dict[str, Any], expected_hash: str) -> None:
    """Atomically write the completion latch.

    Contains no tokens, phone numbers, user ids or provider account details --
    only the requirement names that were satisfied.
    """
    document = {
        "schemaVersion": LATCH_SCHEMA_VERSION,
        "validatorVersion": VALIDATOR_VERSION,
        "requirementsHash": expected_hash,
        "completedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hermesVersion": hermes_version(),
        "requirements": {
            "provider": bool(requirements["provider"]),
            "platforms": sorted(requirements["platforms"]),
        },
    }
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".helm-onboarding-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise ValidatorError(
            f"could not write the completion latch to {path}: {exc}. "
            "Is the Hermes home volume writable by this container?"
        ) from exc


# --------------------------------------------------------------------------
# Narrow adapters around upstream Hermes internals
# --------------------------------------------------------------------------


def hermes_app_root() -> Optional[str]:
    """Locate the Hermes application tree inside the official image.

    ``/opt/hermes`` is the image's WORKDIR and the target of the editable
    install. Bundled plugin packages (``plugins/platforms/<name>``) have no
    ``__init__.py``, so they are only importable when that tree is on
    ``sys.path`` -- which is the case for the gateway (cwd) but not for a
    script executed from a read-only ConfigMap mount.
    """
    for candidate in (
        os.environ.get("HERMES_APP_ROOT", "").strip(),
        "/opt/hermes",
        os.getcwd(),
    ):
        if (
            candidate
            and os.path.isdir(os.path.join(candidate, "hermes_cli"))
            and os.path.isdir(os.path.join(candidate, "gateway"))
        ):
            return candidate
    return None


def ensure_app_on_path() -> None:
    """Append (never prepend) the app tree so installed packages still win."""
    root = hermes_app_root()
    if root and root not in sys.path:
        sys.path.append(root)


def hermes_version() -> str:
    try:
        import hermes_cli  # type: ignore

        return str(getattr(hermes_cli, "__version__", "unknown"))
    except Exception:
        return "unknown"


def _incompatible(component: str, exc: BaseException) -> ValidatorError:
    return ValidatorError(
        f"{component} validator is incompatible with Hermes {hermes_version()}; "
        f"update the chart validator ({type(exc).__name__}: {redact(exc)})"
    )


def prepare_environment() -> None:
    """Load ``$HERMES_HOME/.env`` exactly like the gateway does at startup."""
    ensure_app_on_path()
    try:
        from hermes_cli.env_loader import load_hermes_dotenv  # type: ignore
    except Exception as exc:
        raise _incompatible("environment", exc)
    try:
        load_hermes_dotenv(hermes_home=hermes_home())
    except Exception as exc:
        raise ValidatorError(
            f"failed to load {hermes_home()}/.env: {redact(exc)}"
        ) from exc


def resolve_provider_runtime() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the runtime LLM provider.

    Returns ``(runtime, None)`` on success or ``(None, reason)`` when the
    local configuration simply does not select a usable provider yet.  Raises
    :class:`ValidatorError` when the upstream API itself is unusable.
    """
    try:
        from hermes_cli.auth import AuthError  # type: ignore
        from hermes_cli.runtime_provider import resolve_runtime_provider  # type: ignore
    except Exception as exc:
        raise _incompatible("provider", exc)

    try:
        runtime = resolve_runtime_provider()
    except AuthError as exc:
        return None, redact(exc)
    except ValueError as exc:
        # e.g. `providers.<name>.enabled: false` for the selected provider.
        return None, redact(exc)
    except Exception as exc:
        raise _incompatible("provider", exc)

    if not isinstance(runtime, dict):
        raise ValidatorError(
            "provider validator is incompatible with Hermes "
            f"{hermes_version()}; resolve_runtime_provider() returned "
            f"{type(runtime).__name__}, expected a dict"
        )
    return runtime, None


def secret_is_usable(value: Any) -> bool:
    try:
        from hermes_cli.auth import has_usable_secret  # type: ignore
    except Exception as exc:
        raise _incompatible("provider", exc)
    try:
        return bool(has_usable_secret(value))
    except Exception as exc:
        raise _incompatible("provider", exc)


_gateway_config_cache: Any = None


def gateway_config() -> Any:
    """Load the gateway configuration (config.yaml + env overrides)."""
    global _gateway_config_cache
    if _gateway_config_cache is not None:
        return _gateway_config_cache
    try:
        from gateway.config import load_gateway_config  # type: ignore
    except Exception as exc:
        raise _incompatible("platform", exc)
    try:
        _gateway_config_cache = load_gateway_config()
    except Exception as exc:
        raise ValidatorError(
            f"gateway configuration failed to load: {redact(exc)}"
        ) from exc
    return _gateway_config_cache


def platform_enum(name: str) -> Any:
    try:
        from gateway.config import Platform  # type: ignore
    except Exception as exc:
        raise _incompatible(name, exc)
    try:
        return Platform(name)
    except Exception as exc:
        raise ValidatorError(
            f"{name} is not a known platform in Hermes {hermes_version()}; "
            "update the chart validator or the required platform list"
        ) from exc


def platform_registry_entry(name: str) -> Any:
    """Return the platform plugin's registry entry, or None when absent."""
    try:
        from gateway.platform_registry import platform_registry  # type: ignore
    except Exception as exc:
        raise _incompatible(name, exc)
    try:
        from hermes_cli.plugins import discover_plugins  # type: ignore

        discover_plugins()
    except Exception:
        # Plugin discovery is best-effort; load_gateway_config() also runs it.
        pass
    try:
        return platform_registry.get(name)
    except Exception as exc:
        raise _incompatible(name, exc)


def connected_platform_values() -> set:
    """Platform names Hermes itself considers enabled *and* configured."""
    config = gateway_config()
    try:
        return {str(p.value) for p in config.get_connected_platforms()}
    except Exception as exc:
        raise _incompatible("platform", exc)


def approved_pairings(name: str) -> int:
    try:
        from gateway.pairing import PairingStore  # type: ignore
    except Exception as exc:
        raise _incompatible(name, exc)
    try:
        return len(PairingStore().list_approved(name) or [])
    except Exception as exc:
        raise _incompatible(name, exc)


def photon_user_numbers() -> Tuple[Optional[str], Optional[str]]:
    try:
        from plugins.platforms.photon.auth import load_user_numbers  # type: ignore
    except Exception as exc:
        raise _incompatible("photon", exc)
    try:
        numbers = load_user_numbers()
    except Exception as exc:
        raise _incompatible("photon", exc)
    try:
        phone, assigned = numbers
    except (TypeError, ValueError) as exc:
        raise _incompatible("photon", exc)
    return phone, assigned


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def _is_truthy(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def check_provider() -> List[Missing]:
    """Require a usable local LLM provider configuration."""
    runtime, reason = resolve_provider_runtime()
    if runtime is None:
        return [
            Missing(
                "provider",
                "no usable LLM provider is configured "
                f"({reason}). Run `hermes model` to select a provider, or "
                "supply the provider's API key through the chart Secret.",
            )
        ]

    provider = str(runtime.get("provider") or "unknown")
    if not secret_is_usable(runtime.get("api_key")):
        return [
            Missing(
                "provider",
                f"provider {provider!r} was selected but no usable credential "
                "resolved for it. Run `hermes model` to pick a provider you "
                "have credentials for, or set the matching API key in the "
                "chart Secret.",
            )
        ]
    return []


def _authorization_missing(name: str, entry: Any) -> Optional[str]:
    """Return a reason string when nobody could ever talk to this platform.

    Any one of these makes the authorization configuration internally valid:
    an allow-all switch, a non-empty allowlist, an existing approved pairing,
    or an unauthorized-DM behavior of ``pair`` (self-service pairing).  Only
    ``ignore`` with no allowlist and no grants is a dead end.
    """
    if _is_truthy(os.environ.get("GATEWAY_ALLOW_ALL_USERS")):
        return None

    allow_all_env = str(getattr(entry, "allow_all_env", "") or "")
    if allow_all_env and _is_truthy(os.environ.get(allow_all_env)):
        return None

    allowed_users_env = str(getattr(entry, "allowed_users_env", "") or "")
    if allowed_users_env and str(os.environ.get(allowed_users_env, "")).strip():
        return None

    if approved_pairings(name) > 0:
        return None

    config = gateway_config()
    try:
        behavior = config.get_unauthorized_dm_behavior(platform_enum(name))
    except Exception as exc:
        raise _incompatible(name, exc)
    if behavior == "pair":
        return None

    return (
        f"nobody is authorized on {name}: unauthorized DMs are set to "
        f"{behavior!r}, no approved pairing exists, and neither "
        f"{allowed_users_env or 'the allowlist'} nor an allow-all switch is "
        "set. Approve a user with `hermes pairing`, set the allowlist, or "
        "switch the unauthorized DM behavior back to 'pair'."
    )


def _photon_extra_checks(entry: Any) -> List[Missing]:
    phone, assigned = photon_user_numbers()
    missing: List[Missing] = []
    if not phone:
        missing.append(
            Missing(
                "platform:photon",
                "no registered Photon user/phone number was found. "
                "Run `hermes photon setup` and complete phone registration.",
            )
        )
    if not assigned:
        missing.append(
            Missing(
                "platform:photon",
                "no assigned Photon line was found for the registered user. "
                "Run `hermes photon setup` and finish line assignment.",
            )
        )
    return missing


_EXTRA_PLATFORM_CHECKS: Dict[str, Callable[[Any], List[Missing]]] = {
    "photon": _photon_extra_checks,
}


def check_platform(name: str) -> List[Missing]:
    """Validate that one platform is locally configured and runnable."""
    key = f"platform:{name}"
    config = gateway_config()
    platform = platform_enum(name)
    entry = platform_registry_entry(name)
    hint = str(getattr(entry, "install_hint", "") or "").strip()

    platform_config = None
    try:
        platform_config = config.platforms.get(platform)
    except Exception as exc:
        raise _incompatible(name, exc)

    if platform_config is None or not getattr(platform_config, "enabled", False):
        detail = f"the {name} platform is not enabled in the Hermes configuration."
        return [Missing(key, f"{detail} {hint}".strip())]

    missing: List[Missing] = []

    if name not in connected_platform_values():
        detail = (
            f"{name} is enabled but its credentials do not resolve, so Hermes "
            "does not consider it connected."
        )
        missing.append(Missing(key, f"{detail} {hint}".strip()))

    check_fn = getattr(entry, "check_fn", None) if entry is not None else None
    if check_fn is not None:
        try:
            dependencies_ok = bool(check_fn())
        except Exception as exc:
            raise _incompatible(name, exc)
        if not dependencies_ok:
            detail = f"runtime dependencies for {name} are not installed."
            missing.append(Missing(key, f"{detail} {hint}".strip()))

    extra_check = _EXTRA_PLATFORM_CHECKS.get(name)
    if extra_check is not None:
        missing.extend(extra_check(entry))

    authorization = _authorization_missing(name, entry)
    if authorization:
        missing.append(Missing(key, authorization))

    return missing


def evaluate(requirements: Dict[str, Any]) -> List[Missing]:
    prepare_environment()
    missing: List[Missing] = []
    if requirements["provider"]:
        missing.extend(check_provider())
    for name in requirements["platforms"]:
        missing.extend(check_platform(name))
    return missing


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _describe(requirements: Dict[str, Any]) -> str:
    parts = []
    if requirements["provider"]:
        parts.append("provider")
    parts.extend(requirements["platforms"])
    return ", ".join(parts) if parts else "(nothing)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check.py",
        description="Validate Hermes onboarding requirements exactly once.",
    )
    parser.add_argument(
        "--requirements",
        required=True,
        help="Path to the chart-rendered requirements.json document.",
    )
    parser.add_argument(
        "--latch",
        default=None,
        help=f"Completion latch path (default: $HERMES_HOME/{LATCH_FILENAME}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore an existing completion latch and revalidate.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit a machine-readable result document on stdout.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        requirements = load_requirements(args.requirements)
    except ValidatorError as exc:
        print(f"onboarding: VALIDATOR ERROR: {redact(exc)}", file=sys.stderr)
        return EXIT_VALIDATOR_ERROR

    expected_hash = requirements_hash(requirements)
    latch_path = args.latch or default_latch_path()

    if not args.force and latch_matches(latch_path, expected_hash):
        message = (
            "onboarding: completion latch matches the current requirements "
            f"({_describe(requirements)}); starting Hermes."
        )
        if args.as_json:
            print(
                json.dumps(
                    {
                        "status": "complete",
                        "source": "latch",
                        "requirementsHash": expected_hash,
                        "missing": [],
                    }
                )
            )
        else:
            print(message)
        return EXIT_OK

    try:
        missing = evaluate(requirements)
    except ValidatorError as exc:
        if args.as_json:
            print(
                json.dumps(
                    {
                        "status": "validator_error",
                        "error": redact(exc),
                        "requirementsHash": expected_hash,
                    }
                )
            )
        print(f"onboarding: VALIDATOR ERROR: {redact(exc)}", file=sys.stderr)
        return EXIT_VALIDATOR_ERROR

    if missing:
        if args.as_json:
            print(
                json.dumps(
                    {
                        "status": "incomplete",
                        "requirementsHash": expected_hash,
                        "missing": [
                            {"requirement": m.requirement, "detail": m.detail}
                            for m in missing
                        ],
                    }
                )
            )
        else:
            print(
                "onboarding: configuration incomplete "
                f"(required: {_describe(requirements)})"
            )
            for item in missing:
                print(f"onboarding:   - {item.requirement}: {redact(item.detail)}")
        return EXIT_INCOMPLETE

    try:
        write_latch(latch_path, requirements, expected_hash)
    except ValidatorError as exc:
        print(f"onboarding: VALIDATOR ERROR: {redact(exc)}", file=sys.stderr)
        return EXIT_VALIDATOR_ERROR

    if args.as_json:
        print(
            json.dumps(
                {
                    "status": "complete",
                    "source": "validated",
                    "requirementsHash": expected_hash,
                    "missing": [],
                }
            )
        )
    else:
        print(
            "onboarding: all requirements satisfied "
            f"({_describe(requirements)}); latch written to {latch_path}."
        )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
