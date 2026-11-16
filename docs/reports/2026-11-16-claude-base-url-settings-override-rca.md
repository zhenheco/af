# RCA：Claude 候選項 base_url 被使用者 settings 覆蓋

## 現象
設定 `base_url` + `api_key_command` 的 Claude 候選項（例如 DeepSeek 相容端點）派工失敗：請求實際送到使用者 `$HOME/.claude/settings.json` 的 `env.ANTHROPIC_BASE_URL` 所指向的本機代理，而非 af 注入的端點。

## 根因
`--bare` 本身不會讓 CLI 忽略 settings 的 `env` 區塊；該區塊會覆蓋行程環境變數中 af 注入的 `ANTHROPIC_BASE_URL`。金鑰本身有效（直連 API 與以 `x-api-key` 的 CLI 請求皆成功），並非金鑰失效。

## 修復
有明確 `base_url` 的 Claude 候選項改為 `claude --bare --setting-sources "" ...`（空字串為獨立 argv 值），隔離 user／project／local settings。未設定 `base_url` 的原生 Claude 候選項行為不變。API_KEY／BASE_URL 注入、移除 `ANTHROPIC_AUTH_TOKEN`、`env_allow` 清洗、金鑰指令失敗即中止、tools／權限／MCP／逾時／failover 皆未改動。

## 驗證
- `tests/test_dispatch.py`：假 CLI 模擬 settings `env` 覆蓋語意；修復前請求送往 decoy（紅），修復後送往目標（綠）；另有原生候選項不變、argv 不含金鑰之斷言。
- 離線 canary（儲庫外腳本，使用真實安裝的 CLI、本機 HTTPS 假伺服器、臨時 HOME 內的惡意 settings）：修復後目標 1 次命中、decoy 0 次、tools 僅 `Read`；還原舊碼的對照組則 decoy 命中、目標 0 次。
- 限制：canary 使用自簽憑證與 `NODE_EXTRA_CA_CERTS`，僅證明端點選擇，不證明真實供應商回應；真實 smoke 由主機另行執行。

## 追加回歸：無 tools 的 base_url 候選項無法編輯
- 現象：經候選項 runtime 啟動的真實 CLI 後續任務只能讀檔；Edit／Write 被拒，會改檔的 Bash 停在等待核准。
- 根因：`--setting-sources ""` 同時隔離了全域的 permission settings；而明確 `base_url` 且無 `tools` 的分支未帶 `--permission-mode`（僅 tools 分支設有 `acceptEdits`），因此退回預設需核准模式，非互動下無法寫入。
- 修復：明確 `base_url` 且無 `tools` 時加上 `--permission-mode acceptEdits`，即 af 既有的編輯政策。有 `tools` 時仍只有一組 `acceptEdits`／`--strict-mcp-config`，無重複旗標；原生 Claude 不變；保留 `--bare --setting-sources ""` 與 URL／金鑰／環境隔離。未使用 bypassPermissions、allowedTools 全開，也未改 sandbox；`acceptEdits` 不會自動核准任意 shell。
- 證據：`tests/test_dispatch.py` 的無 tools base_url `env_allow` 測試加入惡意 settings，並斷言目標端點與完整 argv（恰一個 `acceptEdits`）；修復前紅（argv 缺 `--permission-mode acceptEdits`），修復後綠。
- 限制：測試使用假 CLI，僅證明 argv 與端點選擇；真實安裝 CLI 能否在新 runtime 下實際編輯，須由主機以臨時 HOME＋本機假端點的 canary 驗證，本次未親自執行。
