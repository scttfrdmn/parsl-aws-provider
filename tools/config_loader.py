#!/usr/bin/env python3
"""
Configuration loader for Parsl AWS Provider.

Loads configuration from YAML files to eliminate hardcoded values.
"""

import logging
from pathlib import Path
from typing import Dict, Any, Optional

try:
    import yaml
except ImportError:
    raise ImportError("PyYAML is required. Install with: pip install PyYAML")

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Configuration loading or validation error."""

    pass


class ConfigLoader:
    """Loads and manages configuration for Parsl AWS Provider."""

    def __init__(self, config_path: Optional[str] = None):
        """Initialize configuration loader."""
        self.config_path = config_path or self._find_default_config()
        self.config: Dict[str, Any] = {}
        self._load_config()

    def _find_default_config(self) -> str:
        """Find default configuration file."""
        # Look for config.yaml in same directory as this script
        script_dir = Path(__file__).parent
        default_path = script_dir / "config.yaml"

        if default_path.exists():
            return str(default_path)

        # Look for config in current directory
        current_config = Path("config.yaml")
        if current_config.exists():
            return str(current_config)

        raise ConfigurationError(
            f"No configuration file found. Expected: {default_path} or ./config.yaml"
        )

    def _load_config(self):
        """Load configuration from YAML file."""
        try:
            with open(self.config_path, "r") as f:
                self.config = yaml.safe_load(f)

            logger.info(f"Configuration loaded from: {self.config_path}")

        except FileNotFoundError:
            raise ConfigurationError(
                f"Configuration file not found: {self.config_path}"
            )
        except yaml.YAMLError as e:
            raise ConfigurationError(f"Invalid YAML in configuration file: {e}")
        except Exception as e:
            raise ConfigurationError(f"Error loading configuration: {e}")

    def get(self, key_path: str, default: Any = None) -> Any:
        """Get configuration value using dot notation (e.g., 'aws.regions.default_region')."""
        keys = key_path.split(".")
        value = self.config

        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            if default is not None:
                return default
            raise ConfigurationError(f"Configuration key not found: {key_path}")

    def get_base_ami(self, region: str) -> str:
        """Get base AMI ID for specified region."""
        base_amis = self.get("aws.regions.base_amis")

        if region in base_amis:
            return base_amis[region]

        # Fallback to default region's AMI
        default_region = self.get("aws.regions.default_region")
        if default_region in base_amis:
            logger.warning(
                f"No AMI configured for region {region}, using {default_region} AMI"
            )
            return base_amis[default_region]

        raise ConfigurationError(f"No base AMI configured for region {region}")

    def get_port_range(self, range_type: str) -> tuple:
        """Get port range as tuple."""
        port_range = self.get(f"networking.port_ranges.{range_type}")
        if isinstance(port_range, list) and len(port_range) == 2:
            return tuple(port_range)
        raise ConfigurationError(f"Invalid port range configuration: {range_type}")

    def get_security_group_rules(self) -> list:
        """Get security group ingress rules."""
        return self.get("networking.security_group.ingress_rules", [])

    def get_ami_discovery_config(self) -> Dict[str, Any]:
        """Get AMI discovery configuration."""
        return {
            "enabled": self.get("ami.discovery.enabled", True),
            "max_age_days": self.get("ami.discovery.max_age_days", 30),
            "required_tags": self.get("ami.discovery.required_tags", {}),
            "preferred_features": self.get("ami.discovery.preferred_features", []),
        }

    def get_timeout(self, timeout_type: str) -> int:
        """Get timeout configuration."""
        return self.get(f"provider.timeouts.{timeout_type}")

    def get_worker_config(self) -> Dict[str, Any]:
        """Get worker configuration."""
        return {
            "init_script": self.get("worker.init_script", ""),
            "required_args": self.get("worker.required_args", {}),
            "execution": self.get("worker.execution", {}),
        }

    def get_instance_type(self, type_key: str = "default_type") -> str:
        """Get instance type from configuration."""
        if type_key == "default_type":
            return self.get("instance.default_type")
        else:
            # Look up by key in types mapping
            types = self.get("instance.types", {})
            if type_key in types:
                return types[type_key]
            # If not found, assume it's already an instance type
            return type_key

    def validate_configuration(self):
        """Validate configuration completeness and correctness."""
        required_keys = [
            "aws.regions.base_amis",
            "instance.default_type",
            "networking.port_ranges.tunnel_ports",
            "provider.timeouts.ssm_agent_ready",
        ]

        for key in required_keys:
            try:
                self.get(key)
            except ConfigurationError:
                raise ConfigurationError(f"Required configuration missing: {key}")

        logger.info("Configuration validation passed")


# Global configuration instance
_config_loader: Optional[ConfigLoader] = None


def get_config(config_path: Optional[str] = None) -> ConfigLoader:
    """Get global configuration loader instance."""
    global _config_loader

    if _config_loader is None or config_path is not None:
        _config_loader = ConfigLoader(config_path)

    return _config_loader


def reload_config(config_path: Optional[str] = None):
    """Reload configuration from file."""
    global _config_loader
    _config_loader = ConfigLoader(config_path)
    return _config_loader


if __name__ == "__main__":
    # Test configuration loading
    try:
        config = get_config()
        config.validate_configuration()
        print("✅ Configuration loaded and validated successfully")

        # Print some key configuration values
        print(f"Default region: {config.get('aws.regions.default_region')}")
        print(f"Default instance type: {config.get('instance.default_type')}")
        print(f"Tunnel port range: {config.get_port_range('tunnel_ports')}")
        print(f"SSM agent timeout: {config.get_timeout('ssm_agent_ready')}s")

    except Exception as e:
        print(f"❌ Configuration error: {e}")
        exit(1)
