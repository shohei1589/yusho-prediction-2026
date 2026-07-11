# 一般公開手順

GitHubにこの `public_app` フォルダだけをアップロードし、Streamlit Community Cloudからデプロイする想定です。

## こちらで必要な作業

1. GitHubアカウントにログインします。
2. GitHubで新しいリポジトリを作ります。
   - Repository name例: `yusho-prediction-2026`
   - 一般公開するならPublic、まず確認だけならPrivateでも大丈夫です。
   - README、.gitignore、licenseはGitHub側では追加しません。
3. このフォルダでGitを初期化してpushします。

```powershell
cd public_app
git init
git add .
git commit -m "Initial public Streamlit app"
git branch -M main
git remote add origin https://github.com/<YOUR_ACCOUNT>/<YOUR_REPO>.git
git push -u origin main
```

4. Streamlit Community Cloudを開き、Create app / New appからGitHubリポジトリを選びます。
5. Deploy設定は次の通りです。
   - Branch: `main`
   - Main file path: `streamlit_app.py`
   - Secrets: なし
6. デプロイ後、画面を開いてNPB公式データの取得が通ることを確認します。

## 公開前チェック

- `README.md` の注意書きが表示されている
- 内部資料、CSV、Excel、Notebook、HTML出力がGit対象に入っていない
- `.streamlit/secrets.toml` を作った場合はGit対象に入っていない
- 公開URLで勝敗表と残り日程が取得できる
- NPB公式ページ構造が変わった場合のエラー表示を許容できる

## 補足

公開後に画面を更新したい場合は、`public_app` 側のファイルを修正してから次を実行します。

```powershell
git add .
git commit -m "Update app"
git push
```

Streamlit Community Cloud側で自動的に再デプロイされます。
