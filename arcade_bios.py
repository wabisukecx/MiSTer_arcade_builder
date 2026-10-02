"""Read BIOS set names from a MAME DAT or -listxml file."""
from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from arcade_common import check_cancel


def bios_zip_names(path: Path, cancel_requested=None) -> frozenset[str]:
    """Return ZIP names for machines explicitly marked isbios=yes."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"MAME DAT file not found: {path}")
    names = set()
    machines = 0
    root_tag = None
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            if event == "start" and root_tag is None:
                root_tag = element.tag
            if event == "end" and element.tag in ("machine", "game"):
                check_cancel(cancel_requested)
                machines += 1
                name = element.get("name", "").strip().lower()
                if element.get("isbios", "").lower() == "yes" and name and "/" not in name and "\\" not in name:
                    names.add(name + ".zip")
                element.clear()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"Cannot read MAME DAT: {path}: {exc}") from exc
    if machines == 0 or root_tag not in ("datafile", "mame"):
        raise ValueError(f"Not a MAME DAT or -listxml file: {path}")
    return frozenset(names)
