from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from tunelab.ccm.imatest import parse_imatest_csv
from tunelab.ccm.models import ImatestDataset


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"
CC_XML = SOURCES / "cc13_ipe_v2.xml"
D65_IMAGE = SOURCES / "D65_normal.jpg"
D65_CSV = SOURCES / "D65_normal_summary.csv"


@lru_cache(maxsize=1)
def d65_dataset() -> ImatestDataset:
    """Load the shared CCM test dataset from the current plural sources folder."""

    return parse_imatest_csv(D65_CSV)
