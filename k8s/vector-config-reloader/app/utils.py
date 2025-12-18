import yaml

class LiteralStr(str): pass

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
    def save_yaml(path: str, cfg: dict):
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)
