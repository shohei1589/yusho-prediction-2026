# 2026 優勝予測

NPB公式サイトの勝敗表と残り日程を取得し、指定球団の優勝確率と優勝確定日の分布をシミュレーションするStreamlitアプリです。

## 注意書き

- データ出典: NPB.jp 日本野球機構
- 本アプリは非公式・非商用の予測ツールです。
- NPB公式および各球団とは無関係です。
- 取得したNPB公式データをローカルファイルとして同梱・再配布しない構成にしています。
- 予測モデルは簡易シミュレーションであり、先発投手、移動、球場、登録抹消、怪我、天候中止などは明示的に考慮していません。

## 主な機能

- セ・リーグ / パ・リーグ対応
- 対象球団、基準日、試行回数の指定
- NPB公式の勝敗表を初期値にした勝敗・引き分け編集
- 残り試合に対するチーム別の想定勝率編集
- 対象球団の残り日程表示
- ライト / ダーク表示切り替え

## ローカル実行

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\streamlit.exe run .\streamlit_app.py
```

社内ネットワークなどでSSL検証エラーやプロキシ設定エラーが出る場合のみ、ローカル実行時に次を設定します。

```powershell
$env:NPB_VERIFY_SSL='false'
$env:NPB_USE_ENV_PROXY='false'
```

公開環境では原則として `NPB_VERIFY_SSL=true`、`NPB_USE_ENV_PROXY=false` のまま使います。

## 公開時の起動ファイル

Streamlit Community Cloudでは、Main file pathに次を指定します。

```text
streamlit_app.py
```

## ライセンス

公開範囲と再利用許可を決めるまでは、ライセンスファイルを置いていません。
