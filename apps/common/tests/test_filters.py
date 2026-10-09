"""Reading a list's repeated filter parameters (apps/common/filters.py)."""

import uuid

import pytest
from django.http import QueryDict

from apps.common.filters import MAX_VALUES, multi, parse_uuid


def params(query: str) -> QueryDict:
    return QueryDict(query)


class TestMulti:
    def test_every_value_of_a_repeated_parameter_is_kept(self):
        """``GET.get`` keeps only the last one, which is the bug this exists
        to replace: "Live and Offline" would quietly show only Offline."""
        assert multi(params("status=active&status=offline"), "status") == ["active", "offline"]

    def test_the_result_is_deduplicated_and_sorted_so_tokens_are_stable(self):
        """The inbox folds these into a polling ETag; the same selection in
        another order must be the same token."""
        assert multi(params("s=b&s=a&s=b"), "s") == multi(params("s=a&s=b"), "s") == ["a", "b"]

    def test_unknown_values_are_dropped_not_refused(self):
        assert multi(params("status=active&status=bogus"), "status", allowed=["active", "draft"]) == ["active"]

    def test_blank_values_read_as_no_filter(self):
        """An empty hidden input is how a form says "nothing picked"."""
        assert multi(params("status=&status=%20"), "status") == []

    def test_parse_turns_values_into_what_the_query_wants(self):
        good = uuid.uuid4()

        assert multi(params(f"id={good}&id=not-a-uuid"), "id", parse=parse_uuid) == [good]

    def test_the_number_of_values_is_capped(self):
        many = "&".join(f"tag={i:03d}" for i in range(MAX_VALUES + 20))

        assert len(multi(params(many), "tag")) == MAX_VALUES

    def test_allowed_and_parse_are_exclusive(self):
        with pytest.raises(ValueError):
            multi(params("a=1"), "a", allowed=["1"], parse=int)
