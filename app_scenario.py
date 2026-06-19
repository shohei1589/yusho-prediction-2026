from __future__ import annotations

from datetime import date
import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from yusho.npb_client import (
    fetch_remaining_schedule,
    fetch_standings,
    schedule_to_daily_opponents,
)
from yusho.simulation import SimulationResult, run_simulations
from yusho.teams import CENTRAL, PACIFIC, league_teams, team_label


LEAGUE_LABELS = {
    PACIFIC: "パ・リーグ",
    CENTRAL: "セ・リーグ",
}
LEAGUE_BY_LABEL = {label: code for code, label in LEAGUE_LABELS.items()}


st.set_page_config(page_title="2026 優勝予測", layout="wide")


def main() -> None:
    dark_mode = st.session_state.get("dark_mode", False)
    _apply_style(dark_mode)

    with st.sidebar:
        st.header("条件")
        year = st.number_input("年度", min_value=2026, max_value=2030, value=2026, step=1)
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
        seed_enabled = st.checkbox("乱数を固定", value=True)
        seed = st.number_input("Seed", min_value=0, max_value=999_999, value=42, step=1)
        with st.expander("通信設定（通常変更不要）", expanded=False):
            verify_ssl = st.toggle(
                "SSL検証",
                value=os.getenv("NPB_VERIFY_SSL", "true").lower() not in {"0", "false", "no"},
                help="NPB公式サイトの証明書を検証します。公開環境ではオン推奨です。",
            )
            use_env_proxy = st.toggle(
                "環境変数プロキシを使う",
                value=os.getenv("NPB_USE_ENV_PROXY", "false").lower() in {"1", "true", "yes"},
                help="HTTP_PROXY / HTTPS_PROXY などの環境変数に設定されたプロキシを使います。通常はオフです。",
            )
        if st.button("公式データを再取得", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    os.environ["NPB_VERIFY_SSL"] = "true" if verify_ssl else "false"
    os.environ["NPB_USE_ENV_PROXY"] = "true" if use_env_proxy else "false"

    header_left, header_right = st.columns([5.5, 1])
    with header_left:
        st.markdown("<h1 class='app-title'>2026 優勝予測</h1>", unsafe_allow_html=True)
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
            daily_opponents = schedule_to_daily_opponents(schedule_result.frame, league)
    except Exception as exc:
        st.error("NPB公式データの取得に失敗しました。")
        st.exception(exc)
        return

    editor_key = f"scenario_editor_{int(year)}_{league}"
    scenario_input = _scenario_input_frame(standings_result.frame, league)
    _initialize_scenario_state(editor_key, scenario_input)

    with st.expander("勝敗を編集", expanded=True):
        st.caption(
            "勝敗表の初期値はNPB公式の現在値です。過去日や未来日を基準にする場合は、"
            "その日付の試合開始前時点に合わせて勝・敗・分を調整してください。"
        )
        reset_col, note_col = st.columns([1, 4])
        with reset_col:
            if st.button("公式値に戻す", use_container_width=True):
                _reset_scenario_state(editor_key, scenario_input)
                st.rerun()
        with note_col:
            st.info("今後の想定勝率は、残り試合の強さとして使います。勝敗表の現在勝率とは別に調整できます。")

        _render_scenario_controls(editor_key, scenario_input)

    try:
        scenario_standings, assumed_win_rates = _scenario_to_model_inputs(editor_key, scenario_input)
        scenario_signature = _scenario_signature(
            scenario_standings,
            assumed_win_rates,
            target_team,
            start_date,
            simulation_count,
            seed_enabled,
            int(seed),
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
                )
            st.session_state[result_key] = {
                "result": result,
                "standings": scenario_standings,
                "assumed_win_rates": assumed_win_rates,
                "signature": scenario_signature,
            }
        stored = st.session_state[result_key]
        result = stored["result"]
        displayed_standings = stored["standings"]
        displayed_rates = stored["assumed_win_rates"]
        if stored["signature"] != scenario_signature:
            st.warning("入力が変更されています。結果を更新するには「シミュレーション実行」を押してください。")
    except Exception as exc:
        st.error("入力値の変換または計算に失敗しました。")
        st.exception(exc)
        return

    _render_summary(
        result,
        displayed_standings,
        displayed_rates,
        schedule_result.frame,
        daily_opponents,
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


def _render_summary(
    result: SimulationResult,
    standings: pd.DataFrame,
    assumed_win_rates: dict[str, float],
    schedule: pd.DataFrame,
    daily_opponents: pd.DataFrame,
    league: str,
    target_team: str,
    start_date: date,
    simulation_count: int,
    dark_mode: bool,
) -> None:
    team_name = team_label(target_team)
    probability = result.champion_probability * 100

    metric_cols = st.columns([1, 1, 1, 1])
    metric_cols[0].metric(f"{team_name} 優勝確率", f"{probability:.1f}%")
    metric_cols[1].metric("対象球団の残り試合", f"{_remaining_games(schedule, target_team)}")
    metric_cols[2].metric("対象球団の残り開催日", f"{_remaining_game_dates(schedule, target_team)}")
    metric_cols[3].metric("試行回数", f"{simulation_count:,}")

    tab_result, tab_standings, tab_schedule, tab_model = st.tabs(
        ["予測", "入力値", "残り日程", "前提"]
    )

    with tab_result:
        left, right = st.columns([2, 1])
        with left:
            st.plotly_chart(
                _champion_date_chart(result, team_name, dark_mode),
                use_container_width=True,
            )
        with right:
            st.subheader("優勝確定日 上位")
            _render_table(_top_dates(result.champion_dates))
            st.subheader("平均最終成績")
            _render_table(_format_final_standings(result.final_standings))

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
        _render_table(_format_schedule(schedule, target_team))

    with tab_model:
        st.markdown(
            """
- 基準日は「その日の試合開始前」として扱います。
- 勝敗表の初期値はNPB.jpから取得した現在値です。過去日や任意シナリオでは、勝・敗・分を手で調整してください。
- 今後の想定勝率は、残り試合の勝敗確率を決めるために使います。
- 対戦勝率はLog5風のオッズ比で計算します。
- 引分の発生、先発投手、球場、移動、故障者、雨天中止の追加発生はモデルに含めていません。
- 優勝確定日は、対象球団が残り全敗しても他球団が勝率で上回れない最初の日として判定しています。
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
    st.markdown("<div class='scenario-grid'>", unsafe_allow_html=True)
    header = st.columns([1.35, 1.12, 1.12, 1.12, 0.82, 0.98])
    for col, label in zip(header, ["球団", "勝", "敗", "分", "現在勝率", "今後勝率"]):
        col.markdown(f"<div class='scenario-header'>{label}</div>", unsafe_allow_html=True)

    for _, row in scenario_input.iterrows():
        st.markdown("<div class='scenario-row'>", unsafe_allow_html=True)
        team = str(row["Team"])
        cols = st.columns([1.35, 1.12, 1.12, 1.12, 0.82, 0.98])
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
    st.markdown("</div>", unsafe_allow_html=True)


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
    target_team: str,
    start_date: date,
    simulation_count: int,
    seed_enabled: bool,
    seed: int,
) -> tuple[object, ...]:
    standing_values = tuple(
        (row.Team, int(row.Wins), int(row.Losses), int(row.Ties))
        for row in standings.sort_values("Team").itertuples(index=False)
    )
    rate_values = tuple(
        (team, round(float(rate), 4))
        for team, rate in sorted(assumed_win_rates.items())
    )
    return (
        standing_values,
        rate_values,
        target_team,
        start_date.isoformat(),
        int(simulation_count),
        bool(seed_enabled),
        int(seed),
    )


def _render_table(frame: pd.DataFrame) -> None:
    html = frame.to_html(index=False, escape=False, classes="styled-table")
    st.markdown(f"<div class='table-card'>{html}</div>", unsafe_allow_html=True)


def _champion_date_chart(result: SimulationResult, team_name: str, dark_mode: bool):
    frame = result.champion_dates.copy()
    if frame.empty:
        return px.bar(title=f"{team_name}の優勝確定日は記録されませんでした")
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.normalize()
    frame = frame.groupby("Date", as_index=False)["Probability"].sum()
    frame = frame.sort_values("Date").reset_index(drop=True)
    calendar = pd.DataFrame(
        {"Date": pd.date_range(frame["Date"].min(), frame["Date"].max(), freq="D")}
    )
    frame = calendar.merge(frame, on="Date", how="left")
    frame["Probability"] = frame["Probability"].fillna(0.0)
    frame["ProbabilityPct"] = frame["Probability"] * 100
    frame["DateLabel"] = frame["Date"].dt.month.astype(str) + "/" + frame["Date"].dt.day.astype(str)
    category_order = frame["DateLabel"].tolist()
    top_dates = set(frame.nlargest(3, "ProbabilityPct")["Date"])
    top_color = "#f97316" if dark_mode else "#d64b3c"
    other_color = "#3b82f6" if dark_mode else "#2f6f8f"
    frame["BarColor"] = frame["Date"].apply(lambda value: top_color if value in top_dates else other_color)

    fig = go.Figure(
        data=[
            go.Bar(
                x=frame["DateLabel"],
                y=frame["ProbabilityPct"],
                marker_color=frame["BarColor"],
                hovertemplate="%{x}<br>%{y:.1f}%<extra></extra>",
            )
        ]
    )
    fig.update_layout(
        title=f"{team_name} 優勝確定日分布",
        height=440,
        margin={"l": 10, "r": 10, "t": 60, "b": 10},
        showlegend=False,
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
            "title": "日付",
        },
        yaxis={"gridcolor": "#334155" if dark_mode else "#e5eaf0", "title": "確率 (%)"},
    )
    return fig


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


def _format_schedule(schedule: pd.DataFrame, target_team: str) -> pd.DataFrame:
    frame = schedule.copy()
    frame = frame[(frame["HomeTeam"] == target_team) | (frame["AwayTeam"] == target_team)]
    if frame.empty:
        return pd.DataFrame(columns=["日付", "カード", "球場", "開始", "状態"])
    frame["日付"] = frame["Date"].dt.strftime("%Y-%m-%d")
    frame["カード"] = frame.apply(
        lambda row: f"{team_label(row.HomeTeam)} - {team_label(row.AwayTeam)}",
        axis=1,
    )
    frame["状態"] = frame["Status"].map(
        {
            "scheduled": "予定",
            "in_progress": "進行中",
            "final": "終了",
            "canceled": "中止",
            "other": "その他",
        }
    )
    return frame[["日付", "カード", "Venue", "StartTime", "状態"]].rename(
        columns={"Venue": "球場", "StartTime": "開始"}
    )


def _top_dates(champion_dates: pd.DataFrame) -> pd.DataFrame:
    if champion_dates.empty:
        return pd.DataFrame(columns=["日付", "確率"])
    frame = champion_dates.sort_values("Probability", ascending=False).head(10).copy()
    frame["日付"] = frame["Date"].dt.strftime("%Y-%m-%d")
    frame["確率"] = frame["Probability"].map(lambda value: f"{value * 100:.1f}%")
    return frame[["日付", "確率"]]


def _remaining_games(schedule: pd.DataFrame, team: str) -> int:
    if schedule.empty:
        return 0
    return int(((schedule["HomeTeam"] == team) | (schedule["AwayTeam"] == team)).sum())


def _remaining_game_dates(schedule: pd.DataFrame, team: str) -> int:
    if schedule.empty:
        return 0
    frame = schedule[(schedule["HomeTeam"] == team) | (schedule["AwayTeam"] == team)]
    return int(frame["Date"].nunique())


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
div[data-testid="stAlert"] {{
  background: {info_bg};
  border-radius: 7px;
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
  min-height: 34px;
  padding: 0 6px;
  font-weight: 800;
  font-size: 17px;
  background: {button_bg};
  border-color: {border};
  color: {text};
}}
div[data-testid="stNumberInput"] input {{
  min-height: 34px;
  padding: 3px 6px;
  font-size: 18px;
  font-weight: 900;
  text-align: center;
  color: {text};
  background: {input_bg};
  border-color: {border};
}}
div[data-testid="stNumberInput"] button {{
  min-height: 34px;
  font-weight: 800;
  background: {button_bg};
  color: {text};
}}
div[data-testid="stTextInput"] input {{
  min-height: 34px;
  padding: 3px 8px;
  font-size: 17px;
  font-weight: 900;
  text-align: center;
  color: {text};
  background: {input_bg};
  border-color: {border};
}}
.compact-rate {{
  display: inline-block;
  width: 100%;
  padding-top: 5px;
  font-variant-numeric: tabular-nums;
  font-size: 18px;
  font-weight: 900;
  text-align: center;
  color: {text};
}}
.scenario-header {{
  font-size: 17px;
  font-weight: 900;
  line-height: 1.1;
  padding: 2px 0 4px;
  text-align: center;
  color: {text};
}}
.scenario-team {{
  font-size: 17px;
  font-weight: 900;
  line-height: 34px;
  white-space: nowrap;
  text-align: center;
  color: {text};
}}
div[data-testid="stHorizontalBlock"] {{
  gap: 0.42rem;
}}
.scenario-grid {{
  border: 1px solid {border};
  border-radius: 8px;
  overflow: hidden;
  background: {surface};
  box-shadow: {shadow};
}}
.scenario-row {{
  border-top: 1px solid {border};
  padding: 4px 6px 2px;
  background: {surface};
}}
.scenario-row:nth-of-type(even) {{
  background: {surface_soft};
}}
.scenario-grid > div[data-testid="stHorizontalBlock"]:first-of-type {{
  background: {surface_soft};
  border-bottom: 1px solid {border};
  padding: 5px 6px 2px;
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
</style>
""",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
