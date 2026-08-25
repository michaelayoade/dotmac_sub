"""Measure the direct external-connector surfaces still in Sub's own runtime.

## Why this lives in Sub and not only in the fleet sweep

`dotmac_starter_mt/scripts/external_connector_sweep.py` measures five sibling
repositories from one checkout. That sweep can only see `dotmac_sub` when a
`dotmac_sub` checkout happens to sit beside the starter — which never happens in
starter CI. Run there it reports `UNMEASURED`, and a repository that is
unmeasured by the only gate is ungated. Sub's connector surface is therefore
measured by Sub's own CI, against Sub's own tree, on every pull request.

## Where the rules come from

Every constant and predicate below is a port of the Governance conformance
engine's `_external_connector` rule family (dotmac_governance ADR 0010,
`standards_control/engine.py`). This module is deliberately a PORT and not an
invention: the moment ADR 0010 is accepted and Sub repins, the engine runs the
same rules against `external_connector_surface` in the profile, and this module
becomes the local mirror of a centrally-owned contract rather than a second
opinion. It reads its thresholds from that same profile object (see
`staged_profile`) so there is exactly ONE set of numbers in the repository and
nothing to drift.

Until then ADR 0010 is `Proposed`, there is no accepted Governance revision to
pin, and this module is the only thing enforcing the count. That is the point:
a central sweep must never be the only gate.

## What is counted, and what is deliberately not

Six categories, closed (ADR 0010 § "The six categories"). Precision is chosen
over recall: an HTTP call through an unusual wrapper, a webhook route whose path
literal is computed, and a credential named for a provider the list does not
know are all invisible. An honest undercount that shrinks beats an overcount
that gets switched off.

AST, never grep — `import httpx` inside a docstring is not a connector. A source
that cannot be parsed FAILS CLOSED as `unmeasurable` rather than scoring zero.

## The corrected cursor classification

`checkpoint`, `syncstate` and `synccursor` name durable progress over a stream
and count on their own. A bare `*Cursor` must ALSO name a feed, because "cursor"
is equally the ordinary word for a pagination cursor, a DBAPI cursor and a
rotation pointer. Sub is where that correction was found: the fleet sweep
miscounted `InboxTeamRoundRobinCursor` (`app/models/team_inbox.py`) — durable
per-team ROTATION state for Inbox assignment, foreign-keyed to local
`service_teams`, carrying `last_assigned_person_id` and `rotation_count`, with
zero external references — purely because "cursor" was a substring hint. The
COLUMN net is unchanged, which is why narrowing the NAME rule costs no recall on
anything that actually stores a watermark.
"""

from __future__ import annotations

import ast
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tests.architecture.source_index import python_ast, source_text

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The staged schema-6 profile. It is a SEPARATE file from the live
#: `.dotmac/standards-profile.json` on purpose: the live profile is schema 5 and
#: is evaluated by the accepted, pinned Governance engine on every pull request.
#: Migrating it to schema 6 today would make the accepted engine refuse it and
#: take the required `Dotmac engineering standards` check down. The two files
#: merge into one in the change that pins the accepted ADR 0010 revision.
STAGED_PROFILE = REPO_ROOT / ".dotmac" / "standards-profile.next.json"
LIVE_PROFILE = REPO_ROOT / ".dotmac" / "standards-profile.json"

#: Literal that must sit anywhere a real Governance revision will go until
#: ADR 0010 is accepted and merged to canonical `main`. Nothing may be green
#: against it.
PENDING_PIN = "PENDING-APPROVAL"

# --- Ported rule family (dotmac_governance ADR 0010) -----------------------

#: Directory names that are never application runtime, even inside a declared
#: runtime root. A connector faked in a test is how a connector is verified.
EXCLUDED_PARTS = frozenset(
    {"tests", "test", "__pycache__", "migrations", "alembic", "node_modules", ".venv"}
)
#: HTTP client libraries. Importing one means making outbound calls here rather
#: than asking a control plane to make them.
HTTP_CLIENTS = frozenset({"httpx", "requests", "aiohttp", "urllib3", "http.client"})
#: Method names that actually issue a request. Importing a client for a type
#: annotation is not a connector, and this is what separates the two.
REQUEST_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "request", "stream", "send"}
)
#: Route literals that make a handler a provider callback rather than a product
#: API. Matched against the decorator's path constant.
WEBHOOK_PATH_HINTS = ("webhook", "callback", "/hooks", "notify-url", "ipn")
#: Authenticity verification of an inbound provider payload.
WEBHOOK_VERIFY_HINTS = ("verify_signature", "verify_webhook", "check_signature")
#: Provider credential material held in Sub's own configuration. Narrow on
#: purpose: a generic `api_key` is Sub's own key more often than a provider's,
#: so provider credential material must be NAMED with its provider.
PROVIDER_TOKENS = (
    "stripe",
    "twilio",
    "paystack",
    "flutterwave",
    "meta_",
    "facebook",
    "instagram",
    "whatsapp",
    "erpnext",
    "sendgrid",
    "mailgun",
    "smtp_",
    "s3_",
    "minio",
)
CREDENTIAL_SUFFIXES = (
    "_api_key",
    "_secret",
    "_secret_key",
    "_token",
    "_access_token",
    "_webhook_secret",
    "_client_secret",
    "_password",
)
#: Scheduling a connector run from inside the product runtime.
TASK_HINTS = ("celery", "shared_task", "scheduled_task", "periodic_task", "cron")
TASK_SUBJECT_HINTS = ("sync", "connector", "integration", "webhook", "poll", "fetch")
#: Names that already MEAN durable progress over a stream, and so count alone.
FEED_CHECKPOINT_CLASS_HINTS = ("checkpoint", "syncstate", "synccursor")
#: Names that do not. See the module docstring for the miscount that split this.
AMBIGUOUS_CHECKPOINT_CLASS_HINTS = ("cursor",)
#: `meta_` is a provider prefix in a SETTINGS name but an ordinary programming
#: prefix in a CLASS name (`MetadataCursor`), so it does not qualify a cursor.
NOT_A_CLASS_NAME_PROVIDER = frozenset({"meta_"})
#: What turns an ambiguous `*Cursor` into a connector checkpoint: the name says
#: which external feed it is a position in. Provider names count too.
EXTERNAL_FEED_HINTS = (
    "sync",
    "external",
    "integration",
    "connector",
    "feed",
    "ingest",
    "import",
    "poll",
    "replicat",
    "upstream",
    "remote",
    "mirror",
    "provider",
    "webhook",
    "erp",
    *(
        provider.rstrip("_")
        for provider in PROVIDER_TOKENS
        if provider not in NOT_A_CLASS_NAME_PROVIDER
    ),
)
#: The COLUMN net, unchanged by the name rule above: anything that actually
#: stores a watermark counts whatever its class is called.
CHECKPOINT_COLUMN_HINTS = frozenset(
    {"last_synced_at", "sync_cursor", "last_cursor", "watermark"}
)
RETRY_HINTS = ("dead_letter", "deadletter", "retry_backoff", "max_retries", "requeue")

CATEGORIES: tuple[str, ...] = (
    "http_client",
    "webhook_surface",
    "provider_credential",
    "connector_task",
    "sync_checkpoint",
    "delivery_retry",
)


def _imports_http_client(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in HTTP_CLIENTS for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in HTTP_CLIENTS:
                return True
    return False


def _issues_a_request(tree: ast.Module) -> bool:
    """An ``httpx.post(...)``/``client.get(...)``-shaped call.

    Attribute-name matching, not receiver tracking: the receiver could be any
    local name and resolving it needs type inference this module has no business
    doing. Paired with :func:`_imports_http_client`, the false-positive surface
    is a module that both imports a client AND calls something named ``get``.
    """
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in REQUEST_METHODS
        for node in ast.walk(tree)
    )


def _decorator_path_literals(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    literals: list[str] = []
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call):
            literals.extend(
                argument.value.lower()
                for argument in decorator.args
                if isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
            )
    return literals


def _is_webhook_surface(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(
            hint in literal
            for literal in _decorator_path_literals(node)
            for hint in WEBHOOK_PATH_HINTS
        ):
            return True
        if any(hint in node.name.lower() for hint in WEBHOOK_VERIFY_HINTS):
            return True
    return False


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    return []


def _holds_provider_credential(tree: ast.Module) -> bool:
    """A configuration attribute naming BOTH a provider and secret material."""
    for node in ast.walk(tree):
        for name in _assigned_names(node):
            lowered = name.lower()
            if any(token in lowered for token in PROVIDER_TOKENS) and any(
                lowered.endswith(suffix) for suffix in CREDENTIAL_SUFFIXES
            ):
                return True
    return False


def _schedules_a_connector(tree: ast.Module, source: str) -> bool:
    lowered = source.lower()
    if not any(hint in lowered for hint in TASK_HINTS):
        return False
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.decorator_list
        and any(hint in node.name.lower() for hint in TASK_SUBJECT_HINTS)
        for node in ast.walk(tree)
    )


def is_checkpoint_class_name(class_name: str) -> bool:
    """Does this class name denote a position in an EXTERNAL feed?

    The class-name rule is the SECONDARY net; a class that declares a watermark
    column is caught below whatever it is called. This rule exists only for feed
    state whose column is named something else — `ErpDomainSyncCursor` stores
    `watermark_at`/`watermark_id`, neither of which is a declared column hint,
    so the NAME is the only thing that sees it.
    """
    lowered = class_name.lower()
    if any(hint in lowered for hint in FEED_CHECKPOINT_CLASS_HINTS):
        return True
    if any(hint in lowered for hint in AMBIGUOUS_CHECKPOINT_CLASS_HINTS):
        return any(hint in lowered for hint in EXTERNAL_FEED_HINTS)
    return False


def _holds_a_checkpoint(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and is_checkpoint_class_name(node.name):
            return True
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.lower() in CHECKPOINT_COLUMN_HINTS
        ):
            return True
    return False


def _owns_delivery_retry(tree: ast.Module, source: str) -> bool:
    if not any(hint in source.lower() for hint in RETRY_HINTS):
        return False
    # Only counts alongside an actual outbound or inbound connector surface: a
    # retry loop around a local database write is not delivery machinery.
    return _imports_http_client(tree) or _is_webhook_surface(tree)


def classify(source: str) -> frozenset[str]:
    """Return every connector category one Python source belongs to.

    Raises ``SyntaxError`` rather than returning an empty set: an unmeasurable
    runtime file must fail closed, never score zero.
    """
    return classify_tree(ast.parse(source), source)


def classify_tree(tree: ast.Module, source: str) -> frozenset[str]:
    """The ported rule family itself, over an already-parsed module.

    Split out so :func:`measure` can read and parse through
    :mod:`tests.architecture.source_index`, which the architecture suite
    already uses to read and parse `app/` once per xdist worker. Only the I/O
    is shared; every rule below stays a rule-for-rule port of the Governance
    engine, and :func:`classify` remains the entry point for the synthetic
    sources the sensitivity proofs plant.
    """
    found: set[str] = set()
    if _imports_http_client(tree) and _issues_a_request(tree):
        found.add("http_client")
    if _is_webhook_surface(tree):
        found.add("webhook_surface")
    if _holds_provider_credential(tree):
        found.add("provider_credential")
    if _schedules_a_connector(tree, source):
        found.add("connector_task")
    if _holds_a_checkpoint(tree):
        found.add("sync_checkpoint")
    if _owns_delivery_retry(tree, source):
        found.add("delivery_retry")
    return frozenset(found)


def _repository_python_files(root: Path) -> tuple[PurePosixPath, ...]:
    """Repository Python sources, including untracked non-ignored files.

    Same enumeration the Governance engine uses, so the local count and the
    engine's count cannot disagree about what a file is.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "*.py",
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        candidates = (PurePosixPath(raw) for raw in result.stdout.split("\0") if raw)
    else:
        candidates = (
            PurePosixPath(path.relative_to(root).as_posix())
            for path in root.rglob("*.py")
        )
    return tuple(
        sorted(
            {relative for relative in candidates if (root / relative).is_file()},
            key=lambda path: path.as_posix(),
        )
    )


def _inside(relative: PurePosixPath, roots: tuple[PurePosixPath, ...]) -> bool:
    return any(relative == root or root in relative.parents for root in roots)


def runtime_files(
    root: Path, runtime_roots: tuple[PurePosixPath, ...]
) -> tuple[PurePosixPath, ...]:
    return tuple(
        relative
        for relative in _repository_python_files(root)
        if _inside(relative, runtime_roots)
        and not EXCLUDED_PARTS & set(relative.parts)
        and not relative.name.startswith("test_")
    )


@dataclass(frozen=True)
class Measurement:
    """What the detector saw, with the files behind every number."""

    files_by_category: dict[str, tuple[str, ...]]
    unmeasurable: tuple[str, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            category: len(self.files_by_category[category]) for category in CATEGORIES
        }


def measure(
    root: Path | None = None, runtime_roots: tuple[PurePosixPath, ...] | None = None
) -> Measurement:
    root = root or REPO_ROOT
    if runtime_roots is None:
        runtime_roots = staged_runtime_roots()
    found: dict[str, list[str]] = {category: [] for category in CATEGORIES}
    unmeasurable: list[str] = []
    for relative in runtime_files(root, runtime_roots):
        absolute = root / relative
        try:
            source = source_text(absolute)
        except (OSError, UnicodeError) as error:
            unmeasurable.append(f"{relative}: {error}")
            continue
        try:
            categories = classify_tree(python_ast(absolute), source)
        except SyntaxError as error:
            unmeasurable.append(f"{relative}: {error}")
            continue
        for category in categories:
            found[category].append(relative.as_posix())
    return Measurement(
        files_by_category={
            category: tuple(sorted(paths)) for category, paths in found.items()
        },
        unmeasurable=tuple(sorted(unmeasurable)),
    )


class StagedProfileError(RuntimeError):
    """The staged connector contract is unreadable, so nothing is measurable."""


def staged_profile() -> dict:
    """Load the ONE profile object that declares the connector surface.

    While ADR 0010 is unaccepted that is the staged schema-6 file; after the pin
    lands it is the live profile. Exactly one of them may declare
    `external_connector_surface` — two would be two thresholds, which is the
    drift this whole record exists to prevent.
    """
    declaring = [
        path
        for path in (STAGED_PROFILE, LIVE_PROFILE)
        if path.is_file()
        and "external_connector_surface" in json.loads(path.read_text("utf-8"))
    ]
    if not declaring:
        raise StagedProfileError(
            "no profile declares `external_connector_surface`; the connector "
            f"ratchet has nothing to enforce. Expected it in {STAGED_PROFILE} "
            f"(while dotmac_governance ADR 0010 is unaccepted) or {LIVE_PROFILE} "
            "(once the accepted revision is pinned)."
        )
    if len(declaring) > 1:
        raise StagedProfileError(
            "both the staged and live profiles declare "
            "`external_connector_surface`; delete the staged copy in the change "
            "that migrates the live profile to schema 6, so there is one "
            f"baseline rather than two: {[str(path) for path in declaring]}"
        )
    return json.loads(declaring[0].read_text("utf-8"))


def staged_runtime_roots() -> tuple[PurePosixPath, ...]:
    surface = staged_profile()["external_connector_surface"]
    return tuple(PurePosixPath(item) for item in surface["runtime_roots"])


def staged_baselines() -> dict[str, int]:
    return dict(staged_profile()["external_connector_surface"]["baselines"])


def ratchet(observed: dict[str, int], baselines: dict[str, int]) -> list[str]:
    """Two-directional, per starter ADR 0018 and Governance ADR 0010.

    Rising means a new direct connector surface landed. FALLING without the
    baseline being lowered in the same change means the detector stopped seeing
    something — which reads as progress, and is the failure a one-directional
    ratchet cannot tell from real retirement.
    """
    failures: list[str] = []
    for category in CATEGORIES:
        if category not in baselines:
            failures.append(
                f"{category}: the profile declares no baseline; the six "
                "categories are closed so a category cannot be dropped to make "
                "a count disappear"
            )
            continue
        want = baselines[category]
        got = observed[category]
        if got > want:
            failures.append(
                f"{category}: {got} measured runtime files exceed the declared "
                f"baseline {want} — a new direct external-connector surface "
                "landed. Route it through the Integrator rather than adding a "
                "provider client, credential, callback, schedule, checkpoint or "
                "retry loop to Sub's own runtime."
            )
        elif got < want:
            failures.append(
                f"{category}: {got} measured runtime files are below the "
                f"declared baseline {want} — lower the baseline in the SAME "
                "change that deletes the code, so the retirement is reviewed "
                "rather than inferred."
            )
    unknown = sorted(set(baselines) - set(CATEGORIES))
    if unknown:
        failures.append(f"the profile declares unknown categories: {unknown}")
    return failures
