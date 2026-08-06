from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import date

import pandas as pd

from .teams import league_teams


@dataclass(frozen=True)
class MagicAnalysis:
    required_wins: int | None
    is_clinched: bool
    is_lit: bool
    clinch_table: pd.DataFrame
    lighting_table: pd.DataFrame


GAME_RESULT_OPTIONS = ("未入力", "ホーム勝", "引分", "ビジター勝")


@dataclass(frozen=True)
class MagicScenarioAnalysis:
    entered_games: int
    total_games: int
    is_lit: bool
    is_clinched: bool
    current_standings: pd.DataFrame
    condition_table: pd.DataFrame
    timeline: pd.DataFrame
    first_lit_date: pd.Timestamp | None
    first_clinch_date: pd.Timestamp | None
    magic_number: int | None


def analyze_magic_scenario(
    standings: pd.DataFrame,
    schedule: pd.DataFrame,
    league: str,
    target_team: str,
    results: dict[str, str] | None = None,
    official_schedule: pd.DataFrame | None = None,
) -> MagicScenarioAnalysis:
    """Evaluate a game-by-game result scenario for magic and clinching checks.

    Entered results are applied in date order. Unentered games remain in the
    worst/best-case calculation, so the checks are also useful mid-entry.
    """
    teams = league_teams(league)
    base_records = _records_from_standings(standings, teams)
    frame = _prepare_schedule(schedule)
    normalized_results = {
        str(key): _normalize_game_result(value)
        for key, value in (results or {}).items()
    }
    direct_records = _head_to_head_records(
        official_schedule,
        frame,
        normalized_results,
        teams,
        target_team,
    )

    current_records, remaining = _scenario_state(
        base_records,
        frame,
        normalized_results,
        cutoff=None,
    )
    condition_table, is_lit, is_clinched = _condition_table(
        current_records,
        remaining,
        teams,
        target_team,
        direct_records,
    )
    timeline = _scenario_timeline(
        base_records,
        frame,
        normalized_results,
        teams,
        target_team,
        official_schedule,
    )
    first_lit_date = _first_timeline_date(timeline, "IsLit")
    first_clinch_date = _first_timeline_date(timeline, "IsClinched")
    # The summary must show the magic number for the currently applied input,
    # not the number recorded on the first day that the timeline became lit.
    magic_number = _overall_required_wins(condition_table) if is_lit else None

    current_standings = pd.DataFrame(
        [
            {"Team": team, **current_records[team]}
            for team in teams
        ]
    )
    entered_games = sum(
        value != "未入力" for value in normalized_results.values()
    )
    return MagicScenarioAnalysis(
        entered_games=entered_games,
        total_games=len(frame),
        is_lit=is_lit,
        is_clinched=is_clinched,
        current_standings=current_standings,
        condition_table=condition_table,
        timeline=timeline,
        first_lit_date=first_lit_date,
        first_clinch_date=first_clinch_date,
        magic_number=magic_number,
    )


def analyze_magic(
    standings: pd.DataFrame,
    schedule: pd.DataFrame,
    league: str,
    target_team: str,
    official_schedule: pd.DataFrame | None = None,
) -> MagicAnalysis:
    teams = league_teams(league)
    direct_records = _head_to_head_records(
        official_schedule,
        schedule,
        {},
        teams,
        target_team,
    )
    records = {
        str(row.Team): {
            "Wins": int(row.Wins),
            "Losses": int(row.Losses),
            "Ties": int(row.Ties),
        }
        for row in standings.itertuples(index=False)
    }
    remaining = _remaining_by_team(schedule, teams)
    target_remaining = remaining[target_team]
    target_record = records[target_team]

    clinch_rows: list[dict[str, object]] = []
    lighting_rows: list[dict[str, object]] = []

    for rival in teams:
        if rival == target_team:
            continue
        direct_remaining = _direct_remaining(schedule, target_team, rival)
        rival_record = records[rival]
        rival_remaining = remaining[rival]
        needed_wins, target_rate, rival_rate = _needed_wins_vs_rival(
            target_record,
            rival_record,
            target_remaining,
            rival_remaining,
            direct_remaining,
            direct_records.get(rival),
        )
        lit_target_rate, lit_rival_rate, is_lit_vs_rival = _lighting_check_vs_rival(
            target_record,
            rival_record,
            target_remaining,
            rival_remaining,
            direct_remaining,
            direct_records.get(rival),
        )

        clinch_rows.append(
            {
                "Team": rival,
                "TargetRemaining": target_remaining,
                "RivalRemaining": rival_remaining,
                "DirectRemaining": direct_remaining,
                "NeededWins": needed_wins,
                "TargetRate": target_rate,
                "RivalMaxRate": rival_rate,
            }
        )
        lighting_rows.append(
            {
                "Team": rival,
                "DirectRemaining": direct_remaining,
                "TargetScenarioRate": lit_target_rate,
                "RivalMaxRate": lit_rival_rate,
                "IsLit": is_lit_vs_rival,
            }
        )

    clinch_table = pd.DataFrame(clinch_rows)
    lighting_table = pd.DataFrame(lighting_rows)
    required_wins = _overall_required_wins(clinch_table)
    is_clinched = required_wins == 0
    is_lit = bool(lighting_table["IsLit"].all()) if not lighting_table.empty else False

    return MagicAnalysis(
        required_wins=required_wins,
        is_clinched=is_clinched,
        is_lit=is_lit,
        clinch_table=clinch_table,
        lighting_table=lighting_table,
    )


def _remaining_by_team(schedule: pd.DataFrame, teams: tuple[str, ...]) -> dict[str, int]:
    remaining: dict[str, int] = {}
    for team in teams:
        if schedule.empty:
            remaining[team] = 0
        else:
            remaining[team] = int(
                ((schedule["HomeTeam"] == team) | (schedule["AwayTeam"] == team)).sum()
            )
    return remaining


def _records_from_standings(
    standings: pd.DataFrame,
    teams: tuple[str, ...],
) -> dict[str, dict[str, int]]:
    records: dict[str, dict[str, int]] = {}
    for row in standings.itertuples(index=False):
        team = str(row.Team)
        if team not in teams:
            continue
        records[team] = {
            "Wins": int(row.Wins),
            "Losses": int(row.Losses),
            "Ties": int(row.Ties),
        }
    for team in teams:
        records.setdefault(team, {"Wins": 0, "Losses": 0, "Ties": 0})
    return records


def _prepare_schedule(schedule: pd.DataFrame) -> pd.DataFrame:
    if schedule.empty:
        return pd.DataFrame(columns=["GameKey", "Date", "DateLabel", "HomeTeam", "AwayTeam"])
    frame = schedule.copy().reset_index(drop=True)
    if "GameKey" not in frame.columns:
        frame["GameKey"] = [str(index) for index in frame.index]
    frame["GameKey"] = frame["GameKey"].astype(str)
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    if "DateLabel" not in frame.columns:
        frame["DateLabel"] = ""
    frame["DateLabel"] = frame["DateLabel"].fillna("").astype(str).str.strip()
    return frame.sort_values(["Date", "GameKey"], na_position="last").reset_index(drop=True)


def _normalize_game_result(value: object) -> str:
    text = str(value).strip()
    return text if text in GAME_RESULT_OPTIONS else "未入力"


def _scenario_state(
    base_records: dict[str, dict[str, int]],
    schedule: pd.DataFrame,
    results: dict[str, str],
    cutoff: pd.Timestamp | date | None,
) -> tuple[dict[str, dict[str, int]], pd.DataFrame]:
    records = deepcopy(base_records)
    applied_indices: list[int] = []
    for index, row in schedule.iterrows():
        game_date = row.get("Date")
        if cutoff is not None and (
            pd.isna(game_date) or pd.Timestamp(game_date) > pd.Timestamp(cutoff)
        ):
            continue
        key = str(row.get("GameKey", index))
        result = _normalize_game_result(results.get(key, "未入力"))
        if result == "未入力":
            continue
        _apply_game_result(
            records,
            str(row.get("HomeTeam", "")),
            str(row.get("AwayTeam", "")),
            result,
        )
        applied_indices.append(index)

    if applied_indices:
        remaining = schedule.drop(index=applied_indices).reset_index(drop=True)
    else:
        remaining = schedule.copy().reset_index(drop=True)
    return records, remaining


def _apply_game_result(
    records: dict[str, dict[str, int]],
    home: str,
    away: str,
    result: str,
) -> None:
    if result == "ホーム勝":
        _add_result(records, home, "Wins")
        _add_result(records, away, "Losses")
    elif result == "ビジター勝":
        _add_result(records, home, "Losses")
        _add_result(records, away, "Wins")
    elif result == "引分":
        _add_result(records, home, "Ties")
        _add_result(records, away, "Ties")


def _add_result(
    records: dict[str, dict[str, int]],
    team: str,
    field: str,
) -> None:
    if team in records:
        records[team][field] += 1


def _condition_table(
    records: dict[str, dict[str, int]],
    remaining: pd.DataFrame,
    teams: tuple[str, ...],
    target_team: str,
    direct_records: dict[str, dict[str, int]],
) -> tuple[pd.DataFrame, bool, bool]:
    rows: list[dict[str, object]] = []
    for rival in teams:
        if rival == target_team:
            continue
        target_remaining = _team_remaining(remaining, target_team)
        rival_remaining = _team_remaining(remaining, rival)
        direct_remaining = _direct_remaining(remaining, target_team, rival)
        target = records[target_team]
        rival_record = records[rival]

        target_scenario_wins = target["Wins"] + max(0, target_remaining - direct_remaining)
        target_scenario_losses = target["Losses"] + direct_remaining
        target_scenario_rate = _win_rate(target_scenario_wins, target_scenario_losses)
        rival_max_rate = _win_rate(
            rival_record["Wins"] + rival_remaining,
            rival_record["Losses"],
        )
        target_min_rate = _win_rate(
            target["Wins"],
            target["Losses"] + target_remaining,
        )
        rival_max_rate_for_clinch = _win_rate(
            rival_record["Wins"] + rival_remaining,
            rival_record["Losses"],
        )
        needed_wins, _, _ = _needed_wins_vs_rival(
            target,
            rival_record,
            target_remaining,
            rival_remaining,
            direct_remaining,
            direct_records.get(rival),
        )
        rows.append(
            {
                "Team": rival,
                "TargetRemaining": target_remaining,
                "RivalRemaining": rival_remaining,
                "DirectRemaining": direct_remaining,
                "NeededWins": needed_wins,
                "TargetScenarioRate": target_scenario_rate,
                "RivalMaxRate": rival_max_rate,
                "TargetMinRate": target_min_rate,
                "RivalMaxRateForClinch": rival_max_rate_for_clinch,
                "IsLit": _ranking_condition_holds(
                    target_scenario_rate,
                    rival_max_rate,
                    direct_records.get(rival),
                    direct_remaining,
                ),
                "IsClinched": _ranking_condition_holds(
                    target_min_rate,
                    rival_max_rate_for_clinch,
                    direct_records.get(rival),
                    direct_remaining,
                ),
            }
        )
    table = pd.DataFrame(rows)
    is_lit = bool(table["IsLit"].all()) if not table.empty else False
    is_clinched = bool(table["IsClinched"].all()) if not table.empty else False
    return table, is_lit, is_clinched


def _scenario_timeline(
    base_records: dict[str, dict[str, int]],
    schedule: pd.DataFrame,
    results: dict[str, str],
    teams: tuple[str, ...],
    target_team: str,
    official_schedule: pd.DataFrame | None,
) -> pd.DataFrame:
    if schedule.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    dated_schedule = schedule.dropna(subset=["Date"])
    for game_date in dated_schedule["Date"].drop_duplicates().sort_values():
        records, remaining = _scenario_state(
            base_records,
            schedule,
            results,
            cutoff=game_date,
        )
        conditions, is_lit, is_clinched = _condition_table(
            records,
            remaining,
            teams,
            target_team,
            _head_to_head_records(
                official_schedule,
                schedule,
                results,
                teams,
                target_team,
                cutoff=game_date,
            ),
        )
        day_rows = dated_schedule[dated_schedule["Date"] == game_date]
        labels = [label for label in day_rows["DateLabel"].unique() if label]
        target = records[target_team]
        entered = sum(
            _normalize_game_result(results.get(str(row.GameKey), "未入力")) != "未入力"
            for row in dated_schedule.itertuples(index=False)
            if pd.Timestamp(row.Date) <= pd.Timestamp(game_date)
        )
        rows.append(
            {
                "Date": pd.Timestamp(game_date),
                "DateLabel": labels[0] if labels else "",
                "EnteredGames": entered,
                "TargetWins": target["Wins"],
                "TargetLosses": target["Losses"],
                "TargetTies": target["Ties"],
                "TargetRate": _win_rate(target["Wins"], target["Losses"]),
                "RemainingGames": len(remaining),
                "IsLit": is_lit,
                "IsClinched": is_clinched,
                "MagicNumber": _overall_required_wins(conditions),
            }
        )
    return pd.DataFrame(rows)


def _team_remaining(schedule: pd.DataFrame, team: str) -> int:
    if schedule.empty:
        return 0
    return int(((schedule["HomeTeam"] == team) | (schedule["AwayTeam"] == team)).sum())


def _first_timeline_date(frame: pd.DataFrame, column: str) -> pd.Timestamp | None:
    if frame.empty or column not in frame.columns:
        return None
    dates = frame.loc[frame[column].fillna(False).astype(bool), "Date"].dropna()
    return pd.Timestamp(dates.min()) if not dates.empty else None


def _timeline_value(
    frame: pd.DataFrame,
    target_date: pd.Timestamp | None,
    column: str,
) -> int | None:
    if target_date is None or frame.empty or column not in frame.columns:
        return None
    matches = frame[frame["Date"] == pd.Timestamp(target_date)]
    if matches.empty or pd.isna(matches.iloc[0][column]):
        return None
    return int(matches.iloc[0][column])


def _direct_remaining(schedule: pd.DataFrame, target_team: str, rival: str) -> int:
    if schedule.empty:
        return 0
    return int(
        (
            ((schedule["HomeTeam"] == target_team) & (schedule["AwayTeam"] == rival))
            | ((schedule["HomeTeam"] == rival) & (schedule["AwayTeam"] == target_team))
        ).sum()
    )


def _needed_wins_vs_rival(
    target: dict[str, int],
    rival: dict[str, int],
    target_remaining: int,
    rival_remaining: int,
    direct_remaining: int,
    direct_record: dict[str, int] | None = None,
) -> tuple[int | None, float, float]:
    target_non_direct_remaining = max(0, target_remaining - direct_remaining)
    best_target_rate = 0.0
    best_rival_rate = 1.0

    for target_wins_needed in range(target_remaining + 1):
        forced_direct_wins = max(0, target_wins_needed - target_non_direct_remaining)
        rival_wins = rival["Wins"] + rival_remaining - forced_direct_wins
        rival_losses = rival["Losses"] + forced_direct_wins
        target_wins = target["Wins"] + target_wins_needed
        target_losses = target["Losses"] + target_remaining - target_wins_needed
        target_rate = _win_rate(target_wins, target_losses)
        rival_rate = _win_rate(rival_wins, rival_losses)
        best_target_rate = target_rate
        best_rival_rate = rival_rate
        if _ranking_condition_holds(
            target_rate,
            rival_rate,
            direct_record,
            direct_remaining,
            target_direct_wins=forced_direct_wins,
        ):
            return target_wins_needed, target_rate, rival_rate

    return None, best_target_rate, best_rival_rate


def _lighting_check_vs_rival(
    target: dict[str, int],
    rival: dict[str, int],
    target_remaining: int,
    rival_remaining: int,
    direct_remaining: int,
    direct_record: dict[str, int] | None = None,
) -> tuple[float, float, bool]:
    target_wins = target["Wins"] + max(0, target_remaining - direct_remaining)
    target_losses = target["Losses"] + direct_remaining
    rival_wins = rival["Wins"] + rival_remaining
    rival_losses = rival["Losses"]
    target_rate = _win_rate(target_wins, target_losses)
    rival_rate = _win_rate(rival_wins, rival_losses)
    return target_rate, rival_rate, _ranking_condition_holds(
        target_rate,
        rival_rate,
        direct_record,
        direct_remaining,
    )


def _ranking_condition_holds(
    target_rate: float,
    rival_rate: float,
    direct_record: dict[str, int] | None,
    direct_remaining: int,
    target_direct_wins: int = 0,
) -> bool:
    if target_rate > rival_rate:
        return True
    if target_rate < rival_rate:
        return False
    record = direct_record or {}
    target_wins = int(record.get("TargetWins", 0)) + int(target_direct_wins)
    rival_wins = int(record.get("RivalWins", 0)) + max(
        0,
        int(direct_remaining) - int(target_direct_wins),
    )
    return target_wins > rival_wins


def _head_to_head_records(
    official_schedule: pd.DataFrame | None,
    schedule: pd.DataFrame,
    results: dict[str, str],
    teams: tuple[str, ...],
    target_team: str,
    cutoff: pd.Timestamp | date | None = None,
) -> dict[str, dict[str, int]]:
    records = {
        rival: {"TargetWins": 0, "RivalWins": 0, "Ties": 0}
        for rival in teams
        if rival != target_team
    }

    if official_schedule is not None and not official_schedule.empty:
        official = official_schedule.copy()
        official["Date"] = pd.to_datetime(official["Date"], errors="coerce")
        for row in official.itertuples(index=False):
            game_date = getattr(row, "Date", pd.NaT)
            if cutoff is not None and (
                pd.isna(game_date) or pd.Timestamp(game_date) > pd.Timestamp(cutoff)
            ):
                continue
            score_home = pd.to_numeric(getattr(row, "Score1", pd.NA), errors="coerce")
            score_away = pd.to_numeric(getattr(row, "Score2", pd.NA), errors="coerce")
            if pd.isna(score_home) or pd.isna(score_away):
                continue
            _add_head_to_head_result(
                records,
                target_team,
                str(getattr(row, "HomeTeam", "")),
                str(getattr(row, "AwayTeam", "")),
                "引分" if float(score_home) == float(score_away)
                else "ホーム勝" if float(score_home) > float(score_away)
                else "ビジター勝",
            )

    prepared = _prepare_schedule(schedule)
    for row in prepared.itertuples(index=False):
        game_date = getattr(row, "Date", pd.NaT)
        if cutoff is not None and (
            pd.isna(game_date) or pd.Timestamp(game_date) > pd.Timestamp(cutoff)
        ):
            continue
        result = _normalize_game_result(results.get(str(row.GameKey), "未入力"))
        if result == "未入力":
            continue
        _add_head_to_head_result(
            records,
            target_team,
            str(row.HomeTeam),
            str(row.AwayTeam),
            result,
        )
    return records


def _add_head_to_head_result(
    records: dict[str, dict[str, int]],
    target_team: str,
    home: str,
    away: str,
    result: str,
) -> None:
    if home == target_team:
        rival = away
        target_is_home = True
    elif away == target_team:
        rival = home
        target_is_home = False
    else:
        return
    if rival not in records:
        return
    direct = records[rival]
    if result == "引分":
        direct["Ties"] += 1
    elif (result == "ホーム勝") == target_is_home:
        direct["TargetWins"] += 1
    else:
        direct["RivalWins"] += 1


def _overall_required_wins(clinch_table: pd.DataFrame) -> int | None:
    if clinch_table.empty or clinch_table["NeededWins"].isna().any():
        return None
    return int(clinch_table["NeededWins"].max())


def _win_rate(wins: int, losses: int) -> float:
    decisions = wins + losses
    return wins / decisions if decisions else 0.0
