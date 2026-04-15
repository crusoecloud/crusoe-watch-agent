import os
import tempfile
import yaml
import json
import logging
from datetime import datetime, timezone
from prometheus_client import Counter, start_http_server

class LiteralStr(str): pass


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging - efficient and no parsing needed."""

    def format(self, record):
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace('+00:00', 'Z'),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


def literal_str_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")

# Register representer for both default Dumper and SafeDumper used by yaml.safe_dump
yaml.Dumper.add_representer(LiteralStr, literal_str_representer)
yaml.SafeDumper.add_representer(LiteralStr, literal_str_representer)

class YamlUtils:
    @staticmethod
    def load_yaml_config(path: str) -> dict:
        with open(path) as f:
            cfg = dict(yaml.safe_load(f))
        return cfg

    @staticmethod
    def load_yaml_string(yaml_string: str) -> dict:
        return dict(yaml.safe_load(yaml_string) or {})

    @staticmethod
    def save_yaml(path: str, cfg: dict):
        dir_name = os.path.dirname(path) or "."
        with tempfile.NamedTemporaryFile(mode="w", dir=dir_name, delete=False) as f:
            yaml.safe_dump(cfg, f)
            temp_path = f.name
        os.rename(temp_path, path)


# Prometheus metrics for VCR (common labels added by Vector transform)
errors_total = Counter(
    'vcr_errors_total',
    'Total VCR errors by type',
    ['error_type']
)
