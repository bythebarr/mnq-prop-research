"""Loading YAML configuration files safely and strictly.

Why a custom YAML loader?
-------------------------
PyYAML's default behaviour has two traps that are dangerous for a trading
rule specification:

1. **Duplicate keys are silently accepted** - the last one wins. A rule file
   containing ``stop_ticks: 8`` and, further down, ``stop_ticks: 12`` would
   quietly use 12. We reject duplicate keys outright.
2. **YAML 1.1 "helpful" conversions.** Unquoted ``17:00`` becomes the integer
   1020 (base-60 arithmetic), and ``yes``/``no``/``on``/``off`` become
   booleans. Session times are central to this project, so we use YAML 1.2
   style rules instead: only ``true``/``false`` are booleans, and ``17:00``
   stays the string ``"17:00"``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RULE_FREEZE_PATH = Path("configs/rule_freeze_v1.yaml")
DEFAULT_EXPERIMENT_PATH = Path("configs/experiment_001.yaml")
DEFAULT_REGISTRY_PATH = Path("outputs/experiments/registry.jsonl")
DEFAULT_SYNTHETIC_DIR = Path("data/interim/synthetic")


class ConfigError(Exception):
    """A configuration file is missing, unreadable or malformed."""


class StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader with duplicate-key rejection and YAML 1.2-style scalars."""


def _construct_mapping_no_duplicates(loader: StrictSafeLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    seen: dict[Any, yaml.Node] = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ConfigError(
                f"Duplicate key {key!r} at line {key_node.start_mark.line + 1} "
                f"(first defined at line {seen[key].start_mark.line + 1}). "
                "Each setting may appear only once."
            )
        seen[key] = key_node
    return loader.construct_mapping(node, deep=deep)


# Replace the YAML 1.1 implicit resolvers for bool/int/float with YAML 1.2
# core-schema equivalents (no base-60 numbers, no yes/no/on/off booleans).
_REPLACED_TAGS = {
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
}
StrictSafeLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag not in _REPLACED_TAGS]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)
StrictSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    list("-+0123456789"),
)
StrictSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
    ),
    list("-+.0123456789"),
)
StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_duplicates
)


def parse_yaml(text: str, source: str = "<string>") -> Any:
    """Parse YAML text with the strict loader, raising ConfigError on problems."""
    try:
        return yaml.load(text, Loader=StrictSafeLoader)  # noqa: S506 - StrictSafeLoader subclasses SafeLoader
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: not valid YAML.\n{exc}") from None


def load_yaml(path: str | Path) -> Any:
    """Read and parse a YAML file with friendly errors."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"File not found: {path}")
    return parse_yaml(path.read_text(encoding="utf-8"), source=str(path))


def load_mapping(path: str | Path) -> dict[str, Any]:
    """Load a YAML file whose top level must be a mapping (key: value pairs)."""
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected key/value pairs at the top level, got {type(data).__name__}.")
    return data


def find_project_root(start: str | Path | None = None) -> Path:
    """Walk upwards from ``start`` (default: current directory) to find pyproject.toml."""
    current = Path(start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError(
        "Could not find the project folder (no pyproject.toml above "
        f"{current}). Run commands from inside the mnq-prop-research folder."
    )
