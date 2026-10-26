"""A whole risk policy as a file: the YAML (or JSON) text an operator reads and edits
(`policy_yaml`) and parses back (`policy_text`). The admin command line dumps the policy in force
with it (`show-policy`) and reads a replacement before trading starts (`set-policy`); a research
run reads a candidate policy with it (`backtest --policy`). Pure: no file or database access."""

from __future__ import annotations

import yaml

from .policy import PolicyError, RiskPolicy, parse_policy

__all__ = ["policy_text", "policy_yaml"]


def _exact_floats(node: object) -> object:
    """The document with each float as its shortest exact text (`repr`, which reads back to the
    same decimal as written): the policy takes numbers as strings and refuses floats."""
    if isinstance(node, float):
        return repr(node)
    if isinstance(node, dict):
        return {k: _exact_floats(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_exact_floats(v) for v in node]
    return node


def policy_text(text: str) -> RiskPolicy:
    """The policy in a YAML or JSON document, an unquoted decimal read as written;
    `PolicyError` for text that does not parse or a document `parse_policy` refuses."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        raise PolicyError("policy file: not YAML or JSON") from None
    return parse_policy(_exact_floats(doc))


def policy_yaml(policy: RiskPolicy, heading: str = "") -> str:
    """The policy's document (`to_json`) as YAML, in its stored key order, after `heading` as
    a comment line when given; `policy_text` reads it back to the same policy."""
    comment = f"# {heading}\n" if heading else ""
    return comment + yaml.safe_dump(policy.to_json(), sort_keys=False)
