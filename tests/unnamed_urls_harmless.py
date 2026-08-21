"""An unnamed route that addresses no tenant object, so the sweep ignores it."""

from django.http import HttpRequest, HttpResponse
from django.urls import path


def anonymous_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse("")


urlpatterns = [
    path("anonymous/", anonymous_view),
]
