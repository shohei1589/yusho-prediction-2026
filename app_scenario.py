from __future__ import annotations

from datetime import date
from html import escape
import os
import re
import time

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from yusho.npb_client import (
    append_makeup_placeholders,
    fetch_remaining_schedule,
    fetch_schedule,
    fetch_standings,
    schedule_to_daily_opponents,
    standings_as_of,
)
from yusho.magic import MagicScenarioAnalysis, analyze_magic_scenario
from yusho.simulation import SimulationResult, run_simulations
from yusho.teams import (
    CENTRAL,
    FARM_CENTRAL,
    FARM_EAST,
    FARM_WEST,
    PACIFIC,
    is_farm_league,
    league_teams,
    team_label,
)


LEAGUE_LABELS = {
    PACIFIC: "パ・リーグ",
    CENTRAL: "セ・リーグ",
}
LEAGUE_BY_LABEL = {label: code for code, label in LEAGUE_LABELS.items()}
FARM_LEAGUE_LABELS = {
    FARM_WEST: "西地区",
    FARM_CENTRAL: "中地区",
    FARM_EAST: "東地区",
}
FARM_LEAGUE_BY_LABEL = {
    label: code for code, label in FARM_LEAGUE_LABELS.items()
}
MAGIC_INPUT_DEBOUNCE_SECONDS = 2.0
CHART_DIALOG_STATE_KEY = "champion_date_chart_dialog_open"
CHART_DIALOG_SELECTION_KEY = "champion_date_chart_last_selection"
TEAM_ACCENT_COLORS = {
    "G": "#f97316",
    "T": "#facc15",
    "DB": "#0079c1",
    "C": "#d71920",
    "D": "#004b9b",
    "S": "#22c55e",
    "H": "#facc15",
    "F": "#2563eb",
    "M": "#ef4444",
    "Bs": "#8b5cf6",
    "E": "#991b1b",
    "L": "#1d4ed8",
    "OIX": "#64748b",
    "HYT": "#0f766e",
}
TEAM_MATRIX_HEADER_COLORS = {
    "G": "#e87722",
    "T": "#e0b400",
    "DB": "#1479b8",
    "C": "#d71920",
    "D": "#174a8b",
    "S": "#238b57",
    "H": "#e0b400",
    "F": "#3276b1",
    "M": "#9ca3af",
    "Bs": "#174a7c",
    "E": "#d62828",
    "L": "#2d6aa3",
    "OIX": "#6b7280",
    "HYT": "#0f766e",
}
st.set_page_config(page_title="2026 優勝予測", layout="wide")


def _farm_mode_from_query() -> bool:
    value = str(st.query_params.get("farm", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def main() -> None:
    dark_mode = st.session_state.get("dark_mode", False)
    _apply_style(dark_mode)
    farm_mode = _farm_mode_from_query()

    with st.sidebar:
        st.header("条件")
        year = st.number_input("年度", min_value=2026, max_value=2030, value=2026, step=1)
        if farm_mode:
            st.caption("ファームモード")
            league_label = st.radio(
                "ファーム地区",
                list(FARM_LEAGUE_BY_LABEL.keys()),
                index=0,
                horizontal=True,
            )
            league = FARM_LEAGUE_BY_LABEL[league_label]
        else:
            league_label = st.radio("リーグ", list(LEAGUE_BY_LABEL.keys()), horizontal=True)
            league = LEAGUE_BY_LABEL[league_label]
        target_team = st.selectbox(
            "対象球団",
            list(league_teams(league)),
            format_func=team_label,
            index=0,
        )
        start_date = st.date_input("基準日", value=date.today())
        simulation_count = st.slider(
            "試行回数",
            min_value=1_000,
            max_value=20_000,
            value=1_000,
            step=1_000,
        )
        if st.button("公式データを再取得", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    # Keep reproducibility and connection behavior stable without exposing
    # operational settings in the public UI.
    seed_enabled = True
    seed = 42
    verify_ssl = os.getenv("NPB_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
    use_env_proxy = os.getenv("NPB_USE_ENV_PROXY", "false").lower() in {"1", "true", "yes"}

    os.environ["NPB_VERIFY_SSL"] = "true" if verify_ssl else "false"
    os.environ["NPB_USE_ENV_PROXY"] = "true" if use_env_proxy else "false"

    header_left, header_right = st.columns([5.5, 1])
    with header_left:
        title_suffix = "ファーム優勝予測" if farm_mode else "優勝予測"
        st.markdown(
            f"<h1 class='app-title'>{int(year)} {title_suffix}</h1>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<div class='app-caption'>データ出典: NPB.jp 日本野球機構。非公式・非商用の予測ツールです。</div>",
            unsafe_allow_html=True,
        )
    with header_right:
        st.markdown("<div class='mode-control-label'>表示</div>", unsafe_allow_html=True)
        dark_mode = st.toggle("ダーク", value=dark_mode, key="dark_mode")

    try:
        with st.spinner("NPB公式データを取得しています..."):
            standings_result = _cached_standings(int(year), league, verify_ssl, use_env_proxy)
            schedule_result = _cached_schedule(
                int(year),
                league,
                start_date,
                verify_ssl,
                use_env_proxy,
            )
            full_schedule_result = _cached_full_schedule(
                int(year),
                league,
                verify_ssl,
                use_env_proxy,
            )
            external_win_rates = (
                _cached_farm_win_rates(int(year), verify_ssl, use_env_proxy)
                if farm_mode
                else {}
            )
    except Exception as exc:
        st.error("NPB公式データの取得に失敗しました。")
        st.exception(exc)
        return

    base_standings = standings_as_of(
        standings_result.frame,
        full_schedule_result.frame,
        start_date,
    )
    editor_key = f"scenario_editor_{int(year)}_{league}_{start_date.isoformat()}"
    scenario_input = _scenario_input_frame(base_standings, league)
    _initialize_scenario_state(editor_key, scenario_input)

    editor_col, _ = st.columns([0.88, 0.12])
    with editor_col:
        with st.expander("勝敗を編集", expanded=True):
            st.markdown(
                "<div class='scenario-caption'>初期値はNPB公式値です。過去日を基準日にすると、その日の試合開始前時点の勝・敗・分を公式結果から再構成します。基準日当日の結果を入力した場合は、消化済みとして残り日程から除外します。</div>",
                unsafe_allow_html=True,
            )
            reset_col, note_col = st.columns([0.9, 4.8])
            with reset_col:
                if st.button("公式値に戻す", use_container_width=True):
                    _reset_scenario_state(editor_key, scenario_input)
                    st.rerun()
            with note_col:
                st.markdown(
                    "<div class='scenario-note'>「今後勝率」は残り試合での勝率となります（手動で調整可能）</div>",
                    unsafe_allow_html=True,
                )

            _render_scenario_controls(editor_key, scenario_input)

    try:
        scenario_standings, assumed_win_rates = _scenario_to_model_inputs(editor_key, scenario_input)
        base_date_schedule = _schedule_from_base_date(
            full_schedule_result.frame,
            start_date,
        )
        magic_schedule = append_makeup_placeholders(
            base_date_schedule,
            base_standings,
            league,
            full_schedule=full_schedule_result.frame,
        )
        # Keep magic analysis independent from the upper scenario editor.
        # Its base is the official standings and schedule at start_date.
        scenario_standings, base_date_schedule, consumed_games = (
            _consume_entered_start_date_games(
                base_standings,
                scenario_standings,
                base_date_schedule,
                start_date,
                target_team,
            )
        )
        if consumed_games:
            st.info(
                f"入力された{team_label(target_team)}の勝敗を反映し、"
                f"{len(consumed_games)}試合を消化済みとして残り日程から除外しました。"
            )
        completed_schedule = append_makeup_placeholders(
            base_date_schedule,
            scenario_standings,
            league,
            full_schedule=full_schedule_result.frame,
        )
        daily_opponents = schedule_to_daily_opponents(completed_schedule, league, target_team)
        scenario_signature = _scenario_signature(
            scenario_standings,
            assumed_win_rates,
            completed_schedule,
            target_team,
            start_date,
            simulation_count,
            seed_enabled,
            int(seed),
            external_win_rates,
        )
        result_key = f"simulation_result_{int(year)}_{league}"
        run_clicked = st.button("シミュレーション実行", type="primary", use_container_width=True)

        should_run = run_clicked or result_key not in st.session_state
        if should_run:
            with st.spinner("シミュレーションしています..."):
                result = run_simulations(
                    daily_opponents,
                    scenario_standings,
                    league,
                    target_team,
                    simulation_count=simulation_count,
                    seed=int(seed) if seed_enabled else None,
                    assumed_win_rates=assumed_win_rates,
                    external_win_rates=external_win_rates,
                )
            st.session_state[result_key] = {
                "result": result,
                "standings": scenario_standings,
                "assumed_win_rates": assumed_win_rates,
                "external_win_rates": external_win_rates,
                "schedule": completed_schedule,
                "daily_opponents": daily_opponents,
                "signature": scenario_signature,
            }
        stored = st.session_state[result_key]
        result = stored["result"]
        displayed_standings = stored["standings"]
        displayed_rates = stored["assumed_win_rates"]
        displayed_schedule = stored.get("schedule", completed_schedule)
        displayed_daily_opponents = stored.get("daily_opponents", daily_opponents)
        if stored["signature"] != scenario_signature:
            st.warning("入力が変更されています。結果を更新するには「シミュレーション実行」を押してください。")
            displayed_schedule = completed_schedule
            displayed_daily_opponents = daily_opponents
    except Exception as exc:
        st.error("入力値の変換または計算に失敗しました。")
        st.exception(exc)
        return

    _render_summary(
        result,
        displayed_standings,
        displayed_rates,
        displayed_schedule,
        displayed_daily_opponents,
        full_schedule_result.frame,
        base_standings,
        magic_schedule,
        int(year),
        league,
        target_team,
        start_date,
        simulation_count,
        dark_mode,
    )


@st.cache_data(ttl=60 * 30, show_spinner=False)
def _cached_standings(
    year: int,
    league: str,
    verify_ssl: bool,
    use_env_proxy: bool,
) -> object:
    os.environ["NPB_VERIFY_SSL"] = "true" if verify_ssl else "false"
    os.environ["NPB_USE_ENV_PROXY"] = "true" if use_env_proxy else "false"
    return fetch_standings(year, league)


@st.cache_data(ttl=60 * 30, show_spinner=False)
def _cached_schedule(
    year: int,
    league: str,
    start_date: date,
    verify_ssl: bool,
    use_env_proxy: bool,
) -> object:
    os.environ["NPB_VERIFY_SSL"] = "true" if verify_ssl else "false"
    os.environ["NPB_USE_ENV_PROXY"] = "true" if use_env_proxy else "false"
    return fetch_remaining_schedule(year, league, start_date)


@st.cache_data(ttl=60 * 30, show_spinner=False)
def _cached_full_schedule(
    year: int,
    league: str,
    verify_ssl: bool,
    use_env_proxy: bool,
) -> object:
    os.environ["NPB_VERIFY_SSL"] = "true" if verify_ssl else "false"
    os.environ["NPB_USE_ENV_PROXY"] = "true" if use_env_proxy else "false"
    return fetch_schedule(year, league=league)


def _schedule_from_base_date(
    full_schedule: pd.DataFrame,
    start_date: date,
) -> pd.DataFrame:
    if full_schedule.empty or "Date" not in full_schedule.columns:
        return full_schedule.copy()
    frame = full_schedule.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"])
    if "Status" not in frame.columns:
        return frame.iloc[0:0].copy()
    frame = frame[
        (frame["Date"] >= pd.Timestamp(start_date))
        & frame["Status"].isin(["final", "scheduled", "in_progress"])
    ]
    return frame.sort_values(["Date", "HomeTeam", "AwayTeam"]).reset_index(drop=True)


def _consume_entered_start_date_games(
    base_standings: pd.DataFrame,
    scenario_standings: pd.DataFrame,
    schedule: pd.DataFrame,
    start_date: date,
    target_team: str,
) -> tuple[pd.DataFrame, pd.DataFrame, list[tuple[object, str, str, str]]]:
    """Treat entered results from the base date onward as already played.

    The editor starts from standings immediately before ``start_date``. If a
    user enters a result for a scheduled game, keeping it in the simulation
    would count it twice. A repeated result such as +2 wins is assigned to the
    target team's games in chronological order.
    """
    if schedule.empty or "Date" not in schedule.columns:
        return scenario_standings, schedule, []

    candidates = schedule[
        schedule["Status"].isin(["scheduled", "in_progress"])
        & (
            (schedule["HomeTeam"] == target_team)
            | (schedule["AwayTeam"] == target_team)
        )
    ].copy()
    candidates["_sort_date"] = pd.to_datetime(candidates["Date"], errors="coerce")
    candidates = candidates.sort_values(["_sort_date", "HomeTeam", "AwayTeam"])
    if candidates.empty:
        return scenario_standings, schedule, []

    base_by_team = {
        str(row.Team): (int(row.Wins), int(row.Losses), int(row.Ties))
        for row in base_standings.itertuples(index=False)
    }
    entered_by_team = {
        str(row.Team): (int(row.Wins), int(row.Losses), int(row.Ties))
        for row in scenario_standings.itertuples(index=False)
    }
    adjusted = scenario_standings.copy()
    consumed_indices: list[object] = []
    consumed_games: list[tuple[object, str, str, str]] = []

    target_delta = _standing_delta(
        base_by_team[target_team],
        entered_by_team[target_team],
    )
    repeated_result = _repeated_game_result(target_delta)
    if repeated_result is not None:
        game_count = sum(target_delta)
        if game_count <= len(candidates):
            selected = candidates.head(game_count)
            expected_opponent_deltas: dict[str, tuple[int, int, int]] = {}
            for index, game in selected.iterrows():
                home = str(game["HomeTeam"])
                away = str(game["AwayTeam"])
                opponent = away if home == target_team else home
                opponent_result = _opposite_result(repeated_result)
                previous = expected_opponent_deltas.get(opponent, (0, 0, 0))
                expected_opponent_deltas[opponent] = _add_result_delta(
                    previous,
                    opponent_result,
                )
                consumed_indices.append(index)
                home_result = (
                    repeated_result
                    if home == target_team
                    else _opposite_result(repeated_result)
                )
                consumed_games.append(
                    (game["Date"], home, away, home_result)
                )

            missing_opponent_deltas: dict[str, tuple[int, int, int]] = {}
            for opponent, expected_delta in expected_opponent_deltas.items():
                actual_delta = _standing_delta(
                    base_by_team[opponent],
                    entered_by_team[opponent],
                )
                missing_delta = _missing_result_delta(actual_delta, expected_delta)
                if missing_delta is None:
                    return scenario_standings, schedule, []
                missing_opponent_deltas[opponent] = missing_delta

            for opponent, missing_delta in missing_opponent_deltas.items():
                _add_standing_delta(adjusted, opponent, missing_delta)

            remaining = schedule.drop(index=consumed_indices).reset_index(drop=True)
            return adjusted, remaining, consumed_games

    for index, game in candidates.iterrows():
        if pd.to_datetime(game["Date"], errors="coerce").date() != start_date:
            break
        home = str(game["HomeTeam"])
        away = str(game["AwayTeam"])
        if home not in base_by_team or away not in base_by_team:
            continue

        home_delta = _standing_delta(base_by_team[home], entered_by_team[home])
        away_delta = _standing_delta(base_by_team[away], entered_by_team[away])
        result = _matching_game_result(home_delta, away_delta)
        if result is None and target_team in {home, away}:
            target_delta = home_delta if target_team == home else away_delta
            if _is_single_game_delta(target_delta):
                target_result = _delta_to_result(target_delta)
                result = target_result if target_team == home else _opposite_result(target_result)
                _complete_opponent_result(
                    adjusted,
                    away if target_team == home else home,
                    target_result,
                )
        if result is None:
            continue

        consumed_indices.append(index)
        consumed_games.append((game["Date"], home, away, result))

    if not consumed_indices:
        return scenario_standings, schedule, []
    remaining = schedule.drop(index=consumed_indices).reset_index(drop=True)
    return adjusted, remaining, consumed_games


def _standing_delta(
    base: tuple[int, int, int],
    entered: tuple[int, int, int],
) -> tuple[int, int, int]:
    return tuple(entered[index] - base[index] for index in range(3))


def _is_single_game_delta(delta: tuple[int, int, int]) -> bool:
    return delta in {(1, 0, 0), (0, 1, 0), (0, 0, 1)}


def _repeated_game_result(delta: tuple[int, int, int]) -> str | None:
    if sum(delta) <= 0:
        return None
    if delta[0] > 0 and delta[1:] == (0, 0):
        return "Win"
    if delta[1] > 0 and delta[0] == delta[2] == 0:
        return "Lose"
    if delta[2] > 0 and delta[:2] == (0, 0):
        return "Tie"
    return None


def _delta_to_result(delta: tuple[int, int, int]) -> str:
    return {
        (1, 0, 0): "Win",
        (0, 1, 0): "Lose",
        (0, 0, 1): "Tie",
    }[delta]


def _matching_game_result(
    home_delta: tuple[int, int, int],
    away_delta: tuple[int, int, int],
) -> str | None:
    if home_delta == (1, 0, 0) and away_delta == (0, 1, 0):
        return "Win"
    if home_delta == (0, 1, 0) and away_delta == (1, 0, 0):
        return "Lose"
    if home_delta == (0, 0, 1) and away_delta == (0, 0, 1):
        return "Tie"
    return None


def _opposite_result(result: str) -> str:
    return {"Win": "Lose", "Lose": "Win", "Tie": "Tie"}[result]


def _result_delta(result: str) -> tuple[int, int, int]:
    return {"Win": (1, 0, 0), "Lose": (0, 1, 0), "Tie": (0, 0, 1)}[result]


def _add_result_delta(
    current: tuple[int, int, int],
    result: str,
) -> tuple[int, int, int]:
    delta = _result_delta(result)
    return tuple(current[index] + delta[index] for index in range(3))


def _missing_result_delta(
    actual: tuple[int, int, int],
    expected: tuple[int, int, int],
) -> tuple[int, int, int] | None:
    nonzero = [index for index, value in enumerate(expected) if value]
    if not nonzero:
        return (0, 0, 0)
    axis = nonzero[0]
    if any(value != 0 for index, value in enumerate(actual) if index != axis):
        return None
    if actual[axis] < 0 or actual[axis] > expected[axis]:
        return None
    return tuple(
        expected[index] - actual[index]
        for index in range(3)
    )


def _add_standing_delta(
    standings: pd.DataFrame,
    team: str,
    delta: tuple[int, int, int],
) -> None:
    row_index = standings.index[standings["Team"].astype(str) == team]
    if len(row_index) != 1:
        return
    index = row_index[0]
    for column, value in zip(("Wins", "Losses", "Ties"), delta):
        standings.at[index, column] = int(standings.at[index, column]) + value


def _complete_opponent_result(
    standings: pd.DataFrame,
    opponent: str,
    target_result: str,
) -> None:
    result = _opposite_result(target_result)
    row_index = standings.index[standings["Team"].astype(str) == opponent]
    if len(row_index) != 1:
        return
    index = row_index[0]
    column = {"Win": "Wins", "Lose": "Losses", "Tie": "Ties"}[result]
    standings.at[index, column] = int(standings.at[index, column]) + 1


@st.cache_data(ttl=60 * 30, show_spinner=False)
def _cached_farm_win_rates(
    year: int,
    verify_ssl: bool,
    use_env_proxy: bool,
) -> dict[str, float]:
    os.environ["NPB_VERIFY_SSL"] = "true" if verify_ssl else "false"
    os.environ["NPB_USE_ENV_PROXY"] = "true" if use_env_proxy else "false"
    rates: dict[str, float] = {}
    for farm_league in (FARM_WEST, FARM_CENTRAL, FARM_EAST):
        frame = fetch_standings(year, farm_league).frame
        rates.update(
            {
                str(row.Team): float(row.WinRate)
                for row in frame.itertuples(index=False)
            }
        )
    return rates


def _render_summary(
    result: SimulationResult,
    standings: pd.DataFrame,
    assumed_win_rates: dict[str, float],
    schedule: pd.DataFrame,
    daily_opponents: pd.DataFrame,
    full_schedule: pd.DataFrame,
    magic_standings: pd.DataFrame,
    magic_schedule: pd.DataFrame,
    year: int,
    league: str,
    target_team: str,
    start_date: date,
    simulation_count: int,
    dark_mode: bool,
) -> None:
    team_name = team_label(target_team)
    probability = result.champion_probability * 100

    metric_cols = st.columns([1, 1, 1])
    metric_cols[0].metric(f"{team_name} 優勝確率", f"{probability:.1f}%")
    metric_cols[1].metric("対象球団の残り試合", f"{_remaining_games(schedule, target_team)}")
    metric_cols[2].metric("試行回数", f"{simulation_count:,}")
    makeup_summary = _makeup_summary(schedule)
    if makeup_summary:
        st.markdown(makeup_summary, unsafe_allow_html=True)

    tab_result, tab_magic, tab_standings, tab_schedule, tab_model = st.tabs(
        ["予測", "マジック", "入力値", "残り日程", "前提"]
    )

    with tab_result:
        left, right = st.columns([2, 1])
        with left:
            champion_chart = _champion_date_chart(
                result,
                target_team,
                team_name,
                dark_mode,
            )
            chart_event = st.plotly_chart(
                champion_chart,
                use_container_width=True,
                key=f"champion_date_chart_{year}_{league}_{target_team}",
                on_select="rerun",
                selection_mode="points",
                config={
                    "displayModeBar": False,
                    "scrollZoom": False,
                    "responsive": True,
                },
            )
            selection_signature = _plotly_chart_selection_signature(chart_event)
            if selection_signature and selection_signature != st.session_state.get(
                CHART_DIALOG_SELECTION_KEY
            ):
                st.session_state[CHART_DIALOG_SELECTION_KEY] = selection_signature
                st.session_state[CHART_DIALOG_STATE_KEY] = True
            if st.button(
                "グラフを拡大",
                key=f"champion_date_chart_expand_{year}_{league}_{target_team}",
            ):
                st.session_state[CHART_DIALOG_STATE_KEY] = True
            if st.session_state.get(CHART_DIALOG_STATE_KEY, False):
                _show_champion_date_chart_dialog(
                    champion_chart,
                    f"champion_date_chart_dialog_{year}_{league}_{target_team}",
                )
        with right:
            st.subheader("優勝確定日 上位")
            _render_table(_top_dates(result.champion_dates))
            st.subheader("平均最終成績")
            _render_table(
                _format_final_standings_table(result.final_standings),
                table_class="styled-table final-standings-table",
            )

    with tab_magic:
        _render_magic_analysis(
            magic_standings,
            magic_schedule,
            full_schedule,
            year,
            league,
            target_team,
        )

    with tab_standings:
        left, right = st.columns([3, 2])
        with left:
            st.subheader("シナリオ勝敗表")
            _render_table(_format_standings(standings, league))
        with right:
            st.subheader("今後の想定勝率")
            _render_table(_format_assumed_rates(assumed_win_rates))

    with tab_schedule:
        st.caption(f"基準日: {start_date.isoformat()} 以降の{team_label(target_team)}戦だけを表示しています。")
        _render_schedule_calendar(
            result.champion_dates,
            schedule,
            target_team,
            int(year),
            league,
            start_date,
        )
        _render_table(_format_schedule(schedule, target_team))
        if makeup_summary:
            st.markdown(makeup_summary, unsafe_allow_html=True)

    with tab_model:
        if is_farm_league(league):
            st.info(
                "ファーム地区の判定では、選択した地区のチームだけを優勝争いの比較対象にします。"
                "地区外との交流戦は各チームの勝敗・残り試合に含め、相手地区の公式勝率を使ってシミュレーションします。"
                "振替試合は仮置きせず、NPB公式に掲載された日程だけを使用します。"
            )
        st.markdown(
            """
- 基準日は「その日の試合開始前」として扱います。
- 基準日当日の試合結果を勝敗表へ入力した場合は、その試合を消化済みとして残り日程から除外します。相手側を未入力のままにした場合は、反対結果を自動補完します。
- 勝敗表の初期値はNPB.jpから取得した現在値です。基準日が過去の場合は、その日の試合開始前時点の勝・敗・分を公式結果から再構成します。
- 今後の想定勝率は、残り試合の勝敗確率を決めるために使います。
- 残り試合はモンテカルロ法で多数回シミュレーションし、優勝確率と優勝確定日分布を推定します。
- 各試合の勝敗確率は、両チームの今後想定勝率からLog5風のオッズ比で計算します。
- 引分の発生、先発投手、球場、移動、故障者、雨天中止の追加発生はモデルに含めていません。
- マジック点灯は、対象球団が残りの直接対決を全敗し、それ以外の残り試合を全勝した場合の最終勝率が、各ライバル球団の残り試合全勝時の最終勝率を上回る場合と判定します。
- マジック数は、各ライバル球団が残り試合を全勝すると仮定し、対象球団が最終勝率でそれを上回るために必要な追加勝利数の最大値です。勝率が同率の場合は直接対戦成績で判定します。
- 勝率が同率になる場合は、残りの直接対決を対象球団の敗戦としても、公式の過去結果と入力済み結果を合算した直接対戦成績が対象球団優位であれば条件クリアと判定します。
- 優勝確定日は、各日終了時点で「対象チームの残り試合を含めた最低勝率」が「他チームの残り試合を含めた最高勝率」を上回る最初の日として判定しています。
"""
        )


def _scenario_input_frame(standings: pd.DataFrame, league: str) -> pd.DataFrame:
    frame = standings.copy()
    current_rate = frame.apply(lambda row: _win_rate(row["Wins"], row["Losses"]), axis=1)
    frame["現在勝率"] = current_rate
    frame = frame.sort_values(
        ["現在勝率", "Wins", "Losses"],
        ascending=[False, False, True],
    )
    return pd.DataFrame(
        {
            "Team": frame["Team"],
            "球団": frame["Team"].map(team_label),
            "勝": frame["Wins"].astype(int),
            "敗": frame["Losses"].astype(int),
            "分": frame["Ties"].astype(int),
            "現在勝率": current_rate.round(3),
            "今後の想定勝率": current_rate.round(3),
        }
    )


def _initialize_scenario_state(prefix: str, scenario_input: pd.DataFrame) -> None:
    for _, row in scenario_input.iterrows():
        team = str(row["Team"])
        defaults = {
            "wins": int(row["勝"]),
            "losses": int(row["敗"]),
            "ties": int(row["分"]),
            "rate": _rate_display(float(row["今後の想定勝率"])),
        }
        for field, value in defaults.items():
            key = _scenario_widget_key(prefix, team, field)
            if key not in st.session_state:
                st.session_state[key] = value


def _reset_scenario_state(prefix: str, scenario_input: pd.DataFrame) -> None:
    for _, row in scenario_input.iterrows():
        team = str(row["Team"])
        values = {
            "wins": int(row["勝"]),
            "losses": int(row["敗"]),
            "ties": int(row["分"]),
            "rate": _rate_display(float(row["今後の想定勝率"])),
        }
        for field, value in values.items():
            st.session_state[_scenario_widget_key(prefix, team, field)] = value


def _render_scenario_controls(prefix: str, scenario_input: pd.DataFrame) -> None:
    _render_mobile_scenario_table(prefix, scenario_input)
    mobile_edit_open = str(st.query_params.get("mobile_edit", "0")) == "1"
    edit_label = "閲覧に戻る" if mobile_edit_open else "勝敗を編集する"
    edit_target = "0" if mobile_edit_open else "1"
    st.markdown(
        f"<div class='mobile-edit-actions'><a class='mobile-edit-link' href='?mobile_edit={edit_target}'>{edit_label}</a></div>",
        unsafe_allow_html=True,
    )
    if mobile_edit_open:
        st.markdown("<div class='mobile-edit-enabled'></div>", unsafe_allow_html=True)

    _render_desktop_scenario_grid(prefix, scenario_input)


def _render_desktop_scenario_grid(prefix: str, scenario_input: pd.DataFrame) -> None:
    st.markdown("<div class='scenario-grid'>", unsafe_allow_html=True)
    header = st.columns([1.35, 1.12, 1.12, 1.12, 0.82, 0.82])
    for col, label in zip(header, ["球団", "勝", "敗", "分", "現在勝率", "今後勝率"]):
        col.markdown(f"<div class='scenario-header'>{label}</div>", unsafe_allow_html=True)

    for _, row in scenario_input.iterrows():
        team = str(row["Team"])
        cols = st.columns([1.35, 1.12, 1.12, 1.12, 0.82, 0.82])
        cols[0].markdown(f"<div class='scenario-team'>{row['球団']}</div>", unsafe_allow_html=True)
        with cols[1]:
            _stepper(prefix, team, "wins", "勝")
        with cols[2]:
            _stepper(prefix, team, "losses", "敗")
        with cols[3]:
            _stepper(prefix, team, "ties", "分")
        current_rate = _win_rate(
            st.session_state[_scenario_widget_key(prefix, team, "wins")],
            st.session_state[_scenario_widget_key(prefix, team, "losses")],
        )
        cols[4].markdown(
            f"<span class='compact-rate'>{f'{current_rate:.3f}'.lstrip('0')}</span>",
            unsafe_allow_html=True,
        )
        with cols[5]:
            st.text_input(
                "今後勝率",
                key=_scenario_widget_key(prefix, team, "rate"),
                label_visibility="collapsed",
            )
    st.markdown("</div>", unsafe_allow_html=True)


def _render_mobile_scenario_table(prefix: str, scenario_input: pd.DataFrame) -> None:
    rows: list[dict[str, str]] = []
    for _, row in scenario_input.iterrows():
        team = str(row["Team"])
        wins = int(st.session_state[_scenario_widget_key(prefix, team, "wins")])
        losses = int(st.session_state[_scenario_widget_key(prefix, team, "losses")])
        ties = int(st.session_state[_scenario_widget_key(prefix, team, "ties")])
        current_rate = _win_rate(wins, losses)
        future_rate = str(st.session_state[_scenario_widget_key(prefix, team, "rate")])
        rows.append(
            {
                "球団": str(row["球団"]),
                "勝敗分": f"{wins}-{losses}-{ties}",
                "現在": _rate_display(current_rate),
                "今後": future_rate,
            }
        )
    frame = pd.DataFrame(rows)
    html = frame.to_html(index=False, escape=False, classes="mobile-table")
    st.markdown(f"<div class='mobile-scenario-table'>{html}</div>", unsafe_allow_html=True)


def _stepper(prefix: str, team: str, field: str, label: str) -> None:
    value_key = _scenario_widget_key(prefix, team, field)
    minus_key = f"{value_key}_minus"
    plus_key = f"{value_key}_plus"
    cols = st.columns([0.42, 0.86, 0.42])
    cols[0].button(
        "-",
        key=minus_key,
        on_click=_adjust_int_state,
        args=(value_key, -1, 0),
        use_container_width=True,
    )
    cols[1].number_input(
        label,
        min_value=0,
        max_value=200,
        step=1,
        key=value_key,
        label_visibility="collapsed",
    )
    cols[2].button(
        "+",
        key=plus_key,
        on_click=_adjust_int_state,
        args=(value_key, 1, 0),
        use_container_width=True,
    )


def _adjust_int_state(key: str, delta: int, minimum: int) -> None:
    st.session_state[key] = max(minimum, int(st.session_state.get(key, 0)) + delta)


def _scenario_widget_key(prefix: str, team: str, field: str) -> str:
    return f"{prefix}_{team}_{field}"


def _scenario_to_model_inputs(
    prefix: str,
    scenario_input: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, float]]:
    rows: list[dict[str, object]] = []
    rates: dict[str, float] = {}

    for _, row in scenario_input.iterrows():
        team = str(row["Team"])
        wins = int(st.session_state[_scenario_widget_key(prefix, team, "wins")])
        losses = int(st.session_state[_scenario_widget_key(prefix, team, "losses")])
        ties = int(st.session_state[_scenario_widget_key(prefix, team, "ties")])
        rate = _parse_rate(st.session_state[_scenario_widget_key(prefix, team, "rate")])

        if wins < 0 or losses < 0 or ties < 0:
            raise ValueError("勝・敗・分には0以上の整数を入力してください。")
        if not 0 < rate < 1:
            raise ValueError("今後の想定勝率は0より大きく1未満にしてください。")

        rows.append(
            {
                "Team": team,
                "TeamName": team_label(team),
                "Games": wins + losses + ties,
                "Wins": wins,
                "Losses": losses,
                "Ties": ties,
                "WinRate": _win_rate(wins, losses),
            }
        )
        rates[team] = rate

    return pd.DataFrame(rows), rates


def _scenario_signature(
    standings: pd.DataFrame,
    assumed_win_rates: dict[str, float],
    schedule: pd.DataFrame,
    target_team: str,
    start_date: date,
    simulation_count: int,
    seed_enabled: bool,
    seed: int,
    external_win_rates: dict[str, float] | None = None,
) -> tuple[object, ...]:
    standing_values = tuple(
        (row.Team, int(row.Wins), int(row.Losses), int(row.Ties))
        for row in standings.sort_values("Team").itertuples(index=False)
    )
    rate_values = tuple(
        (team, round(float(rate), 4))
        for team, rate in sorted(assumed_win_rates.items())
    )
    external_rate_values = tuple(
        (team, round(float(rate), 4))
        for team, rate in sorted((external_win_rates or {}).items())
    )
    schedule_values = tuple(
        (
            str(row.Date),
            str(row.HomeTeam),
            str(row.AwayTeam),
            str(getattr(row, "Status", "")),
            str(getattr(row, "Score1", "")),
            str(getattr(row, "Score2", "")),
            bool(getattr(row, "IsMakeup", False)),
            str(getattr(row, "DateLabel", "")),
        )
        for row in schedule.sort_values(["Date", "HomeTeam", "AwayTeam"]).itertuples(index=False)
    )
    return (
        standing_values,
        rate_values,
        external_rate_values,
        schedule_values,
        target_team,
        start_date.isoformat(),
        int(simulation_count),
        bool(seed_enabled),
        int(seed),
    )


def _render_table(frame: pd.DataFrame, table_class: str = "styled-table") -> None:
    html = frame.to_html(index=False, escape=False, classes=table_class)
    st.markdown(f"<div class='table-card'>{html}</div>", unsafe_allow_html=True)


def _render_magic_analysis(
    standings: pd.DataFrame,
    schedule: pd.DataFrame,
    full_schedule: pd.DataFrame,
    year: int,
    league: str,
    target_team: str,
) -> None:
    """Render magic analysis from its own official-base scenario inputs."""
    return _render_magic_analysis_with_apply(
        standings,
        schedule,
        full_schedule,
        year,
        league,
        target_team,
    )
    st.subheader("全試合シナリオ確認")
    remaining_matrix_schedule = _magic_matrix_schedule(schedule)
    past_schedule = _magic_past_schedule(full_schedule, league)
    if remaining_matrix_schedule.empty and past_schedule.empty:
        st.info("入力できる残り試合はありません。")
        return

    show_past_key = f"magic_show_past_{league}_{target_team}"
    st.session_state.setdefault(show_past_key, False)
    past_matrix_schedule = _magic_matrix_schedule(past_schedule)
    if st.session_state[show_past_key] and not past_matrix_schedule.empty:
        matrix_schedule = pd.concat(
            [past_matrix_schedule, remaining_matrix_schedule],
            ignore_index=True,
        )
        matrix_schedule = _magic_matrix_schedule(matrix_schedule)
    else:
        matrix_schedule = remaining_matrix_schedule

    result_state_key = (
        f"magic_matrix_results_{year}_{league}_{target_team}"
    )
    scenario_state_key = (
        f"magic_matrix_scenario_{year}_{league}_{target_team}"
    )
    pending_updated_key = (
        f"magic_matrix_pending_updated_{year}_{league}_{target_team}"
    )
    revision_key = f"magic_matrix_revision_{year}_{league}_{target_team}"
    st.session_state.setdefault(revision_key, 0)
    st.session_state.setdefault(pending_updated_key, 0.0)
    _render_magic_live_content(
        standings,
        remaining_matrix_schedule,
        matrix_schedule,
        full_schedule,
        year,
        league,
        target_team,
        result_state_key,
        scenario_state_key,
        pending_updated_key,
        revision_key,
        show_past_key,
    )


@st.fragment(run_every=0.5)
def _render_magic_live_content(
    standings: pd.DataFrame,
    remaining_matrix_schedule: pd.DataFrame,
    matrix_schedule: pd.DataFrame,
    full_schedule: pd.DataFrame,
    year: int,
    league: str,
    target_team: str,
    result_state_key: str,
    scenario_state_key: str,
    pending_updated_key: str,
    revision_key: str,
    show_past_key: str,
) -> None:
    results = dict(st.session_state.get(result_state_key, {}))
    scenario = st.session_state.get(scenario_state_key)
    pending_updated_at = float(st.session_state.get(pending_updated_key, 0.0) or 0.0)
    calculation_due = (
        pending_updated_at > 0
        and time.time() - pending_updated_at >= MAGIC_INPUT_DEBOUNCE_SECONDS
    )
    if scenario is None or calculation_due:
        scenario = analyze_magic_scenario(
            standings,
            remaining_matrix_schedule,
            league,
            target_team,
            results,
            full_schedule,
        )
        st.session_state[scenario_state_key] = scenario
        st.session_state[pending_updated_key] = 0.0

    widget_prefix = f"{result_state_key}_{st.session_state[revision_key]}"
    summary_col, matrix_col = st.columns([0.78, 4.5])
    with summary_col:
        _render_magic_scenario_result(
            scenario,
            result_state_key,
            scenario_state_key,
            pending_updated_key,
            revision_key,
            year,
            league,
            target_team,
        )
    with matrix_col:
        st.markdown("<div class='magic-matrix-marker'></div>", unsafe_allow_html=True)
        toggle_label = "− 過去日を隠す" if st.session_state[show_past_key] else "＋ 過去日を表示"
        if st.button(
            toggle_label,
            key=f"magic_past_toggle_{league}_{target_team}",
            use_container_width=False,
        ):
            st.session_state[show_past_key] = not st.session_state[show_past_key]
            st.rerun()
        with st.container(height=720, border=True):
            _render_magic_matrix_header(league)
            _render_magic_game_matrix(
                matrix_schedule,
                result_state_key,
                widget_prefix,
                pending_updated_key,
                revision_key,
                league,
            )
    st.markdown("<div class='magic-team-status-title'>チーム別の判定</div>", unsafe_allow_html=True)
    _render_table(_format_magic_team_status_table(scenario.condition_table))


def _magic_matrix_schedule(schedule: pd.DataFrame) -> pd.DataFrame:
    if schedule.empty or "Date" not in schedule.columns:
        return pd.DataFrame()
    frame = schedule.copy().reset_index(drop=True)
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"])
    if frame.empty:
        return pd.DataFrame()
    if "DateLabel" not in frame.columns:
        frame["DateLabel"] = ""
    frame["DateLabel"] = frame["DateLabel"].fillna("").astype(str).str.strip()
    frame = frame.sort_values(["Date", "HomeTeam", "AwayTeam"]).reset_index(drop=True)
    duplicate_number = frame.groupby(
        ["Date", "HomeTeam", "AwayTeam"],
        dropna=False,
    ).cumcount()
    frame["GameKey"] = (
        frame["Date"].dt.strftime("%Y%m%d")
        + "_"
        + frame["HomeTeam"].astype(str)
        + "_"
        + frame["AwayTeam"].astype(str)
        + "_"
        + duplicate_number.astype(str)
    )
    if "IsPast" not in frame.columns:
        frame["IsPast"] = False
    else:
        frame["IsPast"] = frame["IsPast"].fillna(False).astype(bool)
    return frame


def _magic_past_schedule(schedule: pd.DataFrame, league: str) -> pd.DataFrame:
    if schedule.empty or "Date" not in schedule.columns:
        return pd.DataFrame()
    frame = schedule.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    league_codes = set(league_teams(league))
    frame = frame[
        (frame["Date"] < pd.Timestamp(date.today()))
        & (frame["HomeTeam"].isin(league_codes) | frame["AwayTeam"].isin(league_codes))
    ].copy()
    if frame.empty:
        return frame
    frame["IsPast"] = True
    return frame


def _render_magic_game_matrix(
    schedule: pd.DataFrame,
    result_state_key: str,
    widget_prefix: str,
    pending_updated_key: str,
    revision_key: str,
    league: str,
) -> dict[str, str]:
    teams = league_teams(league)
    if result_state_key not in st.session_state:
        st.session_state[result_state_key] = {}
    results = st.session_state[result_state_key]
    schedule_by_date = schedule.groupby("Date", sort=True)

    column_widths = [1.1] + [1.0, 0.72] * len(teams)

    for game_date, group in schedule_by_date:
        row_columns = st.columns(column_widths)
        date_labels = [str(value).strip() for value in group["DateLabel"] if str(value).strip()]
        timestamp = pd.Timestamp(game_date)
        date_label = date_labels[0] if date_labels else f"{timestamp.month}月{timestamp.day}日"
        row_columns[0].markdown(
            f"<div class='magic-matrix-date'>{date_label}</div>",
            unsafe_allow_html=True,
        )
        for team, opponent_column, result_column in zip(
            teams,
            row_columns[1::2],
            row_columns[2::2],
        ):
            team_games = group[
                (group["HomeTeam"] == team) | (group["AwayTeam"] == team)
            ]
            if team_games.empty:
                opponent_column.markdown("<div class='magic-matrix-empty'>—</div>", unsafe_allow_html=True)
                result_column.markdown("<div class='magic-matrix-empty'> </div>", unsafe_allow_html=True)
                continue
            for game in team_games.itertuples(index=False):
                game_key = str(game.GameKey)
                opponent = str(game.AwayTeam if game.HomeTeam == team else game.HomeTeam)
                opponent_label = "未定" if opponent == "TBD" else opponent
                opponent_color = TEAM_MATRIX_HEADER_COLORS.get(opponent, "#172033")
                with opponent_column:
                    st.markdown(
                        f"<div class='magic-matrix-opponent' style='color:{opponent_color};'>{opponent_label}</div>",
                        unsafe_allow_html=True,
                    )
                if bool(getattr(game, "IsPast", False)):
                    past_symbol = _official_result_symbol(game, team)
                    result_column.markdown(
                        f"<div class='magic-matrix-result-readonly'>{past_symbol}</div>",
                        unsafe_allow_html=True,
                    )
                    continue
                widget_key = _magic_result_widget_key(widget_prefix, game_key, team)
                current_result = str(results.get(game_key, "未入力"))
                default_symbol = _result_symbol_for_team(current_result, team, game.HomeTeam)
                if st.session_state.get(widget_key) != default_symbol:
                    st.session_state[widget_key] = default_symbol
                with result_column:
                    st.selectbox(
                        "結果",
                        options=["未", "○", "●", "△"],
                        key=widget_key,
                        label_visibility="collapsed",
                        on_change=_sync_magic_matrix_result,
                        args=(
                            result_state_key,
                            pending_updated_key,
                            revision_key,
                            game_key,
                            team,
                            str(game.HomeTeam),
                            widget_key,
                            opponent,
                        ),
                    )
    return {str(key): str(value) for key, value in results.items()}


def _official_result_symbol(game: object, team: str) -> str:
    score_home = getattr(game, "Score1", pd.NA)
    score_away = getattr(game, "Score2", pd.NA)
    if pd.isna(score_home) or pd.isna(score_away):
        return "未"
    if float(score_home) == float(score_away):
        return "△"
    home_won = float(score_home) > float(score_away)
    return "○" if (team == game.HomeTeam) == home_won else "●"


def _render_magic_matrix_header(league: str) -> None:
    teams = league_teams(league)
    column_widths = [1.1] + [1.0, 0.72] * len(teams)
    header = st.columns(column_widths)
    header[0].markdown("<div class='magic-matrix-header'>日付</div>", unsafe_allow_html=True)
    for team, opponent_column, result_column in zip(teams, header[1::2], header[2::2]):
        team_color = TEAM_MATRIX_HEADER_COLORS.get(team, "#4b5563")
        team_text_color = "#172033" if team in {"H", "T", "M"} else "#ffffff"
        opponent_column.markdown(
            f"<div class='magic-matrix-header magic-matrix-team-header' style='background:{team_color};color:{team_text_color};'>{team}</div>",
            unsafe_allow_html=True,
        )
        result_column.markdown(
            f"<div class='magic-matrix-header magic-matrix-result-header' style='background:{team_color};color:{team_text_color};'>&nbsp;</div>",
            unsafe_allow_html=True,
        )


def _magic_result_widget_key(result_state_key: str, game_key: str, team: str) -> str:
    return f"{result_state_key}_{game_key}_{team}"


def _sync_magic_matrix_result(
    result_state_key: str,
    pending_updated_key: str,
    revision_key: str,
    game_key: str,
    team: str,
    home: str,
    widget_key: str,
    opponent_team: str,
) -> None:
    symbol = str(st.session_state.get(widget_key, "未"))
    result = _game_result_from_symbol(symbol, team, home)
    results = dict(st.session_state.get(result_state_key, {}))
    if result == "未入力":
        results.pop(game_key, None)
    else:
        results[game_key] = result
    st.session_state[result_state_key] = results
    st.session_state[pending_updated_key] = time.time()
    next_revision = int(st.session_state.get(revision_key, 0)) + 1
    st.session_state[revision_key] = next_revision
    next_prefix = f"{result_state_key}_{next_revision}"
    st.session_state[_magic_result_widget_key(next_prefix, game_key, team)] = symbol
    st.session_state[_magic_result_widget_key(next_prefix, game_key, opponent_team)] = (
        _result_symbol_for_team(result, opponent_team, home)
        if result != "未入力"
        else "未"
    )


def _game_result_from_symbol(symbol: str, team: str, home: str) -> str:
    if symbol == "○":
        return "ホーム勝" if team == home else "ビジター勝"
    elif symbol == "●":
        return "ビジター勝" if team == home else "ホーム勝"
    elif symbol == "△":
        return "引分"
    return "未入力"


def _result_symbol_for_team(result: str, team: str, home: str) -> str:
    if result == "引分":
        return "△"
    if result == "ホーム勝":
        return "○" if team == home else "●"
    if result == "ビジター勝":
        return "●" if team == home else "○"
    return "未"


def _render_magic_scenario_result(
    scenario: MagicScenarioAnalysis,
    result_state_key: str,
    scenario_state_key: str,
    pending_updated_key: str,
    revision_key: str,
    year: int,
    league: str,
    target_team: str,
) -> None:
    st.markdown("<div class='magic-summary-marker'></div>", unsafe_allow_html=True)
    st.markdown("<div class='magic-summary-title'>判定サマリー</div>", unsafe_allow_html=True)
    champion_value = "優勝決定！" if scenario.is_clinched else "未確定"
    champion_class = (
        "magic-summary-value magic-summary-value-champion"
        if scenario.is_clinched
        else "magic-summary-value"
    )
    magic_status = "点灯" if scenario.is_lit else "未点灯"
    magic_number = scenario.magic_number
    magic_display = "—" if magic_number is None or not scenario.is_lit else f"M{magic_number}"
    magic_class = (
        "magic-summary-value magic-summary-value-alert"
        if scenario.is_lit
        else "magic-summary-value"
    )
    st.markdown(
        "<div class='magic-summary-cards'>"
        f"<div class='magic-summary-card'><div class='magic-summary-card-label'>優勝確認</div>"
        f"<div class='{champion_class}'>{champion_value}</div></div>"
        f"<div class='magic-summary-card'><div class='magic-summary-card-label'>マジック点灯確認</div>"
        f"<div class='{magic_class}'>{magic_status}</div></div>"
        f"<div class='magic-summary-card'><div class='magic-summary-card-label'>マジック数</div>"
        f"<div class='{magic_class}'>{magic_display}</div></div>"
        "</div>",
        unsafe_allow_html=True,
    )
    metric_cols[0].metric("マジック数", magic_display)
    if st.button(
        "結果入力をリセット",
        key=f"magic_game_reset_button_{year}_{league}_{target_team}",
        use_container_width=True,
    ):
        st.session_state[result_state_key] = {}
        st.session_state[scenario_state_key] = None
        st.session_state[pending_updated_key] = 0.0
        st.session_state[revision_key] += 1
        st.rerun()


def _render_magic_analysis_with_apply(
    standings: pd.DataFrame,
    schedule: pd.DataFrame,
    full_schedule: pd.DataFrame,
    year: int,
    league: str,
    target_team: str,
) -> None:
    st.subheader("全試合シナリオ確認")
    remaining_matrix_schedule = _magic_matrix_schedule(schedule)
    past_schedule = _magic_past_schedule(full_schedule, league)
    if remaining_matrix_schedule.empty and past_schedule.empty:
        st.info("入力できる残り試合はありません。")
        return

    show_past_key = f"magic_show_past_{league}_{target_team}"
    st.session_state.setdefault(show_past_key, False)
    past_matrix_schedule = _magic_matrix_schedule(past_schedule)
    if st.session_state[show_past_key] and not past_matrix_schedule.empty:
        matrix_schedule = pd.concat(
            [past_matrix_schedule, remaining_matrix_schedule],
            ignore_index=True,
        )
        matrix_schedule = _magic_matrix_schedule(matrix_schedule)
    else:
        matrix_schedule = remaining_matrix_schedule

    applied_state_key = f"magic_matrix_results_{year}_{league}_{target_team}"
    draft_state_key = f"magic_matrix_draft_{year}_{league}_{target_team}"
    scenario_state_key = f"magic_matrix_scenario_{year}_{league}_{target_team}"
    scenario_signature_key = f"magic_matrix_scenario_signature_{year}_{league}_{target_team}"
    revision_key = f"magic_matrix_revision_{year}_{league}_{target_team}"
    st.session_state.setdefault(applied_state_key, {})
    st.session_state.setdefault(
        draft_state_key,
        dict(st.session_state.get(applied_state_key, {})),
    )
    st.session_state.setdefault(revision_key, 0)
    current_scenario_signature = _magic_scenario_signature(
        standings,
        remaining_matrix_schedule,
    )

    summary_col, matrix_col = st.columns([0.78, 4.5])
    with matrix_col:
        st.markdown("<div class='magic-matrix-marker'></div>", unsafe_allow_html=True)
        control_cols = st.columns([1, 1])
        with control_cols[0]:
            toggle_label = (
                "− 過去日を隠す"
                if st.session_state[show_past_key]
                else "＋ 過去日を表示"
            )
            if st.button(
                toggle_label,
                key=f"magic_past_toggle_{league}_{target_team}",
                use_container_width=False,
            ):
                st.session_state[show_past_key] = not st.session_state[show_past_key]
                st.rerun()
        with control_cols[1]:
            st.markdown(
                "<div class='magic-matrix-help'>"
                "1試合につき入力できる欄は1つです。灰色の欄は反映後に相手チームの結果が表示されます。"
                "入力した結果を判定へ反映するには「入力内容を反映」を押してください。"
                "</div>",
                unsafe_allow_html=True,
            )
            if st.button(
                "入力内容を反映",
                key=f"magic_apply_button_{year}_{league}_{target_team}",
                use_container_width=False,
            ):
                _sync_magic_draft_from_widgets(draft_state_key)
                st.session_state[applied_state_key] = dict(
                    st.session_state.get(draft_state_key, {})
                )
                st.session_state[scenario_state_key] = None
                st.session_state[revision_key] += 1
                st.rerun()

    applied_results = dict(st.session_state.get(applied_state_key, {}))
    scenario = st.session_state.get(scenario_state_key)
    if (
        scenario is None
        or st.session_state.get(scenario_signature_key) != current_scenario_signature
    ):
        scenario = analyze_magic_scenario(
            standings,
            remaining_matrix_schedule,
            league,
            target_team,
            applied_results,
            full_schedule,
        )
        st.session_state[scenario_state_key] = scenario
        st.session_state[scenario_signature_key] = current_scenario_signature

    widget_prefix = f"{draft_state_key}_{st.session_state[revision_key]}"
    with summary_col:
        _render_magic_scenario_result_applied(
            scenario,
            draft_state_key,
            applied_state_key,
            scenario_state_key,
            revision_key,
            year,
            league,
            target_team,
        )
    with matrix_col:
        st.markdown(
            "<div class='magic-matrix-header-host'></div>",
            unsafe_allow_html=True,
        )
        _render_magic_matrix_header(league)
        with st.container(height=720, border=True):
            _render_magic_game_matrix_draft(
                matrix_schedule,
                draft_state_key,
                widget_prefix,
                league,
            )

    st.markdown(
        "<div class='magic-team-status-title'>チーム別の判定</div>",
        unsafe_allow_html=True,
    )
    _render_table(_format_magic_team_status_table(scenario.condition_table))


def _render_magic_game_matrix_draft(
    schedule: pd.DataFrame,
    draft_state_key: str,
    widget_prefix: str,
    league: str,
) -> None:
    teams = league_teams(league)
    st.session_state.setdefault(draft_state_key, {})
    draft_results = st.session_state[draft_state_key]
    widget_map_key = f"{draft_state_key}_widget_map"
    widget_map: dict[str, list[tuple[str, str, str]]] = {}
    schedule_by_date = schedule.groupby("Date", sort=True)
    column_widths = [1.1] + [1.0, 0.72] * len(teams)

    for game_date, group in schedule_by_date:
        row_columns = st.columns(column_widths)
        date_labels = [
            str(value).strip()
            for value in group["DateLabel"]
            if str(value).strip()
        ]
        timestamp = pd.Timestamp(game_date)
        date_label = (
            date_labels[0]
            if date_labels
            else f"{timestamp.month}月{timestamp.day}日"
        )
        row_columns[0].markdown(
            f"<div class='magic-matrix-date'>{date_label}</div>",
            unsafe_allow_html=True,
        )
        for team, opponent_column, result_column in zip(
            teams,
            row_columns[1::2],
            row_columns[2::2],
        ):
            team_games = group[
                (group["HomeTeam"] == team) | (group["AwayTeam"] == team)
            ]
            if team_games.empty:
                opponent_column.markdown(
                    "<div class='magic-matrix-empty'>—</div>",
                    unsafe_allow_html=True,
                )
                result_column.markdown(
                    "<div class='magic-matrix-empty'> </div>",
                    unsafe_allow_html=True,
                )
                continue

            for game in team_games.itertuples(index=False):
                game_key = str(game.GameKey)
                opponent = str(
                    game.AwayTeam if game.HomeTeam == team else game.HomeTeam
                )
                opponent_label = "未定" if opponent == "TBD" else opponent
                opponent_color = TEAM_MATRIX_HEADER_COLORS.get(
                    opponent,
                    "#172033",
                )
                with opponent_column:
                    st.markdown(
                        "<div class='magic-matrix-opponent' "
                        f"style='color:{opponent_color};'>{opponent_label}</div>",
                        unsafe_allow_html=True,
                    )

                if bool(getattr(game, "IsPast", False)):
                    past_symbol = _official_result_symbol(game, team)
                    result_column.markdown(
                        f"<div class='magic-matrix-result-readonly'>{past_symbol}</div>",
                        unsafe_allow_html=True,
                    )
                    continue

                widget_key = _magic_result_widget_key(
                    widget_prefix,
                    game_key,
                    team,
                )
                current_result = str(draft_results.get(game_key, "未入力"))
                default_symbol = _result_symbol_for_team(
                    current_result,
                    team,
                    game.HomeTeam,
                )
                editable_team = _magic_editable_team(
                    str(game.HomeTeam),
                    str(game.AwayTeam),
                    teams,
                )
                if team != editable_team:
                    result_column.markdown(
                        f"<div class='magic-matrix-result-disabled'>{default_symbol}</div>",
                        unsafe_allow_html=True,
                    )
                    continue
                if widget_key not in st.session_state:
                    st.session_state[widget_key] = default_symbol
                widget_map.setdefault(game_key, []).append(
                    (widget_key, team, str(game.HomeTeam))
                )
                with result_column:
                    st.selectbox(
                        "結果",
                        options=["未", "○", "●", "△"],
                        key=widget_key,
                        label_visibility="collapsed",
                    )
    st.session_state[widget_map_key] = widget_map


def _magic_scenario_signature(
    standings: pd.DataFrame,
    schedule: pd.DataFrame,
) -> tuple[object, ...]:
    standing_values = tuple(
        (
            str(row.Team),
            int(row.Wins),
            int(row.Losses),
            int(row.Ties),
        )
        for row in standings.sort_values("Team").itertuples(index=False)
    )
    schedule_values = tuple(
        (
            str(row.Date),
            str(row.HomeTeam),
            str(row.AwayTeam),
            str(getattr(row, "DateLabel", "")),
            bool(getattr(row, "IsMakeup", False)),
        )
        for row in schedule.sort_values(
            ["Date", "HomeTeam", "AwayTeam"]
        ).itertuples(index=False)
    )
    return standing_values, schedule_values


def _magic_editable_team(
    home: str,
    away: str,
    teams: tuple[str, ...],
) -> str:
    """Choose one stable input side for each game in the matrix."""
    present = {home, away}
    for team in teams:
        if team in present:
            return team
    return home


def _sync_magic_draft_from_widgets(draft_state_key: str) -> None:
    widget_map = st.session_state.get(
        f"{draft_state_key}_widget_map",
        {},
    )
    draft_results = dict(st.session_state.get(draft_state_key, {}))
    for game_key, sides in widget_map.items():
        candidates = []
        changed_candidates = []
        for widget_key, team, home in sides:
            symbol = str(st.session_state.get(widget_key, "未"))
            current_result = draft_results.get(game_key, "未入力")
            expected_symbol = _result_symbol_for_team(
                current_result,
                team,
                home,
            )
            candidate = (_game_result_from_symbol(symbol, team, home), team)
            if symbol != "未":
                candidates.append(candidate)
            if symbol != expected_symbol:
                changed_candidates.append(candidate)
        if changed_candidates:
            changed_result = changed_candidates[0][0]
            if changed_result == "未入力":
                draft_results.pop(game_key, None)
            else:
                draft_results[game_key] = changed_result
            continue
        if not candidates:
            draft_results.pop(game_key, None)
            continue
        draft_results[game_key] = draft_results.get(game_key, candidates[0][0])
    st.session_state[draft_state_key] = draft_results


def _render_magic_scenario_result_applied(
    scenario: MagicScenarioAnalysis,
    draft_state_key: str,
    applied_state_key: str,
    scenario_state_key: str,
    revision_key: str,
    year: int,
    league: str,
    target_team: str,
) -> None:
    st.markdown("<div class='magic-summary-marker'></div>", unsafe_allow_html=True)
    st.markdown("<div class='magic-summary-title'>判定サマリー</div>", unsafe_allow_html=True)
    metric_cols = st.columns(1)
    metric_cols[0].metric("優勝確認", "優勝" if scenario.is_clinched else "未確定")
    metric_cols[0].metric("マジック点灯確認", "点灯" if scenario.is_lit else "未点灯")
    magic_number = scenario.magic_number
    magic_display = "—" if magic_number is None or not scenario.is_lit else f"M{magic_number}"
    metric_cols[0].metric("マジック数", magic_display)
    if st.button(
        "結果入力をリセット",
        key=f"magic_game_reset_button_{year}_{league}_{target_team}",
        use_container_width=True,
    ):
        st.session_state[draft_state_key] = {}
        st.session_state[applied_state_key] = {}
        st.session_state[scenario_state_key] = None
        st.session_state[revision_key] += 1
        st.rerun()


def _format_magic_team_status_table(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["相手球団", "マジック状況", "優勝状況", "必要勝利数"]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    formatted = frame.copy()
    formatted["相手球団"] = formatted["Team"].map(
        lambda team: (
            f"<span style='color:{TEAM_MATRIX_HEADER_COLORS.get(str(team), '#172033')};"
            f"font-weight:900'>{team_label(str(team))}</span>"
        )
    )
    formatted["マジック状況"] = formatted["IsLit"].map(
        lambda value: "点灯条件クリア" if bool(value) else "未点灯"
    )
    formatted["優勝状況"] = formatted["IsClinched"].map(
        lambda value: "優勝条件クリア" if bool(value) else "未達"
    )
    formatted["必要勝利数"] = formatted["NeededWins"].map(_needed_wins_display)
    return formatted[columns]


def _champion_date_chart(
    result: SimulationResult,
    target_team: str,
    team_name: str,
    dark_mode: bool,
):
    frame = result.champion_dates.copy()
    if frame.empty:
        return px.bar(title=f"{team_name}の優勝確定日は記録されませんでした")
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.normalize()
    if "DateLabel" not in frame.columns:
        frame["DateLabel"] = ""
    frame["DateLabel"] = frame["DateLabel"].fillna("").astype(str).str.strip()
    frame = _collapse_champion_date_rows(frame)
    frame = frame.sort_values("Date").reset_index(drop=True)
    frame = _insert_regular_calendar_dates(frame)
    frame["Probability"] = frame["Probability"].fillna(0.0)
    frame["RawDateLabel"] = frame["DateLabel"].fillna("").astype(str).str.strip()
    frame["MakeupDays"] = frame["RawDateLabel"].map(_makeup_label_days)
    frame["IsMakeupLabel"] = frame["RawDateLabel"].map(_is_makeup_label)
    frame["IsMultiMakeup"] = frame["IsMakeupLabel"] & (frame["MakeupDays"] >= 2)
    frame["DateLabel"] = frame.apply(_chart_date_label, axis=1)
    frame["ProbabilityPct"] = frame["Probability"] * 100
    frame["ProbabilityLabel"] = frame["ProbabilityPct"].map(
        lambda value: "無し" if float(value) <= 0 else f"{float(value):.1f}%"
    )
    category_order = frame["DateLabel"].tolist()
    positive_frame = frame[frame["ProbabilityPct"] > 0]
    top_labels = set(
        positive_frame[~positive_frame["IsMultiMakeup"]]
        .nlargest(3, "ProbabilityPct")["DateLabel"]
    )
    top_color = TEAM_ACCENT_COLORS.get(target_team, "#2563eb")
    gray_colors = _gray_gradient_colors(frame["ProbabilityPct"], dark_mode)
    frame["BarColor"] = [
        top_color if date_label in top_labels else gray_color
        for date_label, gray_color in zip(frame["DateLabel"], gray_colors)
    ]
    y_max = max(0.5, float(frame["ProbabilityPct"].max()) * 1.24)
    label_frame = (
        frame[frame["DateLabel"].isin(top_labels)]
        .sort_values("Date")
        .reset_index(drop=True)
    )
    label_offsets = [(-14, 9), (0, 14), (14, 9), (0, 22)]

    fig = go.Figure(
        data=[
            go.Bar(
                x=frame["DateLabel"],
                y=frame["ProbabilityPct"],
                customdata=frame["ProbabilityLabel"],
                marker_color=frame["BarColor"],
                marker_line_color="#ffffff" if not dark_mode else "#0f172a",
                marker_line_width=0.8,
                opacity=0.96,
                hovertemplate="%{x}<br>%{customdata}<extra></extra>",
            )
        ]
    )
    fig.update_layout(
        title=f"{team_name} 優勝確定日分布",
        height=440,
        margin={"l": 10, "r": 10, "t": 60, "b": 10},
        clickmode="event+select",
        showlegend=False,
        bargap=0.22,
        plot_bgcolor="#182338" if dark_mode else "#ffffff",
        paper_bgcolor="#182338" if dark_mode else "#ffffff",
        font={"color": "#f8fafc" if dark_mode else "#172033"},
        xaxis={
            "gridcolor": "#334155" if dark_mode else "#e5eaf0",
            "categoryorder": "array",
            "categoryarray": category_order,
            "tickmode": "array",
            "tickvals": category_order,
            "ticktext": category_order,
            "tickangle": -35,
            "automargin": True,
            "title": "日付",
        },
        yaxis={
            "gridcolor": "#334155" if dark_mode else "#e5eaf0",
            "range": [0, y_max],
            "title": "確率 (%)",
            "zeroline": True,
            "zerolinecolor": "#cbd5e1" if not dark_mode else "#475569",
        },
    )
    for index, row in enumerate(label_frame.itertuples(index=False)):
        xshift, yshift = label_offsets[index % len(label_offsets)]
        probability_pct = float(row.ProbabilityPct)
        fig.add_annotation(
            x=row.DateLabel,
            y=probability_pct + max(0.25, y_max * 0.015),
            text=f"<b>{probability_pct:.1f}%</b>",
            showarrow=False,
            xanchor="center",
            yanchor="bottom",
            xshift=xshift,
            yshift=yshift,
            font={
                "size": 12,
                "color": top_color if dark_mode else "#1d4ed8",
                "family": "Noto Sans JP, Yu Gothic, sans-serif",
            },
        )
    return fig


def _plotly_chart_selection_signature(event: object) -> tuple[str, ...]:
    if event is None:
        return ()
    selection = getattr(event, "selection", None)
    if selection is None:
        return ()
    points = getattr(selection, "points", None)
    if not points:
        return ()
    return tuple(sorted(str(point) for point in points))


def _close_champion_date_chart_dialog() -> None:
    st.session_state[CHART_DIALOG_STATE_KEY] = False


@st.dialog(
    " ",
    width="large",
    on_dismiss=_close_champion_date_chart_dialog,
)
def _show_champion_date_chart_dialog(figure: object, chart_key: str) -> None:
    if hasattr(figure, "update_layout"):
        figure.update_layout(height=720)
    st.plotly_chart(
        figure,
        use_container_width=True,
        key=chart_key,
        config={
            "displayModeBar": False,
            "scrollZoom": False,
            "responsive": True,
        },
    )
    if st.button("閉じる", key=f"{chart_key}_close"):
        st.session_state[CHART_DIALOG_STATE_KEY] = False
        st.rerun()


def _collapse_champion_date_rows(frame: pd.DataFrame) -> pd.DataFrame:
    regular = frame[frame["DateLabel"] == ""]
    labeled = frame[frame["DateLabel"] != ""]
    frames: list[pd.DataFrame] = []
    if not regular.empty:
        frames.append(
            regular.groupby(["Date", "DateLabel"], as_index=False)["Probability"].sum()
        )
    if not labeled.empty:
        frames.append(
            labeled.groupby("DateLabel", as_index=False)
            .agg(Date=("Date", "max"), Probability=("Probability", "sum"))
            .loc[:, ["Date", "DateLabel", "Probability"]]
        )
    if not frames:
        return pd.DataFrame(columns=["Date", "DateLabel", "Probability"])
    return pd.concat(frames, ignore_index=True).sort_values("Date")


def _insert_regular_calendar_dates(frame: pd.DataFrame) -> pd.DataFrame:
    regular = frame[frame["DateLabel"] == ""].copy()
    labeled = frame[frame["DateLabel"] != ""].copy()
    frames: list[pd.DataFrame] = []

    if not regular.empty:
        calendar = pd.DataFrame(
            {"Date": pd.date_range(regular["Date"].min(), regular["Date"].max(), freq="D")}
        )
        frames.append(calendar.merge(regular, on="Date", how="left"))
    if not labeled.empty:
        frames.append(labeled.sort_values("Date"))
    if not frames:
        return frame
    return pd.concat(frames, ignore_index=True)


def _makeup_label_days(label: object) -> int:
    text = str(label or "")
    if not _is_makeup_label(text):
        return 0
    match = re.search(r"(?:残)?(\d+)(?:日|試合)", text)
    if match:
        return int(match.group(1))
    return 1


def _is_makeup_label(label: object) -> bool:
    text = str(label or "")
    return text.startswith(("振替日", "自軍振替日", "他軍振替日"))


def _gray_gradient_colors(values: pd.Series, dark_mode: bool) -> list[str]:
    start = "#d8dee7" if not dark_mode else "#3a4657"
    end = "#687386" if not dark_mode else "#9aa8bb"
    max_value = float(values.max()) if not values.empty else 0.0
    if max_value <= 0:
        return [start for _ in values]
    return [
        _mix_color(start, end, min(float(value) / max_value, 1.0) ** 0.55)
        for value in values
    ]


def _mix_color(start_hex: str, end_hex: str, ratio: float) -> str:
    start_rgb = _hex_to_rgb(start_hex)
    end_rgb = _hex_to_rgb(end_hex)
    rgb = tuple(
        round(start + (end - start) * ratio)
        for start, end in zip(start_rgb, end_rgb)
    )
    return _rgb_to_hex(rgb)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{component:02x}" for component in rgb)


def _format_standings(standings: pd.DataFrame, league: str) -> pd.DataFrame:
    order = {team: index for index, team in enumerate(league_teams(league))}
    frame = standings.copy()
    frame["球団"] = frame["Team"].map(team_label)
    frame["勝率"] = frame["WinRate"].map(lambda value: f"{value:.3f}".lstrip("0"))
    frame["表示順"] = frame["Team"].map(order)
    frame = frame.sort_values(["Wins", "Losses", "表示順"], ascending=[False, True, True])
    return frame[["球団", "Games", "Wins", "Losses", "Ties", "勝率"]].rename(
        columns={
            "Games": "試合",
            "Wins": "勝",
            "Losses": "敗",
            "Ties": "分",
        }
    )


def _format_assumed_rates(assumed_win_rates: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"球団": team_label(team), "今後の想定勝率": f"{rate:.3f}".lstrip("0")}
            for team, rate in assumed_win_rates.items()
        ]
    )


def _format_final_standings(final_standings: pd.DataFrame) -> pd.DataFrame:
    if final_standings.empty:
        return pd.DataFrame(columns=["球団", "勝", "敗", "分"])
    frame = final_standings.copy()
    frame["球団"] = frame["Team"].map(team_label)
    for column in ["Wins", "Losses", "Ties"]:
        frame[column] = frame[column].map(lambda value: f"{value:.1f}")
    return frame[["球団", "Wins", "Losses", "Ties"]].rename(
        columns={"Wins": "勝", "Losses": "敗", "Ties": "分"}
    )


def _format_final_standings_with_advantage(
    final_standings: pd.DataFrame,
    advantage_probabilities: pd.DataFrame | None,
) -> pd.DataFrame:
    formatted = _format_final_standings(final_standings)
    if formatted.empty or advantage_probabilities is None:
        return formatted

    probability_map = dict(
        zip(
            advantage_probabilities["Team"].astype(str),
            advantage_probabilities["Probability"].astype(float),
        )
    )
    formatted["2勝アドバンテージ付与確率"] = (
        final_standings["Team"]
        .astype(str)
        .map(probability_map)
        .fillna(0.0)
        .map(lambda value: f"{value:.1%}")
        .to_numpy()
    )
    return formatted


def _format_final_standings_table(final_standings: pd.DataFrame) -> pd.DataFrame:
    columns = ["球団", "勝", "敗", "分", "ゲーム差"]
    if final_standings.empty:
        return pd.DataFrame(columns=columns)

    frame = final_standings.copy()
    frame["TeamLabel"] = frame["Team"].map(team_label)
    for column in ["Wins", "Losses", "Ties"]:
        frame[column] = frame[column].map(lambda value: f"{value:.1f}")
    frame["GamesBehind"] = frame["GamesBehind"].map(_games_behind_display)
    frame.loc[frame.index[0], "GamesBehind"] = "-"
    return frame[["TeamLabel", "Wins", "Losses", "Ties", "GamesBehind"]].rename(
        columns={
            "TeamLabel": "球団",
            "Wins": "勝",
            "Losses": "敗",
            "Ties": "分",
            "GamesBehind": "ゲーム差",
        }
    )


def _games_behind_display(value: float) -> str:
    value = float(value)
    text = f"{value:.1f}"
    if value >= 10.0:
        return f"<span class='games-behind-alert'>{text}</span>"
    return text


def _format_magic_clinch_table(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["相手", "自軍残", "相手残", "直接残", "必要勝利", "到達勝率", "相手最高勝率"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    formatted = frame.copy()
    formatted["相手"] = formatted["Team"].map(team_label)
    formatted["自軍残"] = formatted["TargetRemaining"].astype(int)
    formatted["相手残"] = formatted["RivalRemaining"].astype(int)
    formatted["直接残"] = formatted["DirectRemaining"].astype(int)
    formatted["必要勝利"] = formatted["NeededWins"].map(_needed_wins_display)
    formatted["到達勝率"] = formatted["TargetRate"].map(_rate_display)
    formatted["相手最高勝率"] = formatted["RivalMaxRate"].map(_rate_display)
    return formatted[columns]


def _format_magic_lighting_table(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["相手", "直接残", "自軍想定勝率", "相手最高勝率", "判定"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    formatted = frame.copy()
    formatted["相手"] = formatted["Team"].map(team_label)
    formatted["直接残"] = formatted["DirectRemaining"].astype(int)
    formatted["自軍想定勝率"] = formatted["TargetScenarioRate"].map(_rate_display)
    formatted["相手最高勝率"] = formatted["RivalMaxRate"].map(_rate_display)
    formatted["判定"] = formatted["IsLit"].map(lambda value: "点灯" if bool(value) else "未点灯")
    return formatted[columns]


def _format_magic_scenario_lighting_table(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["相手", "直接残", "点灯時の自軍勝率", "相手最高勝率", "判定"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    formatted = frame.copy()
    formatted["相手"] = formatted["Team"].map(team_label)
    formatted["直接残"] = formatted["DirectRemaining"].astype(int)
    formatted["点灯時の自軍勝率"] = formatted["TargetScenarioRate"].map(_rate_display)
    formatted["相手最高勝率"] = formatted["RivalMaxRate"].map(_rate_display)
    formatted["判定"] = formatted["IsLit"].map(lambda value: "点灯条件クリア" if bool(value) else "未達")
    return formatted[columns]


def _format_magic_scenario_standings(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["球団", "勝", "敗", "分", "勝率"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    formatted = frame.copy()
    formatted["球団"] = formatted["Team"].map(team_label)
    formatted["勝率"] = formatted.apply(
        lambda row: _rate_display(_win_rate(row["Wins"], row["Losses"])),
        axis=1,
    )
    formatted = formatted.sort_values(
        ["Wins", "Losses"],
        ascending=[False, True],
    )
    return formatted[["球団", "Wins", "Losses", "Ties", "勝率"]].rename(
        columns={"Wins": "勝", "Losses": "敗", "Ties": "分"}
    )


def _format_magic_scenario_clinch_table(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["相手", "対象最低勝率", "相手最高勝率", "必要勝利", "判定"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    formatted = frame.copy()
    formatted["相手"] = formatted["Team"].map(team_label)
    formatted["対象最低勝率"] = formatted["TargetMinRate"].map(_rate_display)
    formatted["相手最高勝率"] = formatted["RivalMaxRateForClinch"].map(_rate_display)
    formatted["必要勝利"] = formatted["NeededWins"].map(_needed_wins_display)
    formatted["判定"] = formatted["IsClinched"].map(lambda value: "優勝条件クリア" if bool(value) else "未達")
    return formatted[columns]


def _format_magic_timeline(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["日付", "入力済み", "対象球団の勝敗", "現在勝率", "残り試合", "マジック", "優勝"]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    formatted = frame.copy()
    formatted["日付"] = formatted.apply(_result_date_label, axis=1)
    formatted["入力済み"] = formatted["EnteredGames"].astype(int)
    formatted["対象球団の勝敗"] = formatted.apply(
        lambda row: f"{int(row['TargetWins'])}-{int(row['TargetLosses'])}-{int(row['TargetTies'])}",
        axis=1,
    )
    formatted["現在勝率"] = formatted["TargetRate"].map(_rate_display)
    formatted["残り試合"] = formatted["RemainingGames"].astype(int)
    formatted["マジック"] = formatted["IsLit"].map(lambda value: "点灯" if bool(value) else "未点灯")
    formatted["優勝"] = formatted["IsClinched"].map(lambda value: "優勝" if bool(value) else "未確定")
    return formatted[columns]


def _needed_wins_display(value: object) -> str:
    if pd.isna(value):
        return "不可"
    wins = int(value)
    return "確定済み" if wins == 0 else f"+{wins}勝"


def _render_schedule_calendar(
    champion_dates: pd.DataFrame,
    schedule: pd.DataFrame,
    target_team: str,
    year: int,
    league: str,
    start_date: date,
) -> None:
    """Render a month calendar combining champion-date probability and fixtures."""
    start_timestamp = pd.Timestamp(start_date).normalize()
    probability_map: dict[pd.Timestamp, float] = {}
    if not champion_dates.empty and "Date" in champion_dates.columns:
        probability_frame = champion_dates.copy()
        probability_frame["Date"] = pd.to_datetime(
            probability_frame["Date"], errors="coerce"
        ).dt.normalize()
        probability_frame["Probability"] = pd.to_numeric(
            probability_frame["Probability"], errors="coerce"
        ).fillna(0.0)
        probability_frame = probability_frame.dropna(subset=["Date"])
        probability_frame = probability_frame[
            probability_frame["Date"] >= start_timestamp
        ]
        probability_map = {
            pd.Timestamp(row.Date): float(row.Probability)
            for row in probability_frame.groupby("Date", as_index=False)["Probability"]
            .sum()
            .itertuples(index=False)
        }

    fixture_map: dict[pd.Timestamp, list[tuple[str, str]]] = {}
    date_values: list[pd.Timestamp] = [start_timestamp, *probability_map]
    if not schedule.empty and "Date" in schedule.columns:
        fixture_frame = schedule.copy()
        fixture_frame["Date"] = pd.to_datetime(
            fixture_frame["Date"], errors="coerce"
        ).dt.normalize()
        fixture_frame = fixture_frame.dropna(subset=["Date"])
        fixture_frame = fixture_frame[
            (fixture_frame["HomeTeam"] == target_team)
            | (fixture_frame["AwayTeam"] == target_team)
        ]
        for game_date, group in fixture_frame.groupby("Date", sort=True):
            opponents: list[tuple[str, str]] = []
            for row in group.itertuples(index=False):
                opponent = row.AwayTeam if row.HomeTeam == target_team else row.HomeTeam
                opponent_label = _schedule_team_label(opponent)
                venue_value = getattr(row, "Venue", "")
                if pd.isna(venue_value):
                    venue_value = ""
                venue_label = str(venue_value).strip()
                if venue_label.lower() in {"nan", "<na>"}:
                    venue_label = ""
                if bool(getattr(row, "IsMakeup", False)):
                    opponent_label = f"振替: {opponent_label}"
                opponent_item = (opponent_label, venue_label)
                if opponent_item not in opponents:
                    opponents.append(opponent_item)
            fixture_map[pd.Timestamp(game_date)] = opponents
        date_values.extend(fixture_map)

    if not date_values:
        st.info("表示できる優勝確率・残り日程がありません。")
        return

    minimum_date = min(date_values)
    maximum_date = max(date_values)
    minimum_month = minimum_date.to_period("M")
    maximum_month = maximum_date.to_period("M")
    month_key = (
        f"schedule_calendar_month_{year}_{league}_{target_team}_"
        f"{start_date.isoformat()}"
    )
    calendar_context_key = (
        f"schedule_calendar_context_{year}_{league}_{target_team}"
    )
    default_month = pd.Timestamp(start_date).to_period("M")
    if default_month < minimum_month:
        default_month = minimum_month
    if default_month > maximum_month:
        default_month = maximum_month
    if st.session_state.get(calendar_context_key) != start_date.isoformat():
        st.session_state[month_key] = str(default_month)
        st.session_state[calendar_context_key] = start_date.isoformat()
    try:
        current_month = pd.Period(
            st.session_state.get(month_key, str(default_month)), freq="M"
        )
    except (TypeError, ValueError):
        current_month = default_month
    current_month = min(max(current_month, minimum_month), maximum_month)

    navigation = st.columns([1, 4, 1])
    with navigation[0]:
        if st.button(
            "‹",
            key=f"schedule_calendar_prev_{year}_{league}_{target_team}",
            disabled=current_month <= minimum_month,
            use_container_width=True,
        ):
            current_month -= 1
            st.session_state[month_key] = str(current_month)
            st.rerun()
    with navigation[1]:
        st.markdown(
            f"<div class='schedule-calendar-title'>{current_month.year}年{current_month.month}月</div>",
            unsafe_allow_html=True,
        )
    with navigation[2]:
        if st.button(
            "›",
            key=f"schedule_calendar_next_{year}_{league}_{target_team}",
            disabled=current_month >= maximum_month,
            use_container_width=True,
        ):
            current_month += 1
            st.session_state[month_key] = str(current_month)
            st.rerun()
    st.session_state[month_key] = str(current_month)

    month_start = current_month.start_time.normalize()
    month_end = current_month.end_time.normalize()
    day_values = pd.date_range(month_start, month_end, freq="D")
    cells: list[str] = [
        "<div class='schedule-calendar-cell schedule-calendar-empty'></div>"
    ] * int(month_start.weekday())
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    weekday_html = "".join(
        f"<div class='schedule-calendar-weekday weekday-{index}'>{name}</div>"
        for index, name in enumerate(weekday_names)
    )
    max_probability = max(probability_map.values(), default=0.0)
    target_color = TEAM_ACCENT_COLORS.get(target_team, "#2f6f8f")

    for day in day_values:
        day = pd.Timestamp(day).normalize()
        probability = float(probability_map.get(day, 0.0))
        opponents = fixture_map.get(day, [])
        has_content = bool(opponents) or probability > 0
        if not has_content:
            cells.append(
                f"<div class='schedule-calendar-cell schedule-calendar-day-empty'>"
                f"<span class='schedule-calendar-day-number'>{day.day}</span></div>"
            )
            continue
        intensity = 0
        if max_probability > 0 and probability > 0:
            intensity = min(4, max(1, int(round(probability / max_probability * 4))))
        opponent_html = "".join(
            "<span class='schedule-calendar-opponent'>"
            f"<span class='schedule-calendar-team'>{escape(opponent)}</span>"
            + (
                f"<span class='schedule-calendar-venue'>{escape(venue)}</span>"
                if venue
                else ""
            )
            + "</span>"
            for opponent, venue in opponents
        )
        probability_text = "0%" if probability <= 0 else f"{probability * 100:.1f}%"
        cells.append(
            f"<div class='schedule-calendar-cell schedule-calendar-event probability-{intensity}' "
            f"style='--calendar-accent:{target_color};'>"
            f"<span class='schedule-calendar-day-number'>{day.day}</span>"
            f"{opponent_html}"
            f"<span class='schedule-calendar-probability'>{probability_text}</span>"
            "</div>"
        )
    while len(cells) % 7:
        cells.append("<div class='schedule-calendar-cell schedule-calendar-empty'></div>")

    total_probability = sum(probability_map.values()) * 100
    st.markdown(
        "<div class='schedule-calendar-card'>"
        f"<div class='schedule-calendar-heading'><strong>{escape(team_label(target_team))}</strong>"
        f"<span>優勝確定日確率（合計） <b>{total_probability:.1f}%</b></span></div>"
        f"<div class='schedule-calendar-weekdays'>{weekday_html}</div>"
        f"<div class='schedule-calendar-grid'>{''.join(cells)}</div>"
        "<div class='schedule-calendar-caption'>対戦相手は対象球団の残り日程、確率はシミュレーション結果です。</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def _format_schedule(schedule: pd.DataFrame, target_team: str) -> pd.DataFrame:
    columns = ["日付", "カード", "球場", "開始"]
    if schedule.empty or "Date" not in schedule.columns:
        return pd.DataFrame(columns=columns)
    frame = schedule.copy()
    frame = frame[(frame["HomeTeam"] == target_team) | (frame["AwayTeam"] == target_team)]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    target_makeup_count = _makeup_game_count(frame)
    frame["日付"] = frame.apply(
        lambda row: _schedule_date_label(row, target_makeup_count),
        axis=1,
    )
    frame = frame.dropna(subset=["日付"])
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame["カード"] = frame.apply(
        lambda row: f"{_schedule_team_label(row.HomeTeam)} - {_schedule_team_label(row.AwayTeam)}",
        axis=1,
    )
    return frame[["日付", "カード", "Venue", "StartTime"]].rename(
        columns={"Venue": "球場", "StartTime": "開始"}
    )


def _makeup_summary(schedule: pd.DataFrame) -> str:
    if schedule.empty or "IsMakeup" not in schedule.columns:
        return ""
    frame = schedule[schedule["IsMakeup"].fillna(False).astype(bool)].copy()
    if frame.empty:
        return ""
    frame["カード"] = frame.apply(
        lambda row: f"{_schedule_team_label(row.HomeTeam)} - {_schedule_team_label(row.AwayTeam)}",
        axis=1,
    )
    parts = [
        f"{card} ×{count}"
        for card, count in frame.groupby("カード", sort=False).size().items()
    ]
    lines = "<br>".join(parts)
    return f"<div class='makeup-note'>未確定の振替試合：<br>{lines}</div>"


def _top_dates(champion_dates: pd.DataFrame) -> pd.DataFrame:
    if champion_dates.empty:
        return pd.DataFrame(columns=["日付", "確率"])
    frame = champion_dates.sort_values("Probability", ascending=False).head(10).copy()
    frame["日付"] = frame.apply(_result_date_label, axis=1)
    frame = frame.dropna(subset=["日付"])
    frame["確率"] = frame["Probability"].map(lambda value: f"{value * 100:.1f}%")
    return frame[["日付", "確率"]]


def _first_column(frame: pd.DataFrame, column: str) -> pd.Series:
    values = frame[column]
    if isinstance(values, pd.DataFrame):
        return values.iloc[:, 0]
    return values


def _date_label(value: object) -> str | pd.NA:
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return pd.NA
    return timestamp.strftime("%Y-%m-%d")


def _chart_date_label(row: pd.Series) -> str:
    label = _optional_label(row.get("DateLabel", ""))
    if label:
        return label
    timestamp = pd.to_datetime(row.get("Date"), errors="coerce")
    if pd.isna(timestamp):
        return ""
    return f"{timestamp.month}/{timestamp.day}"


def _result_date_label(row: pd.Series) -> str | pd.NA:
    label = _optional_label(row.get("DateLabel", ""))
    if label:
        return label
    return _date_label(row.get("Date"))


def _schedule_date_label(row: pd.Series, target_makeup_count: int | None = None) -> str | pd.NA:
    is_makeup = bool(row.get("IsMakeup", False))
    label = _optional_label(row.get("DateLabel", ""))
    if is_makeup and label:
        if target_makeup_count is not None:
            return f"自軍振替日（{target_makeup_count}試合）"
        return label
    return _date_label(row.get("Date"))


def _schedule_team_label(code: object) -> str:
    try:
        return team_label(str(code))
    except KeyError:
        return "未定" if str(code) == "TBD" else str(code)


def _optional_label(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _remaining_games(schedule: pd.DataFrame, team: str) -> int:
    if schedule.empty:
        return 0
    return int(((schedule["HomeTeam"] == team) | (schedule["AwayTeam"] == team)).sum())


def _remaining_game_dates(schedule: pd.DataFrame, team: str) -> int:
    if schedule.empty:
        return 0
    frame = schedule[(schedule["HomeTeam"] == team) | (schedule["AwayTeam"] == team)]
    return int(frame["Date"].nunique())


def _makeup_game_count(schedule: pd.DataFrame) -> int:
    if schedule.empty or "IsMakeup" not in schedule.columns:
        return 0
    return int(schedule["IsMakeup"].fillna(False).astype(bool).sum())


def _win_rate(wins: int | float, losses: int | float) -> float:
    games = int(wins) + int(losses)
    return int(wins) / games if games else 0.5


def _rate_display(rate: float) -> str:
    return f"{float(rate):.3f}".lstrip("0")


def _parse_rate(value: object) -> float:
    text = str(value).strip()
    if text.startswith("."):
        text = "0" + text
    return float(text)


def _apply_style(dark_mode: bool) -> None:
    if dark_mode:
        page_bg = "#0b1320"
        surface = "#172235"
        surface_soft = "#1d2a3d"
        border = "#314158"
        text = "#f8fafc"
        muted = "#94a3b8"
        info_bg = "#173b63"
        input_bg = "#111827"
        button_bg = "#111827"
        primary = "#3b82f6"
        shadow = "0 18px 44px rgba(0, 0, 0, 0.34)"
    else:
        page_bg = "#f6f8fb"
        surface = "#ffffff"
        surface_soft = "#f2f5f8"
        border = "#d8dee6"
        text = "#172033"
        muted = "#5f6b7a"
        info_bg = "#e8f2ff"
        input_bg = "#eef1f5"
        button_bg = "#ffffff"
        primary = "#2f6f8f"
        shadow = "0 12px 28px rgba(32, 50, 70, 0.10)"
    matrix_border = "#111827" if not dark_mode else "#94a3b8"

    st.markdown(
        f"""
<style>
.stApp {{
  background:
    radial-gradient(circle at 100% 0%, rgba(249, 115, 22, 0.10), transparent 28%),
    linear-gradient(135deg, {page_bg}, {"#111827" if dark_mode else "#eef3f8"});
  color: {text};
}}
.block-container {{
  padding-top: 2.4rem;
  max-width: 1500px;
}}
.app-title {{
  margin: 0 0 0.35rem;
  padding-top: 0.35rem;
  font-size: 2.45rem;
  line-height: 1.14;
  font-weight: 900;
  color: {text};
  letter-spacing: 0;
}}
.app-caption {{
  margin-bottom: 1.75rem;
  color: {muted};
  font-size: 0.95rem;
  font-weight: 600;
}}
.mode-control-label {{
  margin-top: 0.85rem;
  margin-bottom: 0.15rem;
  text-align: right;
  color: {muted};
  font-size: 0.78rem;
  font-weight: 800;
}}
div[data-testid="stToggle"] {{
  display: flex;
  justify-content: flex-end;
  min-height: 34px;
}}
div[data-testid="stExpander"] {{
  border: 1px solid {border};
  border-radius: 8px;
  background: {surface};
  box-shadow: {shadow};
}}
div[data-testid="stExpander"] details summary {{
  background: {surface_soft};
  border-radius: 8px 8px 0 0;
}}
div[data-testid="stExpander"] details summary p {{
  font-size: 19px;
  font-weight: 900;
  color: {text};
}}
section[data-testid="stSidebar"] div[data-testid="stExpander"] {{
  box-shadow: none;
  margin-top: 0.2rem;
  margin-bottom: 0.55rem;
}}
section[data-testid="stSidebar"] div[data-testid="stExpander"] details summary {{
  min-height: 34px;
  padding: 0.12rem 0.35rem;
}}
section[data-testid="stSidebar"] div[data-testid="stExpander"] details summary p {{
  font-size: 13px;
  font-weight: 800;
}}
section[data-testid="stSidebar"] div[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {{
  padding-top: 0.35rem;
}}
div[data-testid="stAlert"] {{
  background: {info_bg};
  border-radius: 7px;
}}
div[data-testid="stAlert"] > div {{
  padding: 0.48rem 0.72rem;
}}
div[data-testid="stAlert"] p {{
  font-size: 0.88rem;
  line-height: 1.35;
  font-weight: 700;
}}
.scenario-caption {{
  display: block;
  min-height: 22px;
  margin: 0.15rem 0 0.65rem;
  padding: 0;
  overflow: hidden;
  color: {muted};
  font-size: 0.78rem;
  line-height: 22px;
  font-weight: 700;
  white-space: nowrap;
  text-overflow: ellipsis;
}}
.scenario-note {{
  display: inline-flex;
  align-items: center;
  width: fit-content;
  max-width: 100%;
  min-height: 30px;
  padding: 0 0.68rem;
  border-radius: 7px;
  background: {info_bg};
  color: {primary};
  font-size: 0.84rem;
  line-height: 1.25;
  font-weight: 800;
  white-space: nowrap;
}}
.magic-matrix-header {{
  position: sticky;
  top: 0;
  z-index: 1002;
  min-height: 40px;
  height: 40px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0.16rem 0.12rem;
  border: 1px solid {matrix_border};
  background: {surface_soft};
  color: {text};
  text-align: center;
  font-size: 0.84rem;
  line-height: 1.15;
  font-weight: 900;
}}
.magic-matrix-team-header {{
  font-size: 1rem;
  letter-spacing: 0.04em;
  text-shadow: 0 1px 0 rgba(255, 255, 255, 0.42);
}}
.magic-matrix-result-header {{
  font-size: 0.72rem;
}}
.magic-matrix-help {{
  margin: 0 0 0.2rem;
  color: {muted};
  font-size: 0.7rem;
  line-height: 1.35;
  font-weight: 700;
}}
.magic-summary-title {{
  margin: 0 0 0.35rem;
  color: {text};
  font-size: 0.94rem;
  font-weight: 900;
}}
.magic-summary-cards {{
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  width: 100%;
  max-width: 220px;
}}
.magic-summary-card {{
  box-sizing: border-box;
  width: 100%;
  min-height: 66px;
  padding: 0.55rem 0.62rem;
  border: 1px solid {border};
  border-radius: 7px;
  background: {surface};
  box-shadow: {shadow};
}}
.magic-summary-card-label {{
  color: {muted};
  font-size: 0.72rem;
  line-height: 1.2;
  font-weight: 800;
}}
.magic-summary-value {{
  margin-top: 0.22rem;
  color: {text};
  font-size: 1.16rem;
  line-height: 1.1;
  font-weight: 900;
}}
.magic-summary-value-alert,
.magic-summary-value-champion {{
  color: #d71920;
}}
.magic-summary-value-champion {{
  font-size: 1.22rem;
  letter-spacing: 0.03em;
}}
.magic-summary-marker + div[data-testid="stVerticalBlock"] {{
  max-width: 220px;
}}
.magic-summary-marker ~ div[data-testid="stMetric"] {{
  max-width: 220px;
}}
div[data-testid="stVerticalBlock"]:has(.magic-summary-marker) div[data-testid="stMetric"] {{
  margin-bottom: 0.35rem;
  padding: 6px 8px;
  border-radius: 6px;
  box-shadow: none;
}}
div[data-testid="stVerticalBlock"]:has(.magic-summary-marker) div[data-testid="stMetricLabel"] {{
  font-size: 0.72rem;
}}
div[data-testid="stVerticalBlock"]:has(.magic-summary-marker) div[data-testid="stMetricValue"] {{
  font-size: 1.12rem;
}}
div[data-testid="stVerticalBlock"]:has(.magic-summary-marker) button {{
  min-height: 28px;
  padding: 0.15rem 0.35rem;
  font-size: 0.78rem;
}}
.magic-team-status-title {{
  margin: 1.25rem 0 0.35rem;
  color: {text};
  font-size: 1.08rem;
  font-weight: 900;
}}
.magic-matrix-marker + div[data-testid="stVerticalBlock"] {{
  min-width: 900px;
}}
.magic-matrix-marker ~ div[data-testid="stHorizontalBlock"] {{
  min-width: 900px;
}}
.magic-matrix-date {{
  min-height: 28px;
  padding: 0.22rem 0.12rem;
  border: 1px solid {matrix_border};
  color: {text};
  font-size: 0.76rem;
  line-height: 1.15;
  font-weight: 800;
  text-align: center;
}}
.magic-matrix-opponent {{
  min-height: 28px;
  padding: 0.2rem 0.1rem 0;
  border: 1px solid {matrix_border};
  color: {text};
  font-size: 0.9rem;
  line-height: 1.15;
  font-weight: 800;
  text-align: center;
}}
.magic-matrix-empty {{
  min-height: 28px;
  padding: 0.2rem 0.1rem;
  border: 1px solid {matrix_border};
  color: {muted};
  text-align: center;
}}
.magic-matrix-result-readonly {{
  min-height: 46px;
  height: 46px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid {matrix_border};
  color: {text};
  font-size: 0.92rem;
  font-weight: 900;
  text-align: center;
}}
.magic-matrix-result-disabled {{
  min-height: 46px;
  height: 46px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid {matrix_border};
  background: {surface_soft};
  color: {muted};
  font-size: 0.92rem;
  font-weight: 900;
  text-align: center;
  opacity: 0.72;
}}
div[data-testid="stSelectbox"] {{
  margin-bottom: 0.25rem;
}}
div[data-testid="stSelectbox"] > div {{
  min-height: 28px;
}}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div {{
  min-height: 28px;
  padding: 0 0.2rem;
  border-color: {border};
  background: {input_bg};
  color: {text};
  font-size: 0.76rem;
  font-weight: 800;
  text-align: center;
}}
div[data-testid="stVerticalBlock"]:has(.magic-matrix-marker) {{
  overflow: visible;
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-header),
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) {{
  column-gap: 0 !important;
  align-items: stretch;
  width: 100% !important;
  max-width: none !important;
  min-width: 0 !important;
  margin-left: 0 !important;
  margin-right: 0 !important;
  padding-left: 0 !important;
  padding-right: 0 !important;
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-header) {{
  box-sizing: border-box;
  position: sticky !important;
  top: 0 !important;
  z-index: 1000 !important;
  isolation: isolate;
  min-height: 40px !important;
  padding-left: 17px !important;
  padding-right: 26px !important;
  background: {surface} !important;
  box-shadow: 0 1px 0 {matrix_border};
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-header) div[data-testid="stHorizontalBlock"]:has(.magic-matrix-header) {{
  position: sticky !important;
  top: 0 !important;
  z-index: 1000 !important;
  width: 100% !important;
  min-height: 40px !important;
  background: {surface} !important;
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-header) > div[data-testid="column"],
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) > div[data-testid="column"] {{
  box-sizing: border-box;
  min-width: 0;
  margin: 0 !important;
  padding: 0 !important;
  border-left: 1px solid {matrix_border};
  border-bottom: 1px solid {matrix_border};
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-header) > div[data-testid="column"]:last-child,
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) > div[data-testid="column"]:last-child {{
  border-right: 1px solid {matrix_border};
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-header) > div[data-testid="column"]:nth-child(2n+3),
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) > div[data-testid="column"]:nth-child(2n+3) {{
  border-right: 2px solid {matrix_border};
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-header) {{
  height: 720px !important;
  max-height: 720px !important;
  overflow-y: auto !important;
  overflow-x: auto !important;
  border: 1px solid {matrix_border} !important;
  border-radius: 0 !important;
  background: {surface} !important;
  box-shadow: none !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) {{
  overflow: auto !important;
  border: 1px solid {matrix_border} !important;
  border-radius: 0 !important;
  background: {surface} !important;
  box-shadow: none !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-header) > div {{
  overflow: visible !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) > div {{
  overflow: visible !important;
}}
div[data-testid="stVerticalBlock"]:has(.magic-matrix-header) > div[data-testid="stHorizontalBlock"]:first-child {{
  position: sticky !important;
  top: 0 !important;
  z-index: 1000 !important;
  min-height: 40px !important;
  background: {surface} !important;
}}
div[data-testid="stVerticalBlock"]:has(.magic-matrix-header) div[data-testid="stSelectbox"] > div {{
  min-height: 28px;
}}
div[data-testid="stVerticalBlock"]:has(.magic-matrix-header) div[data-testid="stSelectbox"] {{
  margin-bottom: 0 !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) div[data-testid="column"] > div[data-testid="stVerticalBlock"] {{
  gap: 0 !important;
  padding: 0 !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) .magic-matrix-date,
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) .magic-matrix-opponent,
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) .magic-matrix-empty {{
  min-height: 40px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) div[data-testid="column"]:has(div[data-testid="stSelectbox"]) {{
  box-sizing: border-box;
  min-width: 0;
  margin: 0 !important;
  padding: 0 !important;
  border: 1px solid {matrix_border};
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) div[data-testid="column"]:has(div[data-testid="stSelectbox"]) div[data-testid="stSelectbox"] {{
  margin: 0 !important;
  padding: 0 !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) div[data-testid="column"]:has(div[data-testid="stSelectbox"]) div[data-testid="stSelectbox"] > div {{
  min-height: 40px !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) div[data-testid="column"]:has(div[data-testid="stSelectbox"]) [data-baseweb="select"] > div {{
  min-height: 40px !important;
  border: 1px solid {matrix_border} !important;
  border-radius: 0 !important;
}}
div[data-testid="stVerticalBlock"]:has(.magic-matrix-date) {{
  gap: 0 !important;
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) > div[data-testid="column"] > div[data-testid="stVerticalBlock"] {{
  gap: 0 !important;
  padding: 0 !important;
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) > div[data-testid="column"]:has(div[data-testid="stSelectbox"]) [data-baseweb="select"] > div {{
  min-height: 40px !important;
  border: 1px solid {matrix_border} !important;
  border-radius: 0 !important;
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) > div[data-testid="column"]:has(div[data-testid="stSelectbox"]) div[data-testid="stSelectbox"] {{
  margin: 0 !important;
  padding: 0 !important;
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) > div[data-testid="column"] {{
  box-sizing: border-box;
  min-height: 46px !important;
  margin: 0 !important;
  padding: 0 !important;
  border-left: 1px solid {matrix_border};
  border-bottom: 1px solid {matrix_border};
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) .magic-matrix-date,
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) .magic-matrix-opponent,
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) .magic-matrix-empty {{
  min-height: 46px !important;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) div[data-testid="stSelectbox"] {{
  box-sizing: border-box;
  min-height: 46px !important;
  height: 46px !important;
  margin: 0 !important;
  padding: 0 !important;
  border: 1px solid {matrix_border} !important;
  background: {surface};
}}
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) div[data-testid="stSelectbox"] > div,
div[data-testid="stHorizontalBlock"]:has(.magic-matrix-date) [data-baseweb="select"] > div {{
  min-height: 46px !important;
  height: 46px !important;
  box-sizing: border-box;
}}
div[data-testid="stVerticalBlock"]:has(.magic-matrix-header) div[data-testid="stSelectbox"] [data-baseweb="select"] > div {{
  min-height: 28px;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  font-size: 1rem;
  line-height: 1.05;
  font-weight: 900;
  text-align: center;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-header) {{
  height: 720px !important;
  max-height: 720px !important;
  overflow: hidden !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-header) > div {{
  height: 100% !important;
  min-height: 0 !important;
  max-height: none !important;
  overflow-y: auto !important;
  overflow-x: auto !important;
  scroll-padding-top: 40px;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-header) > div > div[data-testid="stVerticalBlock"] > div[data-testid="stHorizontalBlock"]:has(.magic-matrix-header) {{
  position: sticky !important;
  top: 0 !important;
  z-index: 1000 !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) {{
  height: 720px !important;
  max-height: 720px !important;
  overflow: hidden !important;
}}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.magic-matrix-date) > div {{
  height: 100% !important;
  min-height: 0 !important;
  max-height: none !important;
  overflow-y: auto !important;
  overflow-x: auto !important;
}}
.makeup-note {{
  width: fit-content;
  max-width: 100%;
  margin: 0.35rem 0 0.8rem;
  padding: 0.48rem 0.68rem;
  border: 1px solid {border};
  border-radius: 7px;
  background: {surface_soft};
  color: {muted};
  font-size: 0.82rem;
  line-height: 1.45;
  font-weight: 800;
}}
.schedule-calendar-card {{
  width: 100%;
  margin: 0.35rem 0 1rem;
  border: 1px solid {border};
  border-radius: 10px;
  background: {surface};
  box-shadow: {shadow};
  overflow: hidden;
}}
.schedule-calendar-title {{
  min-height: 38px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: {text};
  font-size: 1.18rem;
  font-weight: 900;
  text-align: center;
}}
.schedule-calendar-heading {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.72rem 0.9rem;
  border-bottom: 1px solid {border};
  background: {surface_soft};
  color: {text};
  font-size: 0.9rem;
}}
.schedule-calendar-heading strong {{
  color: {primary};
  font-size: 1.02rem;
}}
.schedule-calendar-heading span {{
  color: {muted};
  font-size: 0.74rem;
  font-weight: 800;
}}
.schedule-calendar-heading b {{
  margin-left: 0.3rem;
  color: {primary};
  font-size: 1.02rem;
  font-weight: 900;
}}
.schedule-calendar-weekdays,
.schedule-calendar-grid {{
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
}}
.schedule-calendar-weekday {{
  padding: 0.42rem 0.18rem;
  border-right: 1px solid {border};
  border-bottom: 1px solid {border};
  background: {surface_soft};
  color: {muted};
  font-size: 0.76rem;
  font-weight: 900;
  text-align: center;
}}
.schedule-calendar-weekday:last-child {{ border-right: 0; }}
.schedule-calendar-weekday.weekday-5 {{ color: #2563eb; }}
.schedule-calendar-weekday.weekday-6 {{ color: #d71920; }}
.schedule-calendar-cell {{
  position: relative;
  box-sizing: border-box;
  min-height: 94px;
  padding: 0.38rem 0.4rem;
  border-right: 1px solid {border};
  border-bottom: 1px solid {border};
  background: {surface};
  color: {text};
}}
.schedule-calendar-cell:nth-child(7n) {{ border-right: 0; }}
.schedule-calendar-grid .schedule-calendar-cell:nth-last-child(-n + 7) {{ border-bottom: 0; }}
.schedule-calendar-day-empty {{ background: {surface_soft}; opacity: 0.66; }}
.schedule-calendar-empty {{ background: {surface_soft}; opacity: 0.4; }}
.schedule-calendar-event {{ background: {surface}; }}
.schedule-calendar-event.probability-1 {{ background: #eef2f6; }}
.schedule-calendar-event.probability-2 {{ background: #e5edf8; }}
.schedule-calendar-event.probability-3 {{ background: #d8e6f7; }}
.schedule-calendar-event.probability-4 {{ background: #fff2bf; }}
.schedule-calendar-day-number {{
  display: block;
  color: {muted};
  font-size: 0.78rem;
  line-height: 1.1;
  font-weight: 900;
}}
.schedule-calendar-opponent {{
  display: block;
  margin-top: 0.25rem;
  color: {primary};
  font-size: 0.72rem;
  line-height: 1.15;
  font-weight: 900;
}}
.schedule-calendar-team,
.schedule-calendar-venue {{
  display: block;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}}
.schedule-calendar-venue {{
  margin-top: 0.16rem;
  padding-top: 0.14rem;
  border-top: 1px solid {primary};
  color: {muted};
  font-size: 0.64rem;
  line-height: 1.1;
  font-weight: 800;
}}
.schedule-calendar-probability {{
  position: absolute;
  right: 0.42rem;
  bottom: 0.38rem;
  color: {primary};
  font-size: 1.15rem;
  line-height: 1;
  font-weight: 900;
}}
.schedule-calendar-caption {{
  padding: 0.52rem 0.8rem;
  color: {muted};
  font-size: 0.72rem;
  font-weight: 700;
}}
div[data-testid="stMetric"] {{
  background: {surface};
  border: 1px solid {border};
  border-radius: 8px;
  padding: 12px 14px;
  box-shadow: {shadow};
}}
div[data-testid="stMetric"] label,
div[data-testid="stMetric"] div {{
  color: {text};
}}
div[data-testid="stPlotlyChart"],
div[data-testid="stDataFrame"] {{
  border: 1px solid {border};
  border-radius: 8px;
  background: {surface};
  box-shadow: {shadow};
  overflow: hidden;
}}
.table-card {{
  width: 100%;
  border: 1px solid {border};
  border-radius: 8px;
  background: {surface};
  box-shadow: {shadow};
  overflow: hidden;
  margin-bottom: 1rem;
}}
.mobile-scenario-table {{
  display: none;
}}
.mobile-edit-actions {{
  display: none;
}}
.mobile-edit-link {{
  display: block;
  width: 100%;
  margin-top: 0.55rem;
  padding: 0.62rem 0.75rem;
  border: 1px solid {border};
  border-radius: 8px;
  background: {surface};
  color: {text} !important;
  text-align: center;
  font-weight: 900;
  text-decoration: none !important;
  box-shadow: 0 8px 18px rgba(32, 50, 70, 0.08);
}}
.mobile-edit-enabled {{
  display: none;
}}
.styled-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.94rem;
  color: {text};
}}
.styled-table thead tr {{
  background: {surface_soft};
}}
.styled-table th {{
  color: {muted};
  font-weight: 900;
  text-align: left;
  padding: 0.62rem 0.7rem;
  border-bottom: 1px solid {border};
}}
.styled-table td {{
  padding: 0.58rem 0.7rem;
  border-bottom: 1px solid {border};
  font-weight: 700;
}}
.styled-table tr:last-child td {{
  border-bottom: 0;
}}
.final-standings-table {{
  table-layout: fixed;
}}
.final-standings-table th:first-child,
.final-standings-table td:first-child {{
  width: 40%;
}}
.final-standings-table th:nth-child(n+2),
.final-standings-table td:nth-child(n+2) {{
  width: 15%;
  text-align: center;
}}
.games-behind-alert {{
  color: #dc2626;
  font-weight: 900;
}}
.styled-table tbody tr:nth-child(even) {{
  background: {"#1a2638" if dark_mode else "#fbfcfe"};
}}
.stTabs [data-baseweb="tab-list"] {{
  gap: 12px;
}}
.stTabs [data-baseweb="tab"] {{
  padding-left: 2px;
  padding-right: 2px;
  font-weight: 800;
}}
button[kind="secondary"] {{
  display: flex;
  align-items: center;
  justify-content: center;
  height: 30px;
  min-height: 30px;
  padding: 0 5px;
  font-weight: 800;
  font-size: 14px;
  background: {button_bg};
  border-color: {border};
  color: {text};
}}
button[kind="secondary"] p {{
  width: 100%;
  margin: 0;
  line-height: 1;
  text-align: center;
}}
div[data-testid="stNumberInput"],
div[data-testid="stTextInput"] {{
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
}}
div[data-testid="stNumberInput"] input {{
  height: 30px;
  min-height: 30px;
  padding: 0 5px;
  line-height: 30px;
  font-size: 15px;
  font-weight: 900;
  text-align: center;
  color: {text};
  background: {input_bg};
  border-color: {border};
}}
div[data-testid="stNumberInput"] button {{
  display: flex;
  align-items: center;
  justify-content: center;
  height: 30px;
  min-height: 30px;
  font-weight: 800;
  background: {button_bg};
  color: {text};
}}
div[data-testid="stTextInput"] input {{
  height: 30px;
  min-height: 30px;
  width: 100%;
  max-width: 160px;
  box-sizing: border-box;
  padding: 0 5px;
  line-height: 30px;
  font-size: 15px;
  font-weight: 900;
  text-align: center;
  color: {text};
  background: {input_bg};
  border-color: {border};
}}
.compact-rate {{
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 30px;
  padding-top: 0;
  font-variant-numeric: tabular-nums;
  font-size: 15px;
  font-weight: 900;
  text-align: center;
  color: {text};
}}
.scenario-header {{
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 38px;
  font-size: 14px;
  font-weight: 900;
  line-height: 1.1;
  padding: 0;
  text-align: center;
  color: {text};
}}
.scenario-team {{
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 30px;
  font-size: 15px;
  font-weight: 900;
  line-height: 1;
  white-space: nowrap;
  text-align: center;
  color: {text};
}}
div[data-testid="stHorizontalBlock"] {{
  gap: 0.28rem;
}}
.scenario-grid {{
  border: 1px solid {border};
  border-radius: 8px;
  overflow: hidden;
  background: {surface};
  box-shadow: {shadow};
  max-width: 1040px;
  margin: 0.85rem 0 0;
}}
.scenario-grid > div[data-testid="stHorizontalBlock"]:first-of-type {{
  background: {surface_soft};
  border-bottom: 1px solid {border};
  padding: 2px 6px;
}}
div[data-testid="stHorizontalBlock"]:has(.scenario-header),
div[data-testid="stHorizontalBlock"]:has(.scenario-team) {{
  max-width: 1040px;
  margin-left: 0 !important;
  margin-right: 0 !important;
  align-items: center;
}}
div[data-testid="stHorizontalBlock"]:has(.scenario-header) {{
  min-height: 40px;
  padding: 2px 0;
}}
div[data-testid="stHorizontalBlock"]:has(.scenario-team) {{
  min-height: 40px;
  padding: 2px 0;
  border-top: 1px solid {border};
}}
div[data-testid="stHorizontalBlock"]:has(.scenario-team) > div[data-testid="column"] {{
  padding-top: 0 !important;
  padding-bottom: 0 !important;
}}
div[data-testid="stHorizontalBlock"]:has(.scenario-team) div[data-testid="stVerticalBlock"] {{
  gap: 0 !important;
}}
div[data-testid="stHorizontalBlock"]:has(.scenario-team) div[data-testid="stHorizontalBlock"]:has(button):has(div[data-testid="stNumberInput"]) {{
  gap: 0.22rem !important;
  align-items: center !important;
}}
div[data-testid="stHorizontalBlock"]:has(.scenario-team) div[data-testid="stElementContainer"],
div[data-testid="stHorizontalBlock"]:has(.scenario-team) div[data-testid="stButton"],
div[data-testid="stHorizontalBlock"]:has(.scenario-team) div[data-testid="stNumberInput"],
div[data-testid="stHorizontalBlock"]:has(.scenario-team) div[data-testid="stTextInput"] {{
  margin-bottom: 0 !important;
}}
div[data-testid="stHorizontalBlock"]:has(.scenario-team) div[data-testid="stButton"] {{
  display: flex;
  align-items: center;
}}
.stCaptionContainer, .stMarkdown p {{
  color: {muted};
}}
button[kind="primary"] {{
  background: linear-gradient(90deg, {primary}, {"#f97316" if dark_mode else "#ff4b4b"});
  border: 0;
  box-shadow: {shadow};
  font-weight: 900;
}}
@media (max-width: 900px) {{
  .block-container {{
    padding: 1rem 0.7rem 2rem;
    max-width: 100%;
  }}
  .app-title {{
    font-size: 1.85rem;
    line-height: 1.18;
    margin-bottom: 0.25rem;
  }}
  .app-caption {{
    font-size: 0.8rem;
    margin-bottom: 1rem;
  }}
  .mode-control-label {{
    margin-top: 0.1rem;
    text-align: left;
  }}
  div[data-testid="stToggle"] {{
    justify-content: flex-start;
  }}
  div[data-testid="stMetric"] {{
    padding: 8px 10px;
  }}
  div[data-testid="stMetric"] label {{
    font-size: 0.72rem;
  }}
  div[data-testid="stMetricValue"] {{
    font-size: 1.2rem;
  }}
  div[data-testid="stExpander"] details summary p {{
    font-size: 16px;
  }}
  .stTabs [data-baseweb="tab-list"] {{
    gap: 8px;
    overflow-x: auto;
    flex-wrap: nowrap;
    scrollbar-width: thin;
  }}
  .stTabs [data-baseweb="tab"] {{
    min-width: max-content;
    font-size: 0.86rem;
    padding-left: 4px;
    padding-right: 4px;
  }}
  .table-card {{
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }}
  .schedule-calendar-card {{
    margin-top: 0.25rem;
  }}
  .schedule-calendar-cell {{
    min-height: 78px;
    padding: 0.32rem 0.25rem;
  }}
  .schedule-calendar-heading {{
    padding: 0.6rem 0.7rem;
  }}
  .schedule-calendar-opponent {{
    font-size: 0.66rem;
  }}
  .schedule-calendar-venue {{
    font-size: 0.58rem;
  }}
  .schedule-calendar-probability {{
    right: 0.25rem;
    bottom: 0.28rem;
    font-size: 0.96rem;
  }}
  .scenario-caption {{
    white-space: normal;
  }}
  .scenario-note {{
    width: 100%;
    white-space: normal;
  }}
  .styled-table {{
    min-width: 420px;
    font-size: 0.82rem;
  }}
  .styled-table th,
  .styled-table td {{
    padding: 0.48rem 0.55rem;
  }}
  div[data-testid="stPlotlyChart"] {{
    border-radius: 6px;
  }}
  .modebar-container,
  .modebar {{
    display: none !important;
  }}
  div[data-testid="stPlotlyChart"] .js-plotly-plot,
  div[data-testid="stPlotlyChart"] .plotly,
  div[data-testid="stPlotlyChart"] .main-svg {{
    touch-action: pan-y !important;
  }}
  div[data-testid="stPlotlyChart"] .draglayer,
  div[data-testid="stPlotlyChart"] .nsewdrag,
  div[data-testid="stPlotlyChart"] .zoomlayer {{
    pointer-events: none !important;
  }}
  button[kind="secondary"] {{
    min-height: 30px;
    font-size: 14px;
  }}
  div[data-testid="stNumberInput"] input,
  div[data-testid="stTextInput"] input {{
    min-height: 30px;
    font-size: 15px;
    padding: 2px 4px;
    max-width: none;
  }}
  .compact-rate,
  .scenario-header,
  .scenario-team {{
    font-size: 14px;
  }}
  .scenario-team {{
    line-height: 30px;
  }}
  .scenario-row {{
    padding: 3px 4px 1px;
  }}
  .scenario-grid {{
    display: none;
  }}
  .scenario-row,
  div[data-testid="stHorizontalBlock"]:has(.scenario-header),
  div[data-testid="stHorizontalBlock"]:has(.scenario-team) {{
    display: none !important;
  }}
  .mobile-scenario-table {{
    display: block;
    width: 100%;
    border: 1px solid {border};
    border-radius: 8px;
    overflow: hidden;
    background: {surface};
    box-shadow: 0 8px 18px rgba(32, 50, 70, 0.08);
  }}
  .mobile-edit-actions {{
    display: block;
  }}
  .mobile-table {{
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-size: 12px;
    color: {text};
  }}
  .mobile-table thead tr {{
    background: {surface_soft};
  }}
  .mobile-table th,
  .mobile-table td {{
    padding: 0.48rem 0.34rem;
    border-bottom: 1px solid {border};
    text-align: center;
    font-weight: 800;
    white-space: nowrap;
  }}
  .mobile-table th:first-child,
  .mobile-table td:first-child {{
    width: 31%;
    text-align: left;
    padding-left: 0.55rem;
  }}
  .mobile-table th:nth-child(2),
  .mobile-table td:nth-child(2) {{
    width: 27%;
  }}
  .mobile-table th:nth-child(3),
  .mobile-table td:nth-child(3),
  .mobile-table th:nth-child(4),
  .mobile-table td:nth-child(4) {{
    width: 21%;
  }}
  .mobile-table tr:last-child td {{
    border-bottom: 0;
  }}
  .stApp:has(.mobile-edit-enabled) .mobile-scenario-table {{
    display: none;
  }}
  .stApp:has(.mobile-edit-enabled) .scenario-grid {{
    display: block;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }}
  .stApp:has(.mobile-edit-enabled) div[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {{
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }}
  .stApp:has(.mobile-edit-enabled) div[data-testid="stHorizontalBlock"]:has(.scenario-header),
  .stApp:has(.mobile-edit-enabled) div[data-testid="stHorizontalBlock"]:has(.scenario-team) {{
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: nowrap !important;
    align-items: center !important;
    min-width: 720px;
  }}
  .stApp:has(.mobile-edit-enabled) .scenario-row {{
    display: block !important;
  }}
  .stApp:has(.mobile-edit-enabled) div[data-testid="stHorizontalBlock"]:has(button):has(div[data-testid="stNumberInput"]) {{
    flex-direction: row !important;
    flex-wrap: nowrap !important;
    gap: 0.24rem !important;
    min-width: 124px;
  }}
  .stApp:has(.mobile-edit-enabled) div[data-testid="stHorizontalBlock"]:has(button):has(div[data-testid="stNumberInput"]) > div[data-testid="column"]:first-child,
  .stApp:has(.mobile-edit-enabled) div[data-testid="stHorizontalBlock"]:has(button):has(div[data-testid="stNumberInput"]) > div[data-testid="column"]:last-child {{
    flex: 0 0 30px !important;
    width: 30px !important;
    min-width: 30px !important;
  }}
  .stApp:has(.mobile-edit-enabled) div[data-testid="stHorizontalBlock"]:has(button):has(div[data-testid="stNumberInput"]) > div[data-testid="column"]:nth-child(2) {{
    flex: 0 0 58px !important;
    width: 58px !important;
    min-width: 58px !important;
  }}
}}
@media (max-width: 640px) {{
  .schedule-calendar-cell {{
    min-height: 68px;
    padding: 0.28rem 0.18rem;
  }}
  .schedule-calendar-weekday {{
    padding: 0.34rem 0.1rem;
    font-size: 0.68rem;
  }}
  .schedule-calendar-day-number {{
    font-size: 0.7rem;
  }}
  .schedule-calendar-opponent {{
    margin-top: 0.18rem;
    font-size: 0.58rem;
  }}
  .schedule-calendar-venue {{
    margin-top: 0.12rem;
    padding-top: 0.1rem;
    font-size: 0.5rem;
  }}
  .schedule-calendar-probability {{
    right: 0.16rem;
    bottom: 0.2rem;
    font-size: 0.82rem;
  }}
  .schedule-calendar-heading span {{
    font-size: 0.64rem;
  }}
  .block-container {{
    padding-left: 0.48rem;
    padding-right: 0.48rem;
  }}
  .app-title {{
    font-size: 1.55rem;
  }}
  .app-caption {{
    font-size: 0.74rem;
  }}
  h2, h3 {{
    font-size: 1.12rem !important;
  }}
  .styled-table {{
    min-width: 360px;
  }}
  .mobile-table {{
    font-size: 11.5px;
  }}
  .mobile-table th,
  .mobile-table td {{
    padding: 0.44rem 0.24rem;
  }}
  div[data-testid="stHorizontalBlock"]:has(.magic-matrix-header) {{
    padding-left: 8px !important;
    padding-right: 8px !important;
  }}
}}
</style>
""",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
