"""Fixtures shared by more than one module in this app's suite.

``tests/support.py`` is deliberately left alone: it is a shared file, and
nothing outside ``apps/contacts`` needs contact builders — the IDOR sweep builds
its own victims in ``tests/idor.py``, the same way it does for invitations.
"""

import pytest

from apps.contacts import services
from apps.contacts.models import CustomFieldType


@pytest.fixture
def workspace(tenancy):
    return tenancy.workspace


@pytest.fixture
def contact(db, workspace):
    return services.create_contact(workspace, first_name="Ada", last_name="Lovelace", email="ada@example.test")


@pytest.fixture
def other_contact(db, workspace):
    return services.create_contact(workspace, first_name="Bob", email="bob@example.test")


@pytest.fixture
def tag(db, workspace):
    created, _ = services.get_or_create_tag(workspace, "VIP")
    return created


@pytest.fixture
def custom_field(db, workspace):
    return services.create_custom_field(workspace, name="Plan", field_type=CustomFieldType.TEXT)
