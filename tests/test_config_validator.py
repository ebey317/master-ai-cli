"""Tests for config_validator.py quoted container detection."""
import pytest
import yaml

from scripts.config_validator import (
    validate_quoted_containers,
    validate_config_structure,
    _container_slots,
    _KNOWN_CONTAINER_TYPES,
    _SCALAR_AS_ONE_ITEM_LIST_KEYS,
)


class TestContainerSlots:
    def test_walks_default_config(self):
        slots = _container_slots()
        # DEFAULT_CONFIG should have at least some list/mapping slots
        assert isinstance(slots, dict)
        # Check that list-type slots are marked "list"
        for key, kind in slots.items():
            assert kind in ("list", "mapping")

    def test_known_container_types_merged(self):
        # Add a test key to _KNOWN_CONTAINER_TYPES temporarily
        _KNOWN_CONTAINER_TYPES["test.quoted_list"] = "list"
        try:
            slots = _container_slots()
            assert "test.quoted_list" in slots
            assert slots["test.quoted_list"] == "list"
        finally:
            del _KNOWN_CONTAINER_TYPES["test.quoted_list"]


class TestQuotedContainerValidation:
    def test_detects_quoted_list_in_mapping_slot(self):
        config = {
            "plugins": {
                "enabled": '["plugin_a", "plugin_b"]'  # quoted string, should be list
            }
        }
        warnings = validate_quoted_containers(config)
        assert len(warnings) == 1
        assert warnings[0]["key"] == "plugins.enabled"
        assert warnings[0]["kind"] == "list"
        assert warnings[0]["quoted_value"] == '["plugin_a", "plugin_b"]'
        assert "sensei config set plugins.enabled" in warnings[0]["remedy"]

    def test_detects_quoted_mapping_in_mapping_slot(self):
        config = {
            "model_catalog": {
                "excluded_providers": '{"openai-api": true}'  # quoted string, should be mapping
            }
        }
        # Add to known types for test
        _KNOWN_CONTAINER_TYPES["model_catalog.excluded_providers"] = "mapping"
        try:
            warnings = validate_quoted_containers(config)
            assert len(warnings) == 1
            assert warnings[0]["key"] == "model_catalog.excluded_providers"
            assert warnings[0]["kind"] == "mapping"
        finally:
            del _KNOWN_CONTAINER_TYPES["model_catalog.excluded_providers"]

    def test_ignores_actual_list_values(self):
        config = {
            "plugins": {
                "enabled": ["plugin_a", "plugin_b"]  # real list, OK
            }
        }
        warnings = validate_quoted_containers(config)
        assert len(warnings) == 0

    def test_ignores_actual_mapping_values(self):
        config = {
            "model_catalog": {
                "excluded_providers": {"openai-api": True}  # real mapping, OK
            }
        }
        _KNOWN_CONTAINER_TYPES["model_catalog.excluded_providers"] = "mapping"
        try:
            warnings = validate_quoted_containers(config)
            assert len(warnings) == 0
        finally:
            del _KNOWN_CONTAINER_TYPES["model_catalog.excluded_providers"]

    def test_ignores_scalar_string_values(self):
        config = {
            "some": {
                "scalar_key": "just a string"  # not a container literal
            }
        }
        warnings = validate_quoted_containers(config)
        assert len(warnings) == 0

    def test_ignores_scalar_as_one_item_list_keys(self):
        # These keys accept bare scalars via parse_config_string_list
        key = "agent.disabled_toolsets"
        _SCALAR_AS_ONE_ITEM_LIST_KEYS.add(key)  # type: ignore[attr-defined]
        try:
            config = {key: '["tool_a"]'}  # quoted but allowed
            warnings = validate_quoted_containers(config)
            assert len(warnings) == 0
        finally:
            _SCALAR_AS_ONE_ITEM_LIST_KEYS.remove(key)  # type: ignore[attr-defined]

    def test_handles_missing_keys_gracefully(self):
        config = {}
        warnings = validate_quoted_containers(config)
        assert len(warnings) == 0

    def test_handles_yaml_parse_errors_gracefully(self):
        config = {
            "plugins": {
                "enabled": '[not valid yaml'  # malformed
            }
        }
        warnings = validate_quoted_containers(config)
        assert len(warnings) == 0


class TestValidateConfigStructure:
    def test_includes_quoted_container_warnings(self):
        config = {
            "plugins": {
                "enabled": '["a", "b"]'
            }
        }
        issues = validate_config_structure(config)
        assert any(i["key"] == "plugins.enabled" for i in issues)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
