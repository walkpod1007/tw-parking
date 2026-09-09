# tw-parking

台灣停車場即時空位查詢的命令列工具。給一個座標或地址，回報附近的停車場、還剩幾格、
以及一條可以直接點開導航的連結。

**大部分功能不需要任何金鑰。** 主要資料源是公開端點，`near <lat> <lon>` 開箱即用。

```
$ python3 parking.py near 25.047697 121.515114 --radius 500

1. 臺北車站西側地上停車場
   143m｜🟢 23/96｜即時｜計時：小型車100元/時、機車30元/時
   https://www.google.com/maps/dir/?api=1&destination=25.047629,121.516532

2. 臺北車站西區地下停車場
   153m｜🟢 17/179｜即時｜計時：100元/時
   https://www.google.com/maps/dir/?api=1&destination=25.047854,121.516622
```

## 燈號

| 燈號 | 意思 |
|---|---|
| 🟢 | 還有 3 格以上 |
| 🟡 | 只剩 1 到 2 格 |
| 🔴 | 0 格 |
| ❓ | 查不到，或資料太舊（超過 24 小時）不敢給數字 |

過期的即時值一律降成 ❓ 不給燈號。拿一天前的數字當現況等於騙人，寧可說不知道。

## 資料來源

分四層，由上而下遞補：

1. **parkboss.tw** — 全國涵蓋最廣的一層，六都的即時空位大多來自這裡
2. **縣市政府自有端點** — 屏東、嘉義、彰化、雲林、新竹、台東、南投等 10 個
3. **本地靜態庫**（`static-store.json`）— 屏東縣路外、嘉義縣、新竹縣三份開放資料
   只給地址不給經緯度，事先轉好座標存成一份檔案，查詢時零額外請求
4. **TDX 運輸資料流通服務** — 只在前三層查不到的縣份出手（苗栗、金門），需要免費金鑰

## 用法

```bash
python3 parking.py near <lat> <lon> [--radius 800] [--limit 3] [--json]
python3 parking.py near-address "<地址或地標>"      # 需要 Google 金鑰，見下
```

常用選項：

- `--radius` 搜尋半徑，公尺，預設 800
- `--limit` 列幾個，預設 3，`0` 代表全部
- `--include-moto` 連機車專用場一起列（預設濾掉）
- `--json` 輸出結構化資料，**不受 `--limit` 影響**（截斷會讓下游誤以為附近只有三個場）

排序規則：先分 500 公尺帶，帶內優先給查得到空位的，再按距離。「已知 0 格」排在
「不知道」後面——已知客滿比未知更糟，去了一定白跑。

## 給 AI 助理用（MCP）

`mcp_server.py` 把這支工具包成一台 MCP server，走 stdio。**它在你自己的機器上跑**，
不是連到誰的伺服器：MCP client 把它當子程序啟動，查詢直接從你的機器打向資料源。

Claude Desktop 之類的設定檔寫法：

```json
{
  "mcpServers": {
    "tw-parking": {
      "command": "python3",
      "args": ["/絕對路徑/tw-parking/mcp_server.py"]
    }
  }
}
```

提供一個工具 `find_parking`，參數 `lat`、`lon`、`radius`、`limit`、`include_moto`，
回傳結構化的停車場清單（含 `nav_url` 導航連結）。沒有第三方套件依賴。

會執行 shell 的代理（Claude Code、Codex CLI、Cursor 之類）其實不必走 MCP，
直接呼叫 `python3 parking.py near <lat> <lon> --json` 更省事。

## 選用設定（都不設也能跑）

| 環境變數 | 作用 |
|---|---|
| `GOOGLE_MAPS_API_KEY` | 開啟 `near-address` 地址查詢（Geocoding API） |
| `TDX_CLIENT_ID` / `TDX_CLIENT_SECRET` | 開啟第四層 TDX；也可寫進 `~/.config/parking/tdx.env` |
| `TDX_CRED_FILE` | 改憑證檔位置 |
| `PARKING_CACHE_DIR` | 改快取目錄，預設 `~/.cache/parking` |
| `PARKING_SHORTLINK_CMD` | 把導航長網址換成短網址的指令，收一個參數印一行 |
| `PARKING_SHORTLINK_PREFIX` | 短網址應有的開頭，用來擋掉指令印出奇怪東西 |

短網址走本地台帳去重：多數縮網址服務每收到一次請求就發一個新短碼，同一個場查十次
就在後端留十筆垃圾。場的座標不會變，所以一個場一輩子只該有一個短碼。

## 已知限制

- **機車專用場的過濾是盡力而為**：資料源沒有車種欄位，只能看名字帶「機車」而且不帶
  「汽」——「汽機車停車場」那種兩種都收不能濾掉。漏網之魚有可能發生。
- **總格數 0 視同沒公布**，只印剩餘不印分母；有些來源用 0 表示未知容量。
- **跨來源合併靠名字比對**，完全同名才無條件放行；包含關係要求較短的那個 6 字以上
  且核心名不是「第一停車場」「站前停車場」這類通用名，否則跨鄉鎮同名場會互相吃到
  對方的空位數。
- 少數政府端點的 TLS 憑證鏈不完整，程式對**名單內的特定主機**放寬驗證。名單寫死在
  `sources.py`，新增來源不會安靜地跟著吃 `CERT_NONE`。

## 資料來源與顯名聲明

程式碼是 MIT，**資料不是**。本專案內含與取用的政府開放資料依
[政府資料開放授權條款第 1 版](https://data.gov.tw/license) 使用，該條款允許再散布與商業利用，
但要求標示原資料提供機關。隨附的 `static-store.json`（351 筆，只有場名、地址、座標、格數、費率）
就是這樣的衍生資料：

- 屏東縣政府「路外公共停車場」「恆春鎮停車場」開放資料（120 筆）
- 嘉義縣政府停車場開放資料（93 筆）
- 新竹縣政府停車場開放資料（138 筆）
- 執行期間另即時取用：彰化縣、雲林縣、花蓮縣、臺東縣、南投縣、澎湖縣政府的停車場端點
- 交通部「TDX 運輸資料流通服務平臺」（第四層，需自行申請免費金鑰）

以上依政府資料開放授權條款第 1 版利用。`parkboss.tw` 是民間服務，非政府開放資料，
本專案只是以一般使用者身分讀取其公開端點。

**對資料來源的禮貌**：所有對外請求都有快取，請不要把節流拿掉——這些是縣市政府的機器，
不是設計來承受大量請求的。

| 層 | 快取 | 說明 |
|---|---|---|
| parkboss 與 10 個縣府端點 | 60 秒（`PARKING_HTTP_TTL`） | 每次查詢會併行打全部縣府端點，快取讓連續查詢不會重複打 |
| 本地靜態庫 | 不必 | 讀本地檔案，零請求 |
| TDX | 場資 7 天、即時空位 60 秒 | 免費方案實測連打第 6 發就 429，這層節流最嚴 |

`PARKING_HTTP_TTL=0` 可以關掉 HTTP 快取，但沒有理由這樣做——即時空位本來就以分鐘計。

## 授權

程式碼採 MIT；資料照上一節的授權條款。

## 遠端版（Cloudflare Worker）

`worker/` 是同一支查詢邏輯的 TypeScript 移植，跑在 Cloudflare Workers 上，給**跑不了本機程式的 AI 網頁版**用
（ChatGPT／Claude／Gemini 的網頁介面接不到 stdio MCP，只能接 HTTP）。行為跟 Python 版對齊：
同一個 `find_parking` 工具、同樣的四層資料源、同樣的新鮮度與燈號判斷。

公開端點（開放模式，不用金鑰）：

```
https://parking.life-os.work
```

MCP client 設定裡加一台 HTTP 型 server 指到這個網址即可；`GET /health` 回 `{"ok":true}` 可拿來探活。

### 自己架一份

```
cd worker
npm install
npx wrangler kv namespace create TOKENS   # 把回傳的 id 填進 wrangler.toml
npx wrangler deploy
```

- 上游回應用 Cache API 快取 60 秒（`TW_PARKING_HTTP_TTL` 可調，0＝不快取）
- 要鎖權杖：`wrangler secret put TW_PARKING_TOKEN`，並把 `[vars]` 的 `TW_PARKING_OPEN` 拿掉；之後 POST 要帶 `Authorization: Bearer <token>`
- 要接 TDX（苗栗、金門那層）：`wrangler secret put TDX_CLIENT_ID` 與 `TDX_CLIENT_SECRET`，token 會存在 KV 裡重用到過期；沒設就跳過那層，其他三層照常
- 免費方案每次呼叫 CPU 10 毫秒、子請求 50 次；台東那層只對半徑內最近的 12 座場站抓細節（超過時日誌印一行筆數），其餘照 Python 版
- 本機開發：`npx wrangler dev --local`，再跑 `tests/parity.sh <基準網址> http://127.0.0.1:8787` 對照兩邊回同樣的場站
