"""coverage/settings.json(탭·담당 목록·이름 변경) + 커버리지 엑셀 '산업구분' 시트의 팀 구분"""
import json
from pathlib import Path

import pandas as pd

from . import config, db

DEFAULT = {"tabs": [], "assignees_extra": [], "aliases": {}, "team_sheet": "산업구분", "my_team": "리서치1팀"}


def settings() -> dict:
    p = Path(config.COVERAGE_FILE).parent / "settings.json"
    try:
        return {**DEFAULT, **json.loads(p.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULT)


def alias(name: str) -> str:
    return settings()["aliases"].get(name, name)


def load_teams(path=None) -> dict:
    """{종목코드: 팀명} — '산업구분' 시트의 Code / 팀 구분 열"""
    st = settings()
    try:
        df = pd.read_excel(path or config.COVERAGE_FILE, sheet_name=st["team_sheet"], header=None, dtype=str)
    except Exception:  # noqa: BLE001
        return {}
    hdr = None
    for r in range(min(60, len(df))):
        row = [str(x).strip() for x in df.iloc[r].tolist()]
        if "Code" in row and any("팀" in x for x in row):
            hdr = r
            ci, ti = row.index("Code"), next(i for i, x in enumerate(row) if "팀" in x and "구분" in x)
            break
    if hdr is None:
        return {}
    out = {}
    for _, row in df.iloc[hdr + 1:].iterrows():
        code, team = str(row.iloc[ci]).strip(), str(row.iloc[ti]).strip()
        if code.startswith("A") and len(code) == 7:
            out[code[1:]] = team
    return out


def save_teams_to_db():
    db.set_meta("teams", json.dumps(load_teams(), ensure_ascii=False))


def teams() -> dict:
    try:
        return json.loads(db.get_meta("teams") or "{}")
    except ValueError:
        return {}


def visible_unassigned(code, tmap=None) -> bool:
    """담당 없는 종목을 '커버리지 외'로 보여줄지: 우리 팀 종목이거나, 팀 목록에 아직 없는 종목(신규상장 등)"""
    tmap = teams() if tmap is None else tmap
    t = tmap.get(code or "")
    return t is None or t == settings()["my_team"]


def assignees() -> list[str]:
    st = settings()
    names = list(st["tabs"])
    for n in st["assignees_extra"] + db.analyst_names():
        if n not in names:
            names.append(n)
    return names
