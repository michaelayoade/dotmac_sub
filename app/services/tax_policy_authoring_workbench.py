"""Bounded cursor-backed catalog pickers and server-owned publication forms."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from dotmac_tax import (
    ListTaxAuthoritiesV1,
    ListTaxCodesV1,
    ListTaxJurisdictionsV1,
    ListTaxRuleHistoryV2,
    TaxRuleHistoryPageV2,
    get_tax_code,
    get_tax_jurisdiction,
    get_tax_rule,
    list_tax_authorities,
    list_tax_codes,
    list_tax_jurisdictions,
    list_tax_rule_history_v2,
)
from pydantic import Field
from sqlalchemy.orm import Session

from app.services.action_forms import (
    ActionConfirmation,
    ActionField,
    ActionFieldKind,
    ActionForm,
    ActionFormSubmission,
    ActionOption,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.permission_admission import PermissionAdmission
from app.services.permission_authorization import (
    PermissionPrincipal,
    PermissionPrincipalKind,
    actor_holds_admin_role,
    has_permission,
)
from app.services.tax_policy_authoring import READ_PERMISSION, WRITE_PERMISSION, admit
from app.services.tax_policy_authoring_contracts import (
    AuthoringOutcome,
    AuthorityDraft,
    CodeDraft,
    DependencySelection,
    FormInput,
    JurisdictionDraft,
    Operation,
    PolicyIdentity,
    PreviewDraft,
    RuleDraft,
    SupersessionDraft,
)

BASE_URL = "/admin/billing/tax-policy"
DRAFT_TYPES: dict[Operation, type[FormInput]] = {
    Operation.authority: AuthorityDraft,
    Operation.jurisdiction: JurisdictionDraft,
    Operation.code: CodeDraft,
    Operation.initial_rule: RuleDraft,
    Operation.supersede: SupersessionDraft,
    Operation.preview: PreviewDraft,
}


class WorkbenchQuery(FormInput):
    catalog: Literal["authorities", "jurisdictions", "codes"] = "authorities"
    cursor: str | None = Field(default=None, max_length=1024)
    page_size: int = Field(default=25, ge=1, le=100)
    authority_id: UUID | None = None
    jurisdiction_id: UUID | None = None
    tax_code_id: UUID | None = None
    tax_code: str | None = Field(default=None, max_length=100)
    jurisdiction_code: str | None = Field(default=None, max_length=100)
    history_cursor: str | None = Field(default=None, max_length=1024)
    dependency_predecessor_version: int | None = Field(default=None, ge=1)
    dependency_target: PolicyIdentity | None = None
    dependency_selected_codes: tuple[UUID, ...] = Field(default=(), max_length=100)
    dependency_prior_jurisdiction_id: UUID | None = None

    def url(
        self, *, cursor: str | None = None, history_cursor: str | None = None
    ) -> str:
        values = self.model_dump(mode="json", exclude_none=True)
        if self.dependency_target:
            values["dependency_target"] = self.dependency_target.model_dump_json()
        if self.dependency_target or self.dependency_selected_codes:
            values["dependency_selected_codes"] = json.dumps(
                [str(identifier) for identifier in self.dependency_selected_codes]
            )
        else:
            values.pop("dependency_selected_codes", None)
        values.pop("cursor", None)
        values.pop("history_cursor", None)
        if cursor:
            values["cursor"] = cursor
        if history_cursor:
            values["history_cursor"] = history_cursor
        return f"{BASE_URL}?{urlencode(values)}"

    def author_url(self, *, predecessor_version: int | None = None) -> str:
        operation = (
            Operation.supersede if predecessor_version else Operation.initial_rule
        )
        values = {
            key: value
            for key, value in self.model_dump(mode="json", exclude_none=True).items()
            if key
            in {"tax_code_id", "jurisdiction_id", "jurisdiction_code", "tax_code"}
        }
        if predecessor_version:
            values["predecessor_version"] = predecessor_version
        return f"{BASE_URL}/author/{operation.value}?{urlencode(values)}"

    def dependency_picker_url(
        self, *, predecessor_version: int | None = None
    ) -> str | None:
        if not (
            self.tax_code_id
            and self.jurisdiction_id
            and self.jurisdiction_code
            and self.tax_code
        ):
            return None
        return WorkbenchQuery(
            catalog="codes",
            jurisdiction_id=self.jurisdiction_id,
            jurisdiction_code=self.jurisdiction_code,
            dependency_predecessor_version=predecessor_version,
            dependency_prior_jurisdiction_id=self.jurisdiction_id,
            dependency_target=PolicyIdentity(
                tax_code_id=self.tax_code_id,
                jurisdiction_id=self.jurisdiction_id,
                jurisdiction_code=self.jurisdiction_code,
                tax_code=self.tax_code,
            ),
        ).url()

    def add_dependency_url(self, *, tax_code_id: UUID, jurisdiction_id: UUID) -> str:
        selected = self.dependency_selected_codes
        if tax_code_id not in selected:
            selected = (*selected, tax_code_id)
        return self.model_copy(
            update={
                "dependency_selected_codes": selected,
                "dependency_prior_jurisdiction_id": self.dependency_prior_jurisdiction_id
                or jurisdiction_id,
            }
        ).url(cursor=self.cursor)

    def dependency_author_url(self, *, all_prior: bool = False) -> str | None:
        if not self.dependency_target or not self.dependency_prior_jurisdiction_id:
            return None
        values = self.dependency_target.model_dump(mode="json")
        values.update(
            dependency_selection="all_prior" if all_prior else "tax_code_ids",
            dependency_prior_jurisdiction_id=str(self.dependency_prior_jurisdiction_id),
            dependency_tax_code_ids=json.dumps(
                []
                if all_prior
                else [str(identifier) for identifier in self.dependency_selected_codes]
            ),
        )
        operation = (
            Operation.supersede
            if self.dependency_predecessor_version
            else Operation.initial_rule
        )
        if self.dependency_predecessor_version:
            values["predecessor_version"] = self.dependency_predecessor_version
        return f"{BASE_URL}/author/{operation.value}?{urlencode(values)}"


@dataclass(frozen=True, slots=True)
class CatalogChoice:
    identifier: UUID
    code: str
    name: str
    status: str
    context: str
    next_url: str
    dependency_url: str | None = None


@dataclass(frozen=True, slots=True)
class Workbench:
    query: WorkbenchQuery
    choices: tuple[CatalogChoice, ...]
    next_url: str | None
    history: TaxRuleHistoryPageV2 | None
    history_next_url: str | None
    observed_at: datetime
    can_author: bool


def browse_policy_catalog(
    db: Session, *, admission: PermissionAdmission, query: WorkbenchQuery
) -> Workbench:
    admit(admission=admission, permission=READ_PERMISSION)
    choices: list[CatalogChoice] = []
    if query.catalog == "authorities":
        authorities = list_tax_authorities(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            request=ListTaxAuthoritiesV1(
                page_size=query.page_size, cursor=query.cursor
            ),
        )
        next_cursor = authorities.next_cursor
        for row in authorities.items:
            choices.append(
                CatalogChoice(
                    row.authority_id,
                    row.code,
                    row.name,
                    row.status,
                    row.authority_level_code or "Level unspecified",
                    f"{BASE_URL}?{urlencode({'catalog': 'jurisdictions', 'authority_id': str(row.authority_id)})}",
                )
            )
    elif query.catalog == "jurisdictions":
        jurisdictions = list_tax_jurisdictions(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            request=ListTaxJurisdictionsV1(
                page_size=query.page_size,
                cursor=query.cursor,
                authority_id=query.authority_id,
            ),
        )
        next_cursor = jurisdictions.next_cursor
        for jurisdiction in jurisdictions.items:
            choices.append(
                CatalogChoice(
                    jurisdiction.jurisdiction_id,
                    jurisdiction.code,
                    jurisdiction.name,
                    jurisdiction.status,
                    f"{jurisdiction.country_code} · {jurisdiction.currency.code}",
                    f"{BASE_URL}?{urlencode({'catalog': 'codes', 'jurisdiction_id': str(jurisdiction.jurisdiction_id), 'jurisdiction_code': jurisdiction.code})}",
                )
            )
    else:
        codes = list_tax_codes(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            request=ListTaxCodesV1(
                page_size=query.page_size,
                cursor=query.cursor,
                jurisdiction_id=query.jurisdiction_id,
            ),
        )
        next_cursor = codes.next_cursor
        for code in codes.items:
            choices.append(
                CatalogChoice(
                    code.tax_code_id,
                    code.code,
                    code.name,
                    code.status,
                    code.tax_kind_code,
                    f"{BASE_URL}?{urlencode({'catalog': 'codes', 'jurisdiction_id': str(code.jurisdiction_id), 'tax_code_id': str(code.tax_code_id), 'tax_code': code.code, **({'jurisdiction_code': query.jurisdiction_code} if query.jurisdiction_code else {})})}",
                    query.add_dependency_url(
                        tax_code_id=code.tax_code_id,
                        jurisdiction_id=code.jurisdiction_id,
                    )
                    if query.dependency_target
                    and (
                        len(query.dependency_selected_codes) < 100
                        or code.tax_code_id in query.dependency_selected_codes
                    )
                    else None,
                )
            )
    history = (
        list_tax_rule_history_v2(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            request=ListTaxRuleHistoryV2(
                tax_code_id=query.tax_code_id,
                page_size=query.page_size,
                cursor=query.history_cursor,
            ),
        )
        if query.tax_code_id
        else None
    )
    return Workbench(
        query=query,
        choices=tuple(choices),
        next_url=query.url(cursor=next_cursor, history_cursor=query.history_cursor)
        if next_cursor
        else None,
        history=history,
        history_next_url=query.url(
            cursor=query.cursor, history_cursor=history.next_cursor
        )
        if history and history.next_cursor
        else None,
        observed_at=datetime.now(UTC),
        can_author=has_permission(
            auth=PermissionPrincipal(
                principal_id=admission.principal_id,
                principal_type=PermissionPrincipalKind.system_user,
                roles=frozenset({"admin"})
                if actor_holds_admin_role(db=db, actor=f"user:{admission.principal_id}")
                else frozenset(),
                scopes=frozenset(),
            ),
            db=db,
            permission_key=WRITE_PERMISSION,
        ),
    )


_HELP = {
    "inclusive": "Enter true or false explicitly; no inclusive policy is inferred.",
    "dependency_selection": "One prior signature only. Dependent policies require source_plus_dependency_tax; Tax validates the relationship.",
    "dependency_prior_jurisdiction_id": "Prior-purpose jurisdiction UUID from the bounded code picker. Complete all four prior signature fields explicitly.",
    "dependency_tax_code_ids": "Selected prior code UUIDs as a JSON array. Use the bounded code picker; all_prior requires an empty array.",
    "prior_determination_set_ids": "JSON array of actual sealed V2 prior result UUIDs. No prior amount or fingerprint is entered or invented here.",
    "base_amount": "Original commercial source amount; never add prior tax. Tax owns the dependent base.",
    "preview_mode": "Dependent rules require verified prior V2 evidence and its original commercial identity. Preview never publishes policy.",
    "rate": "Decimal proportion, not percent. Leave blank for methods without a rate.",
    "recoverable_rate": "Decimal proportion supplied by the approved policy.",
    "fixed_amount": "Exact amount in the stated currency; blank when not applicable.",
    "bands": 'Progressive bands as JSON: [{"sequence":1,"lower_bound":"0","upper_bound":null,"rate":"0.1"}]. Values are policy data; no bands are inferred.',
    "recognition_basis_code": "Use the approved recognition basis. Native invoice bindings require invoice_expectation.",
    "calculation_base_code": "Tax-owned base code, for example source_amount, source_plus_prior_tax, or source_plus_dependency_tax. No base is inferred.",
    "source_ref": "Stable source identity. Reuse only for the exact same command.",
    "source_version": "Stable source revision. Changed inputs require a new reviewed revision.",
    "predecessor_rule_id": "Exact published predecessor UUID from history; a superseded leaf is refused.",
    "predecessor_version": "Exact published predecessor version. Refresh history after a stale refusal.",
    "effective_from": "ISO date YYYY-MM-DD; supersession starts on this explicit date.",
    "effective_to": "Optional ISO date YYYY-MM-DD.",
    "minor_units": "Explicit currency fractional digits. Amounts with greater precision are refused.",
}


def authoring_form(
    *, operation: Operation, submission: ActionFormSubmission | None = None
) -> ActionForm:
    fields: list[ActionField] = []
    for name, info in DRAFT_TYPES[operation].model_fields.items():
        kind = ActionFieldKind.text
        options: tuple[ActionOption, ...] = ()
        value = ""
        if name in {"bands", "dependency_tax_code_ids", "prior_determination_set_ids"}:
            kind = ActionFieldKind.textarea
            value = "[]"
        elif name == "dependency_selection":
            kind = ActionFieldKind.select
            options = tuple(
                ActionOption(item.value, label)
                for item, label in (
                    (DependencySelection.none, "No cross-purpose dependency"),
                    (
                        DependencySelection.all_prior,
                        "All components of one prior signature",
                    ),
                    (
                        DependencySelection.tax_code_ids,
                        "Only the selected prior tax codes",
                    ),
                )
            )
            value = "none"
        elif name == "preview_mode":
            kind = ActionFieldKind.select
            options = (
                ActionOption("current", "Current policy without dependencies"),
                ActionOption(
                    "dependency", "Dependency policy with sealed prior V2 evidence"
                ),
            )
            value = "current"
        fields.append(
            ActionField(
                key=name,
                label=name.replace("_", " ").capitalize(),
                kind=kind,
                required=info.is_required(),
                help_text=_HELP.get(name),
                value=value,
                options=options,
            )
        )
    preview = operation is Operation.preview
    form = ActionForm(
        key=f"tax-policy.{operation.value}",
        title="Preview current persisted policy"
        if preview
        else f"Publish {operation.value.replace('-', ' ')}",
        description="Preview reads published policy only. It does not simulate an unpublished draft or create invoice evidence."
        if preview
        else "Publish through the Tax owner. Published rules and history cannot be edited or deleted here.",
        action_url=f"{BASE_URL}/{operation.value}",
        submit_label="Preview persisted policy"
        if preview
        else "Publish confirmed policy",
        fields=tuple(fields),
        impact=None
        if preview
        else "This publishes effective policy for future determinations. Existing invoice evidence is immutable.",
        confirmation=None
        if preview
        else ActionConfirmation(
            title="Confirm publication",
            message="I reviewed the effective dates, version, source and evidence for this policy.",
        ),
    )
    return form.bind(submission) if submission else form


def policy_form_seed(
    db: Session,
    *,
    admission: PermissionAdmission,
    operation: Operation,
    tax_code_id: UUID,
    predecessor_version: int | None = None,
    jurisdiction_id: UUID | None = None,
    jurisdiction_code: str | None = None,
    tax_code: str | None = None,
    dependency_selection: DependencySelection = DependencySelection.none,
    dependency_prior_jurisdiction_id: UUID | None = None,
    dependency_tax_code_ids: tuple[UUID, ...] = (),
) -> ActionFormSubmission:
    admit(admission=admission, permission=WRITE_PERMISSION)
    values: dict[str, str] = {"tax_code_id": str(tax_code_id)}
    if jurisdiction_id and jurisdiction_code and tax_code:
        code = get_tax_code(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            jurisdiction_id=jurisdiction_id,
            code=tax_code,
        )
        jurisdiction = get_tax_jurisdiction(
            db, tenant_id=OPERATOR_TENANT_ID, code=jurisdiction_code
        )
        if (
            code.tax_code_id != tax_code_id
            or code.jurisdiction_id != jurisdiction.jurisdiction_id
            or jurisdiction.jurisdiction_id != jurisdiction_id
        ):
            raise ValueError("The selected policy identities do not match")
        values.update(
            jurisdiction_id=str(jurisdiction_id),
            jurisdiction_code=jurisdiction.code,
            tax_code=code.code,
            currency_code=jurisdiction.currency.code,
            minor_units=str(jurisdiction.currency.minor_units),
        )
    if operation is Operation.supersede and predecessor_version:
        predecessor = get_tax_rule(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            tax_code_id=tax_code_id,
            version=predecessor_version,
        )
        values.update(
            predecessor_rule_id=str(predecessor.rule_id),
            predecessor_version=str(predecessor.version),
        )
    if dependency_selection is not DependencySelection.none:
        values.update(
            dependency_selection=dependency_selection.value,
            calculation_base_code="source_plus_dependency_tax",
            dependency_tax_code_ids=json.dumps(
                [str(identifier) for identifier in dependency_tax_code_ids]
            ),
        )
        if dependency_prior_jurisdiction_id:
            values["dependency_prior_jurisdiction_id"] = str(
                dependency_prior_jurisdiction_id
            )
    return ActionFormSubmission.from_mapping(f"tax-policy.{operation.value}", values)


def publication_destination(*, outcome: AuthoringOutcome) -> str:
    """Review destination derived only from the successfully published typed scope."""
    if outcome.operation is Operation.authority:
        return WorkbenchQuery(
            catalog="jurisdictions", authority_id=outcome.entity_id
        ).url()
    if outcome.operation is Operation.jurisdiction:
        return WorkbenchQuery(
            catalog="codes",
            jurisdiction_id=outcome.entity_id,
            jurisdiction_code=outcome.jurisdiction_code,
        ).url()
    if outcome.operation in {
        Operation.code,
        Operation.initial_rule,
        Operation.supersede,
    }:
        return WorkbenchQuery(
            catalog="codes",
            jurisdiction_id=outcome.jurisdiction_id,
            tax_code_id=outcome.tax_code_id or outcome.entity_id,
            jurisdiction_code=outcome.jurisdiction_code,
            tax_code=outcome.tax_code,
        ).url()
    raise ValueError("A publication outcome is required")
