from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from itertools import combinations
from html import unescape
from html.parser import HTMLParser
import os
import re
from typing import Iterable

import pandas as pd
import requests
import urllib3

from .teams import CENTRAL, PACIFIC, TEAMS, NPB_NAME_TO_CODE, league_teams, team_label


BASE_URL = "https://npb.jp"
SCHEDULE_MONTHS = tuple(range(3, 12))
REGULAR_SEASON_GAMES = 143
INTRA_LEAGUE_PAIR_GAMES = 25
INTERLEAGUE_PAIR_GAMES = 3
MAKEUP_UNKNOWN_OPPONENT = "TBD"
MAKEUP_UNKNOWN_OPPONENT_NAME = "未定"
SCHEDULE_COLUMNS = [
    "Date",
    "HomeTeam",
    "AwayTeam",
    "HomeTeamName",
    "AwayTeamName",
    "Score1",
    "Score2",
    "State",
    "Status",
    "Venue",
    "StartTime",
    "IsMakeup",
    "DateLabel",
]
REQUEST_TIMEOUT = (4, 8)


@dataclass(frozen=True)
class FetchResult:
    frame: pd.DataFrame
    source_urls: tuple[str, ...]


class _TableRowParser(HTMLParser):
    """Small table parser for NPB pages.

    It keeps td/th text and selected class/id attributes, which is enough for
    the standings and schedule tables without adding BeautifulSoup/lxml.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, object]] = []
        self._row: dict[str, object] | None = None
        self._cell: dict[str, object] | None = None
        self._div_class_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "tr":
            self._row = {
                "id": attrs_dict.get("id", ""),
                "class": attrs_dict.get("class", ""),
                "cells": [],
            }
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {
                "tag": tag,
                "class": attrs_dict.get("class", ""),
                "text": [],
                "parts": {},
            }
        elif tag == "div" and self._cell is not None:
            self._div_class_stack.append(attrs_dict.get("class", ""))
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._div_class_stack:
            self._div_class_stack.pop()
        elif tag in {"td", "th"} and self._row is not None and self._cell is not None:
            text = _clean_text("".join(self._cell["text"]))
            self._cell["text"] = text
            self._row["cells"].append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None
            self._div_class_stack.clear()

    def handle_data(self, data: str) -> None:
        if self._cell is None:
            return
        text = unescape(data)
        self._cell["text"].append(text)
        if self._div_class_stack:
            class_name = self._div_class_stack[-1]
            if class_name:
                parts = self._cell["parts"]
                parts.setdefault(class_name, [])
                parts[class_name].append(text)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _fetch_html(url: str) -> str:
    verify_ssl = os.getenv("NPB_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
    use_env_proxy = os.getenv("NPB_USE_ENV_PROXY", "false").lower() in {"1", "true", "yes"}
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.trust_env = use_env_proxy
    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "yusho-prediction/0.1 (+non-commercial research)"},
        verify=verify_ssl,
    )
    response.raise_for_status()
    return response.content.decode("utf-8", errors="replace")


def _parse_rows(html: str) -> list[dict[str, object]]:
    parser = _TableRowParser()
    parser.feed(html)
    return parser.rows


def _parts_text(cell: dict[str, object], class_name: str) -> str:
    parts = cell.get("parts", {})
    if not isinstance(parts, dict):
        return ""
    values = parts.get(class_name, [])
    if not isinstance(values, list):
        return ""
    return _clean_text("".join(str(value) for value in values))


def fetch_standings(year: int, league: str) -> FetchResult:
    suffix = {CENTRAL: "c", PACIFIC: "p"}[league]
    url = f"{BASE_URL}/bis/{year}/stats/std_{suffix}.html"
    html = _fetch_html(url)
    rows = _parse_rows(html)
    data: list[dict[str, object]] = []
    expected_codes = set(league_teams(league))

    for row in rows:
        if "ststats" not in str(row.get("class", "")).split():
            continue
        cells = row.get("cells", [])
        if not isinstance(cells, list) or len(cells) < 6:
            continue
        team_name = str(cells[0]["text"])
        code = NPB_NAME_TO_CODE.get(team_name)
        if code not in expected_codes:
            continue
        data.append(
            {
                "Team": code,
                "TeamName": team_name,
                "Games": int(str(cells[1]["text"])),
                "Wins": int(str(cells[2]["text"])),
                "Losses": int(str(cells[3]["text"])),
                "Ties": int(str(cells[4]["text"])),
                "WinRate": float(str(cells[5]["text"])),
            }
        )
        if len(data) == len(expected_codes):
            break

    if len(data) != len(expected_codes):
        raise ValueError(f"Could not parse {league} standings from {url}")

    return FetchResult(pd.DataFrame(data), (url,))


def fetch_schedule(year: int, start_date: date | None = None) -> FetchResult:
    month_results: list[tuple[int, str, pd.DataFrame]] = []
    months = _schedule_months_for(year, start_date)
    if months:
        worker_count = min(3, len(months))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(_fetch_schedule_month, year, month): month
                for month in months
            }
            for future in as_completed(futures):
                try:
                    month_results.append(future.result())
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 404:
                        continue
                    raise

    month_results.sort(key=lambda item: item[0])
    urls = [url for _, url, _ in month_results]
    frames = [frame for _, _, frame in month_results]
    schedule = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=SCHEDULE_COLUMNS)
    )
    if start_date is not None and not schedule.empty:
        schedule = schedule[schedule["Date"] >= pd.Timestamp(start_date)]
    if not schedule.empty:
        schedule = schedule.sort_values(["Date", "HomeTeam", "AwayTeam"])
    schedule = schedule.reset_index(drop=True)
    return FetchResult(schedule, tuple(urls))


def _schedule_months_for(year: int, start_date: date | None) -> tuple[int, ...]:
    if start_date is None or start_date.year < year:
        return SCHEDULE_MONTHS
    if start_date.year > year:
        return ()
    start_month = max(SCHEDULE_MONTHS[0], min(start_date.month, SCHEDULE_MONTHS[-1]))
    return tuple(month for month in SCHEDULE_MONTHS if month >= start_month)


def _fetch_schedule_month(year: int, month: int) -> tuple[int, str, pd.DataFrame]:
    url = f"{BASE_URL}/games/{year}/schedule_{month:02d}_detail.html"
    html = _fetch_html(url)
    return month, url, _parse_schedule_month(html, year)


def fetch_remaining_schedule(
    year: int,
    league: str,
    start_date: date | None = None,
) -> FetchResult:
    result = fetch_schedule(year, start_date)
    league_codes = set(league_teams(league))
    frame = result.frame
    frame = frame[
        (frame["Status"].isin(["scheduled", "in_progress"]))
        & (frame["HomeTeam"].isin(league_codes) | frame["AwayTeam"].isin(league_codes))
    ].copy()
    return FetchResult(frame.reset_index(drop=True), result.source_urls)


def append_makeup_placeholders(
    schedule: pd.DataFrame,
    standings: pd.DataFrame,
    league: str,
    total_games: int = REGULAR_SEASON_GAMES,
    full_schedule: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Add synthetic makeup games when official remaining fixtures are short."""
    teams = league_teams(league)
    frame = schedule.copy()
    for column in SCHEDULE_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame["IsMakeup"] = frame["IsMakeup"].fillna(False).astype(bool)
    frame["DateLabel"] = frame["DateLabel"].fillna("").astype(str)

    played_by_team = _played_games_by_team(standings)
    deficits: dict[str, int] = {}
    for team in teams:
        played = played_by_team.get(team, 0)
        remaining = int(((frame["HomeTeam"] == team) | (frame["AwayTeam"] == team)).sum())
        deficits[team] = max(0, int(total_games) - played - remaining)

    if not any(deficits.values()):
        return _sort_schedule(frame)

    last_date = pd.to_datetime(frame["Date"], errors="coerce").dropna().max()
    if pd.isna(last_date):
        last_date = pd.Timestamp.today().normalize()

    placeholder_pairs, deficits = _infer_makeup_pairs_from_rules(
        deficits,
        league,
        full_schedule,
    )
    while True:
        active = [
            team
            for team, count in sorted(deficits.items(), key=lambda item: (-item[1], item[0]))
            if count > 0
        ]
        if len(active) < 2:
            break
        home, away = active[0], active[1]
        placeholder_pairs.append((home, away))
        deficits[home] -= 1
        deficits[away] -= 1

    for team, count in deficits.items():
        for _ in range(count):
            placeholder_pairs.append((team, MAKEUP_UNKNOWN_OPPONENT))

    placeholder_count = len(placeholder_pairs)
    placeholder_label = f"振替日（残{placeholder_count}日）"
    placeholder_rows = [
        _makeup_placeholder_row(
            last_date + pd.Timedelta(days=index),
            home,
            away,
            placeholder_label,
        )
        for index, (home, away) in enumerate(placeholder_pairs, start=1)
    ]
    if not placeholder_rows:
        return _sort_schedule(frame)

    completed = pd.concat(
        [frame, pd.DataFrame(placeholder_rows, columns=SCHEDULE_COLUMNS)],
        ignore_index=True,
    )
    return _sort_schedule(completed)


def _infer_makeup_pairs_from_rules(
    deficits: dict[str, int],
    league: str,
    full_schedule: pd.DataFrame | None,
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    if full_schedule is None or full_schedule.empty:
        return [], deficits

    league_set = set(league_teams(league))
    remaining_deficits = deficits.copy()
    inferred_pairs: list[tuple[str, str]] = []

    for home, away in _missing_pairs_from_schedule(full_schedule, league):
        home_in_league = home in league_set
        away_in_league = away in league_set
        if home_in_league and remaining_deficits.get(home, 0) <= 0:
            continue
        if away_in_league and remaining_deficits.get(away, 0) <= 0:
            continue

        inferred_pairs.append((home, away))
        if home_in_league:
            remaining_deficits[home] -= 1
        if away_in_league:
            remaining_deficits[away] -= 1

    return inferred_pairs, remaining_deficits


def _missing_pairs_from_schedule(
    full_schedule: pd.DataFrame,
    league: str,
) -> list[tuple[str, str]]:
    teams = league_teams(league)
    league_set = set(teams)
    expected_pairs = _expected_pair_counts(league)
    played_or_scheduled_counts: dict[tuple[str, str], int] = defaultdict(int)
    canceled_pairs: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)

    if full_schedule.empty:
        return []

    for row in full_schedule.itertuples(index=False):
        home = str(getattr(row, "HomeTeam", ""))
        away = str(getattr(row, "AwayTeam", ""))
        if home not in TEAMS or away not in TEAMS:
            continue
        if home not in league_set and away not in league_set:
            continue

        key = _pair_key(home, away)
        status = str(getattr(row, "Status", ""))
        if status in {"final", "scheduled", "in_progress"}:
            played_or_scheduled_counts[key] += 1
        elif status == "canceled":
            canceled_pairs[key].append((home, away))

    missing_pairs: list[tuple[str, str]] = []
    for pair, expected_count in expected_pairs.items():
        missing_count = expected_count - played_or_scheduled_counts.get(pair, 0)
        if missing_count <= 0:
            continue
        canceled_candidates = canceled_pairs.get(pair, [])
        for index in range(missing_count):
            if index < len(canceled_candidates):
                missing_pairs.append(canceled_candidates[index])
            else:
                missing_pairs.append(_display_pair_order(pair[0], pair[1], league))

    return missing_pairs


def _expected_pair_counts(league: str) -> dict[tuple[str, str], int]:
    teams = league_teams(league)
    opposite = CENTRAL if league == PACIFIC else PACIFIC
    expected: dict[tuple[str, str], int] = {}
    for home, away in combinations(teams, 2):
        expected[_pair_key(home, away)] = INTRA_LEAGUE_PAIR_GAMES
    for team in teams:
        for opponent in league_teams(opposite):
            expected[_pair_key(team, opponent)] = INTERLEAGUE_PAIR_GAMES
    return expected


def _pair_key(home: str, away: str) -> tuple[str, str]:
    return tuple(sorted((home, away)))


def _display_pair_order(home: str, away: str, league: str) -> tuple[str, str]:
    league_order = {team: index for index, team in enumerate(league_teams(league))}
    if home in league_order and away not in league_order:
        return home, away
    if away in league_order and home not in league_order:
        return away, home
    return tuple(sorted((home, away), key=lambda team: league_order.get(team, 99)))


def schedule_to_daily_opponents(
    schedule: pd.DataFrame,
    league: str,
    target_team: str | None = None,
) -> pd.DataFrame:
    teams = league_teams(league)
    makeup_counts = (
        _target_makeup_counts(schedule, target_team)
        if target_team is not None
        else {"target": 0, "other": 0}
    )
    rows: list[dict[str, object]] = []
    for game_date, group in schedule.groupby("Date", sort=True):
        row: dict[str, object] = {
            "Date": game_date,
            "DateLabel": _group_date_label(group, target_team, makeup_counts),
        }
        for team in teams:
            row[f"{team}_Opponent"] = pd.NA
        for game in group.itertuples(index=False):
            home = str(game.HomeTeam)
            away = str(game.AwayTeam)
            if home in teams and away in teams:
                row[f"{home}_Opponent"] = away
                row[f"{away}_Opponent"] = home
            elif home in teams:
                row[f"{home}_Opponent"] = away
            elif away in teams:
                row[f"{away}_Opponent"] = home
        rows.append(row)
    return pd.DataFrame(rows)


def _target_makeup_counts(schedule: pd.DataFrame, target_team: str | None) -> dict[str, int]:
    if target_team is None or schedule.empty or "IsMakeup" not in schedule.columns:
        return {"target": 0, "other": 0}
    makeup = schedule[schedule["IsMakeup"].fillna(False).astype(bool)]
    if makeup.empty:
        return {"target": 0, "other": 0}
    target_mask = (makeup["HomeTeam"] == target_team) | (makeup["AwayTeam"] == target_team)
    target_count = int(target_mask.sum())
    return {"target": target_count, "other": int(len(makeup) - target_count)}


def _played_games_by_team(standings: pd.DataFrame) -> dict[str, int]:
    played: dict[str, int] = {}
    for row in standings.itertuples(index=False):
        team = str(row.Team)
        if hasattr(row, "Games"):
            played[team] = int(row.Games)
        else:
            played[team] = int(row.Wins) + int(row.Losses) + int(row.Ties)
    return played


def _makeup_placeholder_row(
    game_date: pd.Timestamp,
    home: str,
    away: str,
    date_label: str,
) -> dict[str, object]:
    return {
        "Date": game_date,
        "HomeTeam": home,
        "AwayTeam": away,
        "HomeTeamName": _schedule_team_name(home),
        "AwayTeamName": _schedule_team_name(away),
        "Score1": pd.NA,
        "Score2": pd.NA,
        "State": "振替日",
        "Status": "makeup",
        "Venue": "未定",
        "StartTime": "",
        "IsMakeup": True,
        "DateLabel": date_label,
    }


def _schedule_team_name(code: str) -> str:
    if code == MAKEUP_UNKNOWN_OPPONENT:
        return MAKEUP_UNKNOWN_OPPONENT_NAME
    return team_label(code)


def _group_date_label(
    group: pd.DataFrame,
    target_team: str | None = None,
    makeup_counts: dict[str, int] | None = None,
) -> str:
    if target_team is not None and "IsMakeup" in group.columns:
        is_makeup = group["IsMakeup"].fillna(False).astype(bool)
        if bool(is_makeup.any()):
            target_mask = (group["HomeTeam"] == target_team) | (group["AwayTeam"] == target_team)
            if bool(target_mask.any()):
                count = (makeup_counts or {}).get("target", int(target_mask.sum()))
                return f"自軍振替日（{count}試合）"
            count = (makeup_counts or {}).get("other", int(is_makeup.sum()))
            return f"他軍振替日（{count}試合）"

    if "DateLabel" not in group.columns:
        return ""
    labels = [
        str(value).strip()
        for value in group["DateLabel"].dropna().unique()
        if str(value).strip()
    ]
    return labels[0] if labels else ""


def _sort_schedule(schedule: pd.DataFrame) -> pd.DataFrame:
    if schedule.empty:
        return schedule.loc[:, SCHEDULE_COLUMNS].reset_index(drop=True)
    frame = schedule.loc[:, SCHEDULE_COLUMNS].copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    return frame.sort_values(["Date", "HomeTeam", "AwayTeam"]).reset_index(drop=True)


def _parse_schedule_month(html: str, year: int) -> pd.DataFrame:
    rows = _parse_rows(html)
    games: list[dict[str, object]] = []
    for row in rows:
        row_id = str(row.get("id", ""))
        match = re.fullmatch(r"date(\d{2})(\d{2})", row_id)
        if not match:
            continue
        cells = row.get("cells", [])
        if not isinstance(cells, list):
            continue
        game_cell = _first_cell_with_part(cells, "team1")
        if game_cell is None:
            continue

        team1_name = _parts_text(game_cell, "team1")
        team2_name = _parts_text(game_cell, "team2")
        home = NPB_NAME_TO_CODE.get(team1_name)
        away = NPB_NAME_TO_CODE.get(team2_name)
        if home is None or away is None:
            continue

        place_cell = _first_cell_with_part(cells, "place")
        score1 = _parts_text(game_cell, "score1")
        score2 = _parts_text(game_cell, "score2")
        state = _parts_text(game_cell, "state")
        status = _game_status(score1, score2, state)

        games.append(
            {
                "Date": pd.Timestamp(date(year, int(match.group(1)), int(match.group(2)))),
                "HomeTeam": home,
                "AwayTeam": away,
                "HomeTeamName": team1_name,
                "AwayTeamName": team2_name,
                "Score1": int(score1) if score1.isdigit() else pd.NA,
                "Score2": int(score2) if score2.isdigit() else pd.NA,
                "State": state,
                "Status": status,
                "Venue": _parts_text(place_cell, "place") if place_cell else "",
                "StartTime": _parts_text(place_cell, "time") if place_cell else "",
                "IsMakeup": False,
                "DateLabel": "",
            }
        )
    return pd.DataFrame(games, columns=SCHEDULE_COLUMNS)


def _first_cell_with_part(
    cells: Iterable[dict[str, object]],
    class_name: str,
) -> dict[str, object] | None:
    for cell in cells:
        parts = cell.get("parts", {})
        if isinstance(parts, dict) and class_name in parts:
            return cell
    return None


def _game_status(score1: str, score2: str, state: str) -> str:
    if state == "中止":
        return "canceled"
    if score1.isdigit() and score2.isdigit() and state != "-":
        return "in_progress"
    if score1.isdigit() and score2.isdigit():
        return "final"
    if state == "-":
        return "scheduled"
    return "other"
