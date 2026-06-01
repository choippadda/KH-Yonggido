from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class RuleEngineError(ValueError):
    pass


ALLOWED_GEOMETRY_FAMILIES = {"line", "point", "polygon"}


@dataclass(frozen=True)
class OccupancyRule:
    rule_id: str
    label: str
    geometry_family: str
    permanent_model: dict[str, Any]
    temporary_model: dict[str, Any]
    metadata: dict[str, Any]


class OccupancyRuleEngine:
    def __init__(self, rules: dict[str, OccupancyRule], default_rule_id: str | None = None) -> None:
        self._rules = dict(rules)
        self._default_rule_id = default_rule_id

    @property
    def default_rule_id(self) -> str | None:
        return self._default_rule_id

    def list_rule_ids(self) -> list[str]:
        return list(self._rules.keys())

    def get_rule(self, rule_id: str | None = None) -> OccupancyRule:
        resolved_rule_id = rule_id or self._default_rule_id
        if not resolved_rule_id:
            raise RuleEngineError("기본 객체 유형 규칙이 설정되지 않았습니다.")
        if resolved_rule_id not in self._rules:
            raise RuleEngineError(f"객체 유형 규칙을 찾을 수 없습니다: {resolved_rule_id}")
        return self._rules[resolved_rule_id]

    def require_geometry_family(self, rule_id: str, geometry_family: str) -> OccupancyRule:
        rule = self.get_rule(rule_id)
        if rule.geometry_family != geometry_family:
            raise RuleEngineError(
                f"객체 유형 '{rule_id}'는 {rule.geometry_family} geometry용으로 정의되어 있어 "
                f"{geometry_family} geometry에 적용할 수 없습니다."
            )
        return rule

    def summarize(self) -> str:
        labels = [f"{rule.rule_id}({rule.geometry_family})" for rule in self._rules.values()]
        default_text = self._default_rule_id or "없음"
        return f"기본 객체={default_text}, 규칙={', '.join(labels)}"


def _ensure_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuleEngineError(f"{field_name} 설정은 객체(dict)여야 합니다.")
    return value


def build_rule_engine(config: dict[str, Any], logger=None) -> OccupancyRuleEngine:
    engine_config = _ensure_mapping(config.get("common_occupancy_engine", {}), "common_occupancy_engine")
    default_rule_id = engine_config.get("default_feature_type")
    raw_rules = _ensure_mapping(engine_config.get("feature_rules", {}), "common_occupancy_engine.feature_rules")
    if not raw_rules:
        raise RuleEngineError("공통 점용 엔진 규칙이 비어 있습니다.")

    rules: dict[str, OccupancyRule] = {}
    for rule_id, raw_rule in raw_rules.items():
        rule_config = _ensure_mapping(raw_rule, f"common_occupancy_engine.feature_rules.{rule_id}")
        geometry_family = str(rule_config.get("geometry_family", "")).strip().lower()
        if geometry_family not in ALLOWED_GEOMETRY_FAMILIES:
            raise RuleEngineError(
                f"객체 유형 '{rule_id}'의 geometry_family는 "
                f"{sorted(ALLOWED_GEOMETRY_FAMILIES)} 중 하나여야 합니다."
            )

        permanent_model = _ensure_mapping(
            rule_config.get("permanent_model", {}),
            f"common_occupancy_engine.feature_rules.{rule_id}.permanent_model",
        )
        temporary_model = _ensure_mapping(
            rule_config.get("temporary_model", {}),
            f"common_occupancy_engine.feature_rules.{rule_id}.temporary_model",
        )

        rules[rule_id] = OccupancyRule(
            rule_id=rule_id,
            label=str(rule_config.get("label", rule_id)),
            geometry_family=geometry_family,
            permanent_model=permanent_model,
            temporary_model=temporary_model,
            metadata=_ensure_mapping(rule_config.get("metadata", {}), f"common_occupancy_engine.feature_rules.{rule_id}.metadata"),
        )

    engine = OccupancyRuleEngine(rules, default_rule_id=default_rule_id)
    if logger is not None:
        logger.info("가시설/구조물 공통 엔진 규칙을 확인했습니다. %s", engine.summarize())
    return engine
