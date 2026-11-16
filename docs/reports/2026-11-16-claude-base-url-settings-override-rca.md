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
