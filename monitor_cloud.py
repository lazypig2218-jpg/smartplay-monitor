"""
SmartPLAY 監控 — Cloud 版(寫入 Supabase)
==========================================
每日跑:打康文署 API 攞 1610 條 -> upsert 入 Supabase。
「今次先 insert」到嘅 = 新 release,由 DB 嘅 first_seen_at 判斷,
唔再靠本機 seen_keys.json(cloud 上冇持久檔案)。

環境變數(GitHub Actions Secrets 設):
    SUPABASE_URL          你個 project URL,例:https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  service_role key(繞過 RLS 先寫到),★ 保密
    NTFY_TOPIC            (可選)第一階段用 ntfy 頂住推送

安裝:
    pip install curl_cffi supabase
"""

import os
import sys
import json
import time
import datetime
from curl_cffi import requests as creq
from supabase import create_client

API_URL = "https://www.smartplay.lcsd.gov.hk/rest/facility/api/v1/publ/fac-book-release-log/list"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# 香港 proxy(過康文署對非香港 IP 嘅封鎖)。
# 格式例:"http://user:pass@host:port" 或 "http://host:port"
# 冇設就直連(本機香港網絡跑得,唔使 proxy)。
HK_PROXY = os.environ.get("HK_PROXY")
PROXIES = {"http": HK_PROXY, "https": HK_PROXY} if HK_PROXY else None

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# 盡量似真人瀏覽器嘅完整 header。喺 data center IP(GitHub Actions)
# 康文署有時會回「請稍後再試」,補齊 header + retry 可以提高成功率。
BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-HK,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.smartplay.lcsd.gov.hk/facilities/watchlist",
    "Origin": "https://www.smartplay.lcsd.gov.hk",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}


# ------------------------------------------------------------- fetch ----

def fetch_all_sessions(max_retry=5):
    """
    一個 request 用大 pageSize 攞晒。如果康文署回「請稍後再試」,
    等陣再試(exponential backoff),最多試 max_retry 次。
    """
    last_err = None
    if HK_PROXY:
        # 唔好 print 埋密碼,只講有冇用 proxy
        print(f"經 proxy 出去:{HK_PROXY.split('@')[-1]}")
    for attempt in range(1, max_retry + 1):
        try:
            r = creq.get(
                API_URL,
                params={"pageNum": 1, "pageSize": 3000},
                impersonate="chrome",
                timeout=45,
                headers=BROWSER_HEADERS,
                proxies=PROXIES,
            )
            r.raise_for_status()
            payload = r.json()
            code = str(payload.get("code"))
            if code == "0":
                return (payload.get("data") or {}).get("list") or []
            # 康文署話「請稍後再試」—— 等陣 retry
            last_err = f"code={code} msg={payload.get('message')}"
            print(f"[試 {attempt}/{max_retry}] API 唔肯俾: {last_err}")
        except Exception as e:
            last_err = repr(e)
            print(f"[試 {attempt}/{max_retry}] 出錯: {last_err}")

        if attempt < max_retry:
            wait = 2 ** attempt   # 2,4,8,16 秒
            print(f"  等 {wait}s 再試...")
            time.sleep(wait)

    raise RuntimeError(f"API 試咗 {max_retry} 次都失敗,最後: {last_err}")


# ------------------------------------------------------------- map ----

def session_key(row):
    return "|".join(str(row.get(k, "")) for k in
                    ("venueId", "frmId", "ssnStartDate", "ssnStartTime", "ssnEndTime"))


def _clean_date(v):
    """空字串 / None -> None(Postgres date 唔收 '')。"""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    return v


def _clean_int(v):
    """空字串 -> None,數字字串 -> int。"""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def to_db_row(row, now_iso):
    """API record -> Supabase row。"""
    return {
        "session_key": session_key(row),
        "fat_id": _clean_int(row.get("fatId")),
        "fat_name": row.get("fatName"),
        "venue_id": _clean_int(row.get("venueId")),
        "venue_name": row.get("venueName"),
        "dist_code": row.get("distCode"),
        "dist_name": (row.get("distName") or "").strip(),
        "frm_id": _clean_int(row.get("frmId")),
        "frm_name": row.get("frmName"),
        "ssn_date": _clean_date(row.get("ssnStartDate")),
        "ssn_start": row.get("ssnStartTime"),
        "ssn_end": row.get("ssnEndTime"),
        "rel_datetime": _clean_date(row.get("relDatetime")),
        "bookable": bool(row.get("bookable")),
        "raw": row,
        "last_seen_at": now_iso,          # 每次都更新「最近見到」
        # 注意:唔喺度寫 first_seen_at。靠 DB default now() 喺首次 insert 時填,
        # 之後 upsert 唔覆蓋佢(見下面 upsert 寫法),咁先 diff 得準。
    }


# ------------------------------------------------------------- run ----

def run():
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    today = now.date().isoformat()

    # 1. 攞資料
    rows = fetch_all_sessions()
    print(f"[{today}] API 攞到 {len(rows)} 條。")

    # 2. 直接 upsert 全部,唔使自己查 existing。
    #    (之前用 in_(session_key, ...) 查 existing,但 session_key 含 | 同中文,
    #     塞落 PostgREST 個 in.(...) 會整壞 URL,回 400。改用純 upsert 更穩。)
    #    靠 DB:新 row default 填 first_seen_at=now();舊 row upsert 唔覆蓋
    #    first_seen_at(payload 冇呢欄),只更新其他欄位 + last_seen_at。
    db_rows = [to_db_row(r, now_iso) for r in rows]

    # 同一批入面如果有重複 session_key,upsert 會炒。先去重(留最後一個)。
    dedup = {}
    for r in db_rows:
        dedup[r["session_key"]] = r
    db_rows = list(dedup.values())
    print(f"去重後準備寫入 {len(db_rows)} 條。")

    CH = 500
    for i in range(0, len(db_rows), CH):
        batch = db_rows[i:i + CH]
        try:
            sb.table("sessions").upsert(batch, on_conflict="session_key").execute()
        except Exception as e:
            print(f"[upsert 錯誤] 第 {i}-{i+len(batch)} 批失敗: {e!r}")
            print("  問題批第一條:",
                  json.dumps(batch[0], ensure_ascii=False, default=str)[:500])
            raise

    # 3. 搵今次新 release:first_seen_at >= 今次 run 開始時間。
    #    新 insert 嘅 row first_seen_at ≈ now(會中);舊 row 保持舊時間(唔會中)。
    res = sb.table("sessions").select("*").gte("first_seen_at", now_iso).execute()
    new_rows = res.data or []
    print(f"當中 {len(new_rows)} 個係新 release。")

    # 4. 記錄今次跑批
    sb.table("scrape_runs").insert({
        "total_fetched": len(rows),
        "new_count": len(new_rows),
        "ok": True,
        "note": f"upserted {len(db_rows)}",
    }).execute()

    # 5. 通知(第一階段:ntfy。第二階段會改成按用戶偏好 FCM)
    #    注意:new_rows 由 DB 查返嚟,用嘅係 DB 欄位名(snake_case)。
    if new_rows:
        new_rows.sort(key=lambda r: (r.get("ssn_date") or "", r.get("ssn_start") or ""))
        lines = [
            f"{r.get('venue_name')} — {r.get('frm_name')}  "
            f"{r.get('ssn_date')} {r.get('ssn_start')}-{r.get('ssn_end')}  "
            f"({(r.get('dist_name') or '').strip()})"
            for r in new_rows[:30]
        ]
        more = f"\n…仲有 {len(new_rows) - 30} 個" if len(new_rows) > 30 else ""
        notify(f"SmartPLAY 有 {len(new_rows)} 個新 release", "\n".join(lines) + more)
    else:
        print("冇新 release。")


def notify(title, body):
    if not NTFY_TOPIC:
        print(f"\n{title}\n{body}\n(未設 NTFY_TOPIC,略過推送)")
        return
    try:
        creq.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": "high"},
            impersonate="chrome", timeout=15,
        )
        print(f"已推送:{title}")
    except Exception as e:
        print(f"[錯誤] 推送失敗: {e!r}")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        # 失敗都記一筆入 DB,方便日後查
        try:
            sb.table("scrape_runs").insert({"ok": False, "note": repr(e)[:500]}).execute()
        except Exception:
            pass
        print(f"[致命錯誤] {e!r}")
        sys.exit(1)
