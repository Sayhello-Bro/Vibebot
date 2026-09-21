# 登入與頻道資料

1. 開發模式執行 `python login.py`。
2. 第一次使用時輸入帳號與密碼，按「儲存帳號」。
3. 按「登入」後會開啟主介面。
4. 主介面需填寫「頻道名稱」才能啟動直播流程。

帳號資料儲存在 `login_accounts.json`，密碼只保存 SHA-256 雜湊，不保存明文。

每次啟動直播後，頻道名稱、網址、FB 帳號 ID 與登入帳號會寫到：

`sessions/metadata/<stream_id>.json`

若要建立登入執行檔：

```powershell
pyinstaller login.spec
```
