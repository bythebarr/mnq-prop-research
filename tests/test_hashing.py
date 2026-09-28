"""Reproducibility: hashes are deterministic and synthetic data is repeatable."""

from __future__ import annotations

from mnq_research.config import parse_yaml
from mnq_research.hashing import build_manifest, hash_config_file, hash_dataframe, hash_object
from mnq_research.synthetic_data import SyntheticSpec, generate_synthetic_bars

YAML_A = """
# a comment
stop:
  method: TBD
  buffer_ticks: 2
name: "example"
"""

YAML_B = """
name: example
stop: {buffer_ticks: 2,   method: TBD}   # different order, style and comments
"""


def test_config_hash_ignores_formatting_and_key_order(tmp_path):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text(YAML_A)
    b.write_text(YAML_B)
    assert hash_config_file(a) == hash_config_file(b)
    assert hash_config_file(a) == hash_config_file(a)  # repeatable


def test_config_hash_changes_when_any_value_changes():
    base = hash_object(parse_yaml(YAML_A))
    assert hash_object(parse_yaml(YAML_A.replace("buffer_ticks: 2", "buffer_ticks: 3"))) != base
    # Type matters: integer 2 and decimal 2.0 are different specifications.
    assert hash_object(parse_yaml(YAML_A.replace("buffer_ticks: 2", "buffer_ticks: 2.0"))) != base


def test_manifest_hash_is_order_independent_and_content_sensitive(tmp_path):
    (tmp_path / "x.csv").write_text("1\n")
    (tmp_path / "y.csv").write_text("2\n")
    files = [tmp_path / "x.csv", tmp_path / "y.csv"]
    first = build_manifest(files, root=tmp_path)["manifest_sha256"]
    assert build_manifest(list(reversed(files)), root=tmp_path)["manifest_sha256"] == first
    (tmp_path / "y.csv").write_text("3\n")
    assert build_manifest(files, root=tmp_path)["manifest_sha256"] != first


def test_synthetic_generation_is_reproducible():
    spec = SyntheticSpec(n_trading_days=2, seed=7)
    assert hash_dataframe(generate_synthetic_bars(spec)) == hash_dataframe(generate_synthetic_bars(spec))
    other_seed = SyntheticSpec(n_trading_days=2, seed=8)
    assert hash_dataframe(generate_synthetic_bars(other_seed)) != hash_dataframe(generate_synthetic_bars(spec))
