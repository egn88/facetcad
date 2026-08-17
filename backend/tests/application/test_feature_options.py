"""Unknown feature options are surfaced, never silently dropped.

The whole system is built on failing loudly rather than guessing: a selector
that no longer resolves stops the build instead of binding to whatever is
nearest. Options were the one place that did not follow the rule. A `counterbore`
on a pad — a real mistake, because holes do take one — was accepted, written to
the document, and silently did nothing. The part built, looked wrong, and
nothing anywhere said why.
"""

from __future__ import annotations

import pytest

from facet.application.features import (
    Option,
    describe_types,
    unknown_options,
    validate_options,
)
from facet.domain.errors import FeatureBuildError
from facet.domain.features import FeatureSpec


def spec(kind: str, **options: object) -> FeatureSpec:
    return FeatureSpec.from_dict({"id": "f", "type": kind, **options})


def test_a_key_the_type_does_not_read_is_refused() -> None:
    with pytest.raises(FeatureBuildError) as caught:
        validate_options(spec("pad", length=5, nonsense_key=42))
    assert "nonsense_key" in str(caught.value)


def test_the_refusal_lists_what_the_type_does_take() -> None:
    with pytest.raises(FeatureBuildError) as caught:
        validate_options(spec("pad", length=5, bogus=1))
    message = str(caught.value)
    # Being told what is wrong without being told what is right is half an
    # error message, and the caller is usually an agent that cannot go and read.
    assert "length" in message
    assert "midplane" in message


def test_an_option_that_belongs_to_another_type_is_refused() -> None:
    """The mistake that prompted this: a counterbore on a pad.

    Plausible enough to write, because holes really do take one — which is
    exactly why silently dropping it was so expensive.
    """
    with pytest.raises(FeatureBuildError) as caught:
        validate_options(spec("pad", length=5, counterbore_depth=2))
    assert "counterbore_depth" in str(caught.value)


def test_a_near_miss_is_named() -> None:
    with pytest.raises(FeatureBuildError) as caught:
        validate_options(spec("pocket", dept=3))
    assert "did you mean 'depth'" in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "options"),
    [
        ("pad", {"length": 5, "midplane": True, "direction": -1}),
        ("pocket", {"depth": 3, "through_all": False}),
        ("hole", {"at": "s.p", "standard": "M6", "fit": "close", "through_all": True}),
        ("fillet", {"edges": "a ^ b", "radius": 2, "on_failure": "skip"}),
        ("chamfer", {"edges": "a ^ b", "distance": 1}),
        ("thread", {"at": "s.p", "standard": "M6", "depth": 8, "modelled": "export"}),
    ],
)
def test_every_documented_option_is_accepted(kind: str, options: dict[str, object]) -> None:
    validate_options(spec(kind, **options))


def test_reserved_document_keys_are_not_options() -> None:
    """`profile`, `targets`, `doc` and `suppressed` belong to every feature."""
    validate_options(
        spec("pad", length=5, profile="outline.outer", doc="a note", suppressed=False)
    )


def test_an_unknown_type_is_left_to_the_registry_to_report() -> None:
    """Two errors for one mistake would bury the useful one."""
    validate_options(spec("nonexistent", whatever=1))


def test_every_registered_type_declares_its_options() -> None:
    for row in describe_types():
        assert row["options"], f"{row['type']} declares none"


def test_the_declaration_says_which_options_are_required() -> None:
    by_type = {row["type"]: row for row in describe_types()}
    required = {
        option["name"]
        for option in by_type["thread"]["options"]  # type: ignore[index]
        if option["required"]
    }
    # The three a thread cannot be built without, and the trio an agent
    # previously had to discover from build errors one at a time.
    assert required == {"at", "standard", "depth"}


def test_option_carries_a_description() -> None:
    assert Option("x", "what x is").describe == "what x is"
    for row in describe_types():
        for option in row["options"]:  # type: ignore[union-attr]
            assert option["describe"], f"{row['type']}.{option['name']} has no description"


# -- loud, but not retroactively destructive -------------------------------
#
# The first version of this refused unknown options on the *rebuild* path. That
# broke three working parts on a live server: they had been saved with a key the
# handler ignored, so the geometry had always been correct, and suddenly every
# rebuild failed and everything downstream was skipped. The rule now depends on
# what is happening — refuse when a feature is being written, warn when an
# existing document is being rebuilt.


def test_writing_a_feature_with_an_unknown_option_is_refused() -> None:
    """At the point of the mistake, where the caller can still fix it."""
    with pytest.raises(FeatureBuildError) as caught:
        validate_options(spec("hole", at="s.p", through=True))
    assert "does not take 'through'" in str(caught.value)
    assert "Remove it or correct it" in str(caught.value)


def test_rebuilding_reports_it_without_refusing() -> None:
    """`unknown_options` is the same knowledge, without the refusal.

    The rebuild path uses this so a document saved before the check existed still
    builds, and says what it is ignoring.
    """
    reported = unknown_options(spec("hole", at="s.p", through=True))
    assert reported is not None
    assert "does not take 'through'" in reported
    assert "through_all" in reported


def test_nothing_is_reported_when_every_key_is_known() -> None:
    assert unknown_options(spec("hole", at="s.p", through_all=True)) is None


def test_the_report_carries_no_consequence_of_its_own() -> None:
    """Two callers, two consequences — so the shared text states neither.

    One ignores the key and one refuses it; if this sentence said 'is ignored'
    the refusal would be a lie, which it briefly was.
    """
    reported = unknown_options(spec("pad", length=5, bogus=1))
    assert reported is not None
    assert "ignored" not in reported
    assert "Remove it" not in reported
