# PROMPT: Give me unit vectors in R^3 such that cosine-similarity pairs are
#   exactly 1.0, 0.9, 0.85, 0.849, and 0.0 against [1, 0, 0], without
#   rounding. Use these to test a ReIdentifier that returns visitor_id if
#   similarity >= threshold (inclusive), else None. Also cover: register
#   overwrites existing embedding, forget removes from registry, best-match
#   selection when multiple candidates exist.
#
# CHANGES MADE: Cosine similarity on L2-normalised vectors equals their dot
#   product, so all boundary tests are exact with no floating-point surprises.
#   Used np.float32 cast to match production dtype from torchreid.
"""
Phase 4 Tests — Task 4: ReIdentifier  (RED phase)

ReIdentifier extracts L2-normalised appearance embeddings from person crops
and matches them against a registry of previously-seen visitors.

Contract:
  extract_embedding(crop_bgr)
    → np.ndarray of shape (D,), L2-norm == 1.0
    → calls the injected model exactly once per crop

  register(visitor_id, embedding)
    → stores (or overwrites) the embedding for visitor_id
    → does NOT raise on duplicate visitor_ids

  identify(embedding)
    → compares embedding against all registered entries via cosine similarity
    → returns visitor_id of best match if similarity >= threshold (inclusive)
    → returns None when no match, or when registry is empty

  forget(visitor_id)
    → removes visitor_id from registry so future identify() calls skip it

No model weights are downloaded in this test module.  The "model" is a pure
Python callable that returns a synthetic numpy array — blazingly fast and
CI/CD friendly.

Cosine similarity on L2-normalised vectors equals their dot product, so
all boundary calculations below are exact (no floating-point surprises).

Prompt used to design boundary embeddings (AI-assisted):
  "Give me unit vectors in R^3 such that cos_sim pairs are exactly
   1.0, 0.9, 0.85, 0.849, and 0.0 against [1, 0, 0], without rounding."
"""
from __future__ import annotations

import numpy as np
import pytest

from reid import ReIdentifier, SharedRegistry


# ---------------------------------------------------------------------------
# Helpers: synthetic model & embeddings
# ---------------------------------------------------------------------------

def _mock_model(return_value: np.ndarray):
    """Return a callable that ignores the crop and always returns *return_value*."""
    def _model(crop_bgr: np.ndarray) -> np.ndarray:
        return return_value.copy()
    return _model


def _unit(v: list) -> np.ndarray:
    """Return v cast to float32 and L2-normalised."""
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


# Axis-aligned unit vectors — cos_sim between any two = dot product
E_X    = _unit([1.0, 0.0, 0.0])   # reference
E_Y    = _unit([0.0, 1.0, 0.0])   # orthogonal  → sim = 0.0
E_SIM9 = _unit([0.9, np.sqrt(1 - 0.9**2), 0.0])   # sim with E_X = 0.9
E_AT   = _unit([0.85, np.sqrt(1 - 0.85**2), 0.0]) # sim with E_X = 0.85 (at threshold)
E_BELOW = _unit([0.849, np.sqrt(1 - 0.849**2), 0.0])  # sim with E_X ≈ 0.849 (below)

_CROP  = np.zeros((50, 50, 3), dtype=np.uint8)   # synthetic crop, content irrelevant


@pytest.fixture
def reid():
    return ReIdentifier(_mock_model(E_X), threshold=0.85)


# ---------------------------------------------------------------------------
# extract_embedding
# ---------------------------------------------------------------------------

class TestExtractEmbedding:

    def test_returns_numpy_array(self, reid):
        result = reid.extract_embedding(_CROP)
        assert isinstance(result, np.ndarray), "extract_embedding must return np.ndarray"

    def test_output_is_l2_normalised(self, reid):
        """
        Embedding must have unit L2 norm so cosine similarity reduces to a
        dot product — no division-by-zero edge cases at identify() time.
        """
        result = reid.extract_embedding(_CROP)
        assert np.isclose(np.linalg.norm(result), 1.0, atol=1e-5), (
            f"Expected L2 norm == 1.0, got {np.linalg.norm(result):.6f}"
        )

    def test_model_is_called_exactly_once(self):
        """extract_embedding must invoke the model exactly once per call."""
        call_log = []

        def counting_model(crop):
            call_log.append(1)
            return E_X.copy()

        r = ReIdentifier(counting_model, threshold=0.85)
        r.extract_embedding(_CROP)
        assert len(call_log) == 1, (
            f"Model must be called exactly once; was called {len(call_log)} times"
        )

    def test_unnormalised_model_output_is_normalised(self):
        """
        Even if the underlying model returns an unnormalised vector,
        extract_embedding must L2-normalise it before returning.
        """
        raw = np.array([3.0, 4.0, 0.0], dtype=np.float32)  # norm = 5
        r = ReIdentifier(_mock_model(raw), threshold=0.85)
        result = r.extract_embedding(_CROP)
        assert np.isclose(np.linalg.norm(result), 1.0, atol=1e-5), (
            "Raw model output [3,4,0] (norm=5) must be normalised to norm=1"
        )
        expected_normalised = raw / np.linalg.norm(raw)
        assert np.allclose(result, expected_normalised, atol=1e-5)

    def test_output_is_one_dimensional(self, reid):
        result = reid.extract_embedding(_CROP)
        assert result.ndim == 1, f"Embedding must be 1-D, got shape {result.shape}"


# ---------------------------------------------------------------------------
# register / identify — core matching logic
# ---------------------------------------------------------------------------

class TestIdentify:

    def test_empty_registry_returns_none(self, reid):
        """identify() on an empty registry must return None, not raise."""
        assert reid.identify(E_X) is None

    def test_identical_embedding_returns_visitor_id(self, reid):
        """
        A query identical to the registered embedding has cos_sim = 1.0 — must match.
        """
        reid.register("vis_001", E_X)
        assert reid.identify(E_X) == "vis_001"

    def test_similar_embedding_above_threshold_returns_match(self, reid):
        """cos_sim = 0.9 ≥ threshold=0.85 → match returned."""
        reid.register("vis_001", E_X)
        assert reid.identify(E_SIM9) == "vis_001"

    def test_orthogonal_embedding_returns_none(self, reid):
        """cos_sim = 0.0 < threshold=0.85 → None."""
        reid.register("vis_001", E_X)
        assert reid.identify(E_Y) is None

    def test_at_exact_threshold_returns_match(self, reid):
        """
        cos_sim == threshold (inclusive >=): must return visitor_id.
        A partially occluded staff member who barely matches must still be
        identified to avoid spurious ENTRY events.
        """
        reid.register("vis_001", E_X)
        result = reid.identify(E_AT)
        assert result == "vis_001", (
            f"cos_sim=0.85 at threshold=0.85 must match (inclusive), got {result!r}"
        )

    def test_just_below_threshold_returns_none(self, reid):
        """cos_sim ≈ 0.849 < threshold=0.85 → None."""
        reid.register("vis_001", E_X)
        result = reid.identify(E_BELOW)
        assert result is None, (
            f"cos_sim≈0.849 just below threshold=0.85 must NOT match, got {result!r}"
        )

    def test_best_match_returned_when_multiple_candidates(self, reid):
        """
        Two registered visitors: query closer to vis_001 than vis_002.
        identify() must return the BEST match (highest cosine similarity).
        """
        reid.register("vis_001", E_X)    # query E_SIM9 is similar to this
        reid.register("vis_002", E_Y)    # query E_SIM9 is orthogonal to this

        result = reid.identify(E_SIM9)
        assert result == "vis_001", (
            f"Best match should be vis_001 (sim≈0.9), got {result!r}"
        )

    def test_only_best_match_returned_not_all_above_threshold(self, reid):
        """
        Even when multiple candidates exceed threshold, only ONE visitor_id
        is returned — the closest one.
        """
        e_close  = _unit([0.95, np.sqrt(1 - 0.95**2), 0.0])   # sim=0.95 with E_X
        e_medium = _unit([0.88, np.sqrt(1 - 0.88**2), 0.0])   # sim=0.88 with E_X

        reid.register("vis_close",  e_close)
        reid.register("vis_medium", e_medium)

        result = reid.identify(E_X)
        assert result == "vis_close", (
            "identify() must return the single best match, not a list"
        )


# ---------------------------------------------------------------------------
# register — overwrite behaviour
# ---------------------------------------------------------------------------

class TestRegister:

    def test_register_does_not_raise_on_duplicate_visitor_id(self, reid):
        """Re-registering the same visitor_id must silently overwrite."""
        reid.register("vis_001", E_X)
        reid.register("vis_001", E_Y)   # must not raise

    def test_register_overwrites_previous_embedding(self, reid):
        """
        After overwriting, identify() uses the LATEST embedding.
        Original embedding no longer matches.
        """
        reid.register("vis_001", E_X)   # first registration
        reid.register("vis_001", E_Y)   # overwrite with orthogonal embedding

        assert reid.identify(E_Y) == "vis_001", "Latest embedding must be used"
        assert reid.identify(E_X) is None, (
            "Old embedding must no longer be in registry after overwrite"
        )

    def test_multiple_visitors_registered_independently(self, reid):
        """Each visitor_id stores its own embedding independently."""
        reid.register("vis_A", E_X)
        reid.register("vis_B", E_Y)

        assert reid.identify(E_X) == "vis_A"
        assert reid.identify(E_Y) == "vis_B"


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------

class TestForget:

    def test_forget_removes_visitor_from_registry(self, reid):
        """After forget(), identify() no longer returns that visitor."""
        reid.register("vis_001", E_X)
        reid.forget("vis_001")
        assert reid.identify(E_X) is None

    def test_forget_nonexistent_visitor_does_not_raise(self, reid):
        """forget() on an unknown visitor_id must not crash."""
        reid.forget("vis_ghost")   # must not raise

    def test_forget_only_removes_targeted_visitor(self, reid):
        """Forgetting vis_001 must leave vis_002 intact."""
        reid.register("vis_001", E_X)
        reid.register("vis_002", E_Y)
        reid.forget("vis_001")

        assert reid.identify(E_X) is None      # vis_001 gone
        assert reid.identify(E_Y) == "vis_002" # vis_002 untouched


# ---------------------------------------------------------------------------
# SharedRegistry — cross-process (cross-camera) identity sharing via SQLite
# ---------------------------------------------------------------------------

class TestSharedRegistry:
    """
    SharedRegistry is the cross-camera fix for double-counting: a visitor
    registered by ONE camera process must be looked up by ANOTHER process
    pointed at the same DB. We simulate the two camera processes with two
    independent SharedRegistry instances over one temp SQLite file.
    """

    def test_lookup_across_two_instances_same_db(self, tmp_path):
        db = str(tmp_path / "shared.db")

        # Camera A registers a brand-new visitor.
        cam_a = SharedRegistry("STORE_BLR_002", db_path=db)
        assert cam_a.lookup(E_X, threshold=0.85) is None, "empty registry → no match"
        cam_a.register("vis_001", E_X)

        # Camera B starts fresh against the SAME DB and must SEE vis_001
        # (proves the registration was persisted and is shared cross-process).
        cam_b = SharedRegistry("STORE_BLR_002", db_path=db)
        assert cam_b.lookup(E_X, threshold=0.85) == "vis_001", (
            "Camera B must resolve the same person to the visitor_id Camera A "
            "registered — otherwise the visitor is double-counted across cameras"
        )

    def test_lookup_below_threshold_returns_none_cross_instance(self, tmp_path):
        db = str(tmp_path / "shared.db")
        cam_a = SharedRegistry("STORE_BLR_002", db_path=db)
        cam_a.register("vis_001", E_X)

        cam_b = SharedRegistry("STORE_BLR_002", db_path=db)
        # Orthogonal embedding (sim 0.0) is below threshold → treated as new.
        assert cam_b.lookup(E_Y, threshold=0.85) is None

    def test_store_isolation(self, tmp_path):
        """A registration for one store must not leak into another store."""
        db = str(tmp_path / "shared.db")
        SharedRegistry("STORE_A", db_path=db).register("vis_001", E_X)

        other = SharedRegistry("STORE_B", db_path=db)
        assert other.lookup(E_X, threshold=0.85) is None, (
            "Embeddings must be scoped per store_id"
        )

    def test_table_created_when_absent(self, tmp_path):
        """
        __init__ must create visitor_embeddings if the DB has never been
        touched by the API, so register()/_write_to_db never silently no-ops.
        """
        import sqlite3
        db = str(tmp_path / "fresh.db")
        reg = SharedRegistry("STORE_BLR_002", db_path=db)
        reg.register("vis_001", E_X)

        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT visitor_id FROM visitor_embeddings WHERE store_id = ?",
                ("STORE_BLR_002",),
            ).fetchall()
        finally:
            con.close()
        assert rows == [("vis_001",)], "registration must persist to a created table"
