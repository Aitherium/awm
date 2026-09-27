"""Scope parsing and the sibling boundary -- the security property awm exists for."""

from __future__ import annotations

import pytest
from awm.scope import ANCESTOR_DECAY, Scope, ScopeError, visible_scopes


class TestParse:
    def test_three_segments_round_trip(self) -> None:
        s = Scope.parse("acme:alice:proj")
        assert s.parts == ("acme", "alice", "proj")
        assert str(s) == "acme:alice:proj"

    def test_whitespace_around_the_whole_scope_is_trimmed(self) -> None:
        assert str(Scope.parse("  acme:*:*  ")) == "acme:*:*"

    @pytest.mark.parametrize("text", ["acme", "acme:alice", "a:b:c:d", ""])
    def test_wrong_segment_count_is_refused(self, text: str) -> None:
        with pytest.raises(ScopeError):
            Scope.parse(text)

    @pytest.mark.parametrize("text", [None, 3, "   "])
    def test_non_string_or_blank_is_refused(self, text) -> None:
        with pytest.raises(ScopeError):
            Scope.parse(text)  # type: ignore[arg-type]

    @pytest.mark.parametrize("text", ["::", "acme::proj", ":alice:proj"])
    def test_an_empty_segment_is_refused_not_normalised(self, text: str) -> None:
        with pytest.raises(ScopeError, match="must not be empty"):
            Scope.parse(text)

    def test_a_separator_inside_a_part_is_refused(self) -> None:
        with pytest.raises(ScopeError, match="separator"):
            Scope("acme:evil", "alice", "proj")

    @pytest.mark.parametrize("text", ["acme:*:proj", "platform:*:proj"])
    def test_wildcard_user_with_a_named_project_is_refused(self, text: str) -> None:
        # A narrower level under a wildcard is ambiguous -- "that project for
        # every user" -- and no ancestor walk can express it consistently.
        with pytest.raises(ScopeError, match="wildcard user is ambiguous"):
            Scope.parse(text)

    def test_wildcard_user_with_wildcard_project_is_legal(self) -> None:
        assert Scope.parse("acme:*:*").depth == 1


class TestHierarchy:
    def test_depths(self) -> None:
        assert Scope.parse("platform:*:*").depth == 0
        assert Scope.parse("acme:*:*").depth == 1
        assert Scope.parse("acme:alice:*").depth == 2
        assert Scope.parse("acme:alice:proj").depth == 3

    def test_ancestors_are_nearest_first_and_end_at_platform(self) -> None:
        got = [str(s) for s in Scope.parse("acme:alice:proj").ancestors()]
        assert got == ["acme:alice:*", "acme:*:*", "platform:*:*"]
        assert Scope.parse("platform:*:*").ancestors() == []

    def test_visible_scopes_leads_with_the_query(self) -> None:
        q = Scope.parse("acme:alice:*")
        assert [str(s) for s in visible_scopes(q)] == [
            "acme:alice:*", "acme:*:*", "platform:*:*"]

    def test_siblings_never_cover_each_other(self) -> None:
        alice = Scope.parse("acme:alice:*")
        bob = Scope.parse("acme:bob:*")
        assert not alice.covers(bob)
        assert not bob.covers(alice)
        assert alice.weight_for(bob) == 0.0

    def test_tenant_prefix_is_not_a_tenant(self) -> None:
        # "acmecorp".startswith("acme") is the leak segment-wise compare prevents.
        assert not Scope.parse("acme:*:*").covers(Scope.parse("acmecorp:alice:proj"))

    def test_platform_covers_everything(self) -> None:
        assert Scope.parse("platform:*:*").covers(Scope.parse("globex:bob:x"))

    def test_descendant_does_not_cover_ancestor(self) -> None:
        assert not Scope.parse("acme:alice:proj").covers(Scope.parse("acme:alice:*"))

    def test_weight_decays_per_level(self) -> None:
        q = Scope.parse("acme:alice:proj")
        assert q.weight_for(q) == 1.0
        assert Scope.parse("acme:alice:*").weight_for(q) == ANCESTOR_DECAY
        assert Scope.parse("acme:*:*").weight_for(q) == ANCESTOR_DECAY ** 2
        assert Scope.parse("platform:*:*").weight_for(q) == ANCESTOR_DECAY ** 3

    def test_covers_refuses_a_non_scope(self) -> None:
        with pytest.raises(ScopeError):
            Scope.parse("acme:*:*").covers("acme:*:*")  # type: ignore[arg-type]
