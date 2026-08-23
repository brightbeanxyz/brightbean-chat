"""``GET /api/v1/flows``, ``/tags`` and ``/fields`` — SPEC §17's read endpoints.

Three lists an integration needs before it can do anything useful: the flows it
may start, the tags it may attach, and the custom fields it may set. All read-only
and all gated on ``use_inbox``, the permission every workspace role holds — a
key that can look at contacts can look at the vocabulary they are described in.
"""

from typing import Annotated, Any

from ninja import Query, Router

from apps.api.errors import ApiError
from apps.api.pagination import paginate
from apps.api.requests import ApiRequest
from apps.api.schemas import CustomFieldOut, FlowOut, Page, TagOut
from apps.api.serializers import custom_field_payload, flow_payload, tag_payload
from apps.contacts.models import CustomField, Tag
from apps.flows.models import Flow, FlowStatus
from apps.members.decorators import require_permission

router = Router(tags=["catalog"])


def _page(queryset: Any, *, limit: int | None, cursor: str | None, render: Any) -> dict[str, Any]:
    try:
        page = paginate(queryset, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise ApiError(str(exc), code="invalid_cursor", status=422) from exc
    return {**page, "data": [render(row) for row in page["data"]]}


@router.get("/flows", response=Page[FlowOut], url_name="flows_list")
@require_permission("use_inbox")
def list_flows(
    request: ApiRequest,
    status: Annotated[str | None, Query(description='"draft", "active" or "archived". Default: all.')] = None,
    limit: Annotated[int | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """List flows.

    Only an ``active`` flow has a published version, so only an ``active`` flow
    can be started — but the list is not filtered to those by default, because
    an integration building a picker should be able to show a draft greyed out
    rather than silently omitted.
    """
    if status is not None and status not in set(FlowStatus.values):
        raise ApiError(
            f"status must be one of {', '.join(sorted(FlowStatus.values))}.",
            code="invalid_request",
            status=422,
        )
    rows = Flow.objects.for_workspace(request.workspace)
    if status is not None:
        rows = rows.filter(status=status)
    return _page(rows.order_by("name", "id"), limit=limit, cursor=cursor, render=flow_payload)


@router.get("/tags", response=Page[TagOut], url_name="tags_list")
@require_permission("use_inbox")
def list_tags(
    request: ApiRequest,
    limit: Annotated[int | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    rows = Tag.objects.for_workspace(request.workspace).order_by("name", "id")
    return _page(rows, limit=limit, cursor=cursor, render=tag_payload)


@router.get("/fields", response=Page[CustomFieldOut], url_name="fields_list")
@require_permission("use_inbox")
def list_fields(
    request: ApiRequest,
    limit: Annotated[int | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    rows = CustomField.objects.for_workspace(request.workspace).order_by("name", "id")
    return _page(rows, limit=limit, cursor=cursor, render=custom_field_payload)
