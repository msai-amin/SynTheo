"""Isabelle backend: static anti-gaming guards (no Docker) + kernel goldens (image-gated).

The anti-gaming tests are the ADR-004 sequel for the fourth backend — each reproduces a
way a model could get a "verified" label without a real proof, and asserts it doesn't.
The static guards run without the image; the kernel goldens skip cleanly when the
syntheo-isabelle image isn't built (like tests/test_sandbox.py:20-24).
"""
import pytest

from core.verify.isabelle_hol import (
    ISABELLE_IMAGE,
    _image_present,
    check_isabelle,
    detect_embedding,
    extract_theory,
    goal_matches_target,
    has_oops,
    has_sorry,
    introduces_axiom,
    static_rejection,
    theory_name,
)

# A clean, trivially-true theory used to exercise extraction/name/embedding helpers.
CLEAN_THY = """theory Submission
  imports Main
begin
theorem target: "A \\<longrightarrow> A"
  by simp
end"""

SORRY_THY = """theory Submission
  imports Main
begin
theorem target: "A \\<longrightarrow> A"
  sorry
end"""

# Builds and 'proves' its target, but only because it postulated a false axiom — the
# smuggled-axiom cheat. Uses a real, well-typed proposition so it actually type-checks.
AXIOM_THY = """theory Submission
  imports Main
begin
axiomatization where cheat: "(2::nat) = 3"
theorem target: "(2::nat) = 3"
  by (simp add: cheat)
end"""


# --- extraction / helpers (pure) ---

def test_extract_theory_from_fenced_block():
    sample = f"Here is my proof:\n```isabelle\n{CLEAN_THY}\n```\nDone."
    assert extract_theory(sample) == CLEAN_THY


def test_extract_theory_requires_header_and_begin():
    assert extract_theory("```isabelle\njust prose, no theory\n```") is None


def test_theory_name_parsed():
    assert theory_name(CLEAN_THY) == "Submission"


def test_detect_embedding_labels_logic():
    thy = 'theory T imports S5_Embedding begin theorem x: "\\<box> P" by (simp add: S5) end'
    label = detect_embedding(thy)
    assert "S5" in label


# --- anti-gaming guard: sorry (always) / oops (verify path only) (ADR-004 sequel) ---

def test_has_sorry_detects_stub():
    assert has_sorry(SORRY_THY)
    assert not has_sorry(CLEAN_THY)


def test_oops_detected_separately_from_sorry():
    # `oops` is legitimate in a refutation, so it is NOT a pre-run reject; only flagged.
    assert has_oops("theorem t: \"P\" nitpick oops")
    assert not has_oops(CLEAN_THY)
    assert not has_sorry("theorem t: \"P\" nitpick oops")


def test_sorry_never_verifies():
    """A `sorry` proof is the theorem-prover echo block: goal accepted, nothing proved."""
    out = check_isabelle(SORRY_THY, target_statement="A \\<longrightarrow> A")
    assert out["verified"] is False
    assert out["method"] == "isabelle-rejected"
    assert "sorry" in out["detail"]


def test_sorry_word_in_comment_does_not_false_trip():
    thy = CLEAN_THY.replace("by simp", "(* not a sorry, a real proof *) by simp")
    assert not has_sorry(thy)


# --- anti-gaming guard: goal must match the target ---

def test_goal_matches_target_normalizes_whitespace():
    assert goal_matches_target(CLEAN_THY, "A   \\<longrightarrow>   A")
    assert not goal_matches_target(CLEAN_THY, "B \\<longrightarrow> B")


def test_goal_mismatch_rejected_before_kernel():
    """Proving an easier neighbor of the asked question earns nothing."""
    out = check_isabelle(CLEAN_THY, target_statement="God_exists")
    assert out["verified"] is False
    assert out["method"] == "isabelle-rejected"
    assert "target" in out["detail"]


def test_static_rejection_passes_clean_theory():
    assert static_rejection(CLEAN_THY, "A \\<longrightarrow> A") is None


# --- anti-gaming guard: smuggled axioms only bite the verified path ---

def test_introduces_axiom_predicate():
    assert introduces_axiom(AXIOM_THY)
    assert not introduces_axiom(CLEAN_THY)


# --- kernel goldens (require the built image) ---

pytestmark_image = pytest.mark.skipif(
    not _image_present(), reason=f"{ISABELLE_IMAGE} not built")


# The real GoedelGod API (AFP): validity is `[_]`, modal box `\<box>`, lifted exists
# `\<exists>`, God-like `G`; the main theorem T3 is "[\<box> (\<exists> G)]" (necessarily God exists).
GOD_NEC = "[\\<box> (\\<exists> G)]"


@pytestmark_image
def test_known_goedel_scott_verifies():
    """Scott's variant of Gödel's ontological argument checks green (the note's flagship):
    T3 — necessarily, God exists — re-derived and re-checked by the kernel. AFP session
    theories are session-qualified, hence `imports "GoedelGod.GoedelGod"`."""
    thy = f"""theory Submission
  imports "GoedelGod.GoedelGod"
begin
theorem target: "{GOD_NEC}"
  using T3 by simp
end"""
    out = check_isabelle(thy, target_statement=GOD_NEC)
    assert out["verified"] is True, out
    assert out["method"] == "isabelle"


@pytestmark_image
def test_nitpick_countermodel_refuted():
    """A non-theorem is REFUTED by a Nitpick countermodel — a positive result. The
    `oops` here is the normal refutation shape and must NOT be treated as gaming."""
    thy = """theory Submission
  imports Main
begin
theorem target: "(x::nat) = 0"
  nitpick [expect = genuine] oops
end"""
    out = check_isabelle(thy, target_statement=None)
    assert out["method"] == "isabelle-refuted", out
    assert out["verified"] is False


@pytestmark_image
def test_god_nonexistence_refuted():
    """'Necessarily God does not exist' is false under Gödel's axioms — Nitpick exhibits
    a countermodel. The metaphysics-flavored refutation anchor."""
    thy = """theory Submission
  imports "GoedelGod.GoedelGod"
begin
theorem target: "[m\\<not> (\\<exists> G)]"
  nitpick [user_axioms, expect = genuine] oops
end"""
    out = check_isabelle(thy, target_statement=None)
    assert out["method"] in ("isabelle-refuted", "isabelle-unverifiable"), out


@pytestmark_image
def test_smuggled_axiom_verify_downgraded_to_rejected():
    """A proof that closes only because the model postulated a (false) axiom is rejected
    even though the kernel 'accepted' the derivation from it."""
    out = check_isabelle(AXIOM_THY, target_statement="(2::nat) = 3")
    assert out["verified"] is False, out
    assert out["method"] == "isabelle-rejected"
