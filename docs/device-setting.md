# Device Setting — 新裝置環境重建指南

記錄在此裝置（Windows 11）上建立開發環境的完整步驟，供新裝置重建使用。

---

## 1. GitHub CLI 安裝

```powershell
winget install GitHub.cli
```

安裝路徑：`C:\Program Files\GitHub CLI`

安裝完成後，需將路徑加入 PATH（新開的 terminal 會自動生效，當前 session 需手動加）：

```powershell
$env:PATH = $env:PATH + ";C:\Program Files\GitHub CLI"
```

---

## 2. GitHub CLI 登入

開啟新的 PowerShell 視窗執行（不要用 Claude Code 的 `!` 前綴，需要互動式操作）：

```powershell
gh auth login
```

選擇順序：
1. `GitHub.com`
2. `HTTPS`
3. `Login with a web browser`
4. 複製畫面上的 8 位代碼，在瀏覽器完成授權

驗證登入：
```powershell
gh auth status
```

應看到：`✓ Logged in to github.com account e7228466 (keyring)`

---

## 3. Python 虛擬環境建立

```powershell
cd "C:\Users\JenTs\OneDrive\画像\ドキュメント\bloomAI\CC\coding-agent-final"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 4. 環境變數設定

複製範本並填入實際值：

```powershell
cp .env.example .env
```

需填入的變數（參考 `.env.example`）：
- `ANTHROPIC_API_KEY`
- `GITHUB_TOKEN`
- `GITHUB_REPO`
- `HARNESS_API_KEY`
- `HARNESS_ACCOUNT_ID`
- `HARNESS_ORG_ID`
- `HARNESS_PROJECT_ID`
- `HARNESS_PIPELINE_ID`
- `WEBHOOK_SECRET`

---

## 5. 測試確認環境正常

```powershell
.venv\Scripts\pytest tests/ -v --cov=agent --cov-report=term-missing
```

預期結果：122 tests 通過，coverage 100%

---

## 6. Git 工作流程（每次修改）

```powershell
# 1. 建立新 branch
git checkout -b <branch-name>

# 2. 做修改...

# 3. 跑測試
.venv\Scripts\pytest tests/ -v

# 4. Commit
git add <files>
git commit -m "type: description"

# 5. Push
git push -u origin <branch-name>

# 6. 建立 PR
gh pr create --title "..." --base main

# 7. Merge
gh pr merge <PR番號> --squash --delete-branch
```

---

## 7. 本地啟動 agent

```powershell
.venv\Scripts\uvicorn agent.main:app --reload --port 8000
```

---

## 備註

- `gh` CLI 在 Bash 工具中找不到，需透過 PowerShell 或獨立 terminal 使用
- `pytest` 需使用 `.venv\Scripts\pytest`，系統 PATH 可能沒有直接的 `pytest` 指令
- 不要直接 push 到 `main`，一律透過 branch + PR 流程
