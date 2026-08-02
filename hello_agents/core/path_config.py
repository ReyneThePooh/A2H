"""Shared filesystem paths loaded from .env."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]


def env_path(name: str, default: Optional[str] = None, required: bool = False) -> Path:
    value = os.getenv(name, default)
    if value is None or str(value).strip() == "":
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        value = "."

    path = Path(str(value).strip().strip("\"'")).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


ANDROID_PROJECT_DIR = env_path(
    "ANDROID_PROJECT_DIR",
    os.getenv("ANDROID_PROJECT", "."),
)
HARMONY_TEMPLATE_DIR = env_path("HARMONY_TEMPLATE_DIR", "HarmonyTemplate")
HARMONY_SOURCE_PROJECT_DIR = env_path("HARMONY_SOURCE_PROJECT_DIR", "HarmonyProject")
HARMONY_WORK_BASE_DIR = env_path("HARMONY_WORK_BASE_DIR", "HarmonyCheckWorkDir")
