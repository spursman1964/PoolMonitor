import yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"

with open(CONFIG_PATH, "r") as file:
    settings = yaml.safe_load(file)

print(settings)
print("Logging interval:", settings["logging"]["interval_seconds"])
print("Target pH:", settings["targets"]["ph_target"])
