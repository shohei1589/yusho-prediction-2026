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
   - Secrets: 認証を使わない場合はなし。Googleログイン制限を使う場合は後述のSecretsを設定します。
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

## Googleアカウント制限

`g.softbank.co.jp` のGoogleアカウントだけに制限する場合は、Google CloudでOAuthクライアントを作成し、Streamlit Community CloudのApp settings > Secretsに次を設定します。

```toml
APP_AUTH_ENABLED = "true"
APP_ALLOWED_EMAIL_DOMAIN = "g.softbank.co.jp"

[auth]
redirect_uri = "https://<your-app>.streamlit.app/oauth2callback"
cookie_secret = "<random-long-secret>"
client_id = "<google-oauth-client-id>"
client_secret = "<google-oauth-client-secret>"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

Google Cloud側のOAuthクライアントでは、Authorized redirect URIsに同じ `redirect_uri` を追加します。

Secretsを保存すると、アプリはGoogleログインを要求します。ログイン後のメールアドレスが `@g.softbank.co.jp` でない場合は利用できません。
