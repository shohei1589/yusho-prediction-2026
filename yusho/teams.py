from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Team:
    code: str
    short_name: str
    full_name: str
    league: str


CENTRAL = "central"
PACIFIC = "pacific"
FARM_EAST = "farm_east"
FARM_CENTRAL = "farm_central"
FARM_WEST = "farm_west"


TEAMS: dict[str, Team] = {
    "G": Team("G", "巨人", "読売ジャイアンツ", CENTRAL),
    "T": Team("T", "阪神", "阪神タイガース", CENTRAL),
    "DB": Team("DB", "DeNA", "横浜DeNAベイスターズ", CENTRAL),
    "C": Team("C", "広島", "広島東洋カープ", CENTRAL),
    "D": Team("D", "中日", "中日ドラゴンズ", CENTRAL),
    "S": Team("S", "ヤクルト", "東京ヤクルトスワローズ", CENTRAL),
    "H": Team("H", "ソフトバンク", "福岡ソフトバンクホークス", PACIFIC),
    "F": Team("F", "日本ハム", "北海道日本ハムファイターズ", PACIFIC),
    "M": Team("M", "ロッテ", "千葉ロッテマリーンズ", PACIFIC),
    "Bs": Team("Bs", "オリックス", "オリックス・バファローズ", PACIFIC),
    "E": Team("E", "楽天", "東北楽天ゴールデンイーグルス", PACIFIC),
    "L": Team("L", "西武", "埼玉西武ライオンズ", PACIFIC),
    "OIX": Team("OIX", "オイシックス", "オイシックス新潟アルビレックスBC", FARM_EAST),
    "HYT": Team("HYT", "ハヤテ", "ハヤテベンチャーズ静岡", FARM_CENTRAL),
}


LEAGUES: dict[str, tuple[str, ...]] = {
    CENTRAL: ("G", "T", "DB", "C", "D", "S"),
    PACIFIC: ("H", "F", "M", "Bs", "E", "L"),
    FARM_WEST: ("H", "Bs", "C", "T"),
    FARM_CENTRAL: ("G", "DB", "L", "D", "HYT"),
    FARM_EAST: ("F", "E", "M", "S", "OIX"),
}


NPB_NAME_TO_CODE: dict[str, str] = {
    team.short_name: code for code, team in TEAMS.items()
} | {
    team.full_name: code for code, team in TEAMS.items()
}
NPB_NAME_TO_CODE.update(
    {
        "巨人": "G",
        "阪神": "T",
        "DeNA": "DB",
        "広島": "C",
        "中日": "D",
        "ヤクルト": "S",
        "ソフトバンク": "H",
        "日本ハム": "F",
        "ロッテ": "M",
        "オリックス": "Bs",
        "楽天": "E",
        "西武": "L",
        "オイシックス新潟": "OIX",
        "ハヤテ静岡": "HYT",
    }
)


def league_teams(league: str) -> tuple[str, ...]:
    try:
        return LEAGUES[league]
    except KeyError as exc:
        raise ValueError(f"Unknown league: {league}") from exc


def is_farm_league(league: str) -> bool:
    return league in {FARM_EAST, FARM_CENTRAL, FARM_WEST}


def team_label(code: str) -> str:
    return TEAMS[code].short_name
