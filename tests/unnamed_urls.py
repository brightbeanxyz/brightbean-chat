"""A tenant route registered without ``name=``.

Used only by tests/test_idor.py, to prove the sweep refuses to walk past a route
it cannot reverse instead of silently dropping it.
"""

from django.http import HttpRequest, HttpResponse
from django.urls import path


def anonymous_view(request: HttpRequest, workspace_id: str) -> HttpResponse:
    return HttpResponse("")


urlpatterns = [
    path("w/<uuid:workspace_id>/anonymous/", anonymous_view),
]
