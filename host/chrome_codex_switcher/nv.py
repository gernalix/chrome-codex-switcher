"""Read the project release number from the installed/tracked root only."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def release_label():
    try:
        raw = (ROOT / 'NV.txt').read_bytes()
        if not re.fullmatch(rb'[1-9][0-9]*\n', raw) or (ROOT / 'version.txt').exists():
            raise ValueError('inconsistent source')
        return 'NV ' + raw[:-1].decode('ascii')
    except (OSError, ValueError):
        return 'NV unavailable'
