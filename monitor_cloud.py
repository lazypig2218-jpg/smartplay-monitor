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
import datetime
from curl_cffi import requests as creq
from supabase import create_client

API_URL = "https://www.smartplay.lcsd.gov.hk/rest/facility/api/v1/publ/fac-book-release-log/list"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ------------------------------------------------------------- fetch ----

def fetch_all_sessions():
    """一個 request 用大 pageSize 攞晒(實測 2000 已足夠 1610 條)。"""
    r = creq.get(
        API_URL,
        params={"pageNum": 1, "pageSize": 3000},
        impersonate="chrome",
        timeout=45,
        headers={
            "Accept": "application/json",
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.smartplay.lcsd.gov.hk/facilities/watchlist",
        },
    )
    r.raise_for_status()
    payload = r.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"API code={payload.get('code')} msg={payload.get('message')}")
    return (payload.get("data") or {}).get("list") or []


# ------------------------------------------------------------- map ----

def session_key(row):
    return "|".join(str(row.get(k, "")) for k in
                    ("venueId", "frmId", "ssnStartDate", "ssnStartTime", "ssnEndTime"))


def to_db_row(row, now_iso):
    """API record -> Supabase row。"""
    return {
        "session_key": session_key(row),
        "fat_id": row.get("fatId"),
        "fat_name": row.get("fatName"),
        "venue_id": row.get("venueId"),
        "venue_name": row.get("venueName"),
        "dist_code": row.get("distCode"),
        "dist_name": (row.get("distName") or "").strip(),
        "frm_id": row.get("frmId"),
        "frm_name": row.get("frmName"),
        "ssn_date": row.get("ssnStartDate"),
        "ssn_start": row.get("ssnStartTime"),
        "ssn_end": row.get("ssnEndTime"),
        "rel_datetime": row.get("relDatetime"),
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
    keys_today = [session_key(r) for r in rows]
    print(f"[{today}] API 攞到 {len(rows)} 條。")

    # 2. 未 upsert 前,問 DB「呢批 key 邊啲已經存在」
    #    咁就知邊啲係新(DB 冇嘅)。分批問,避免 URL 過長。
    existing = set()
    CH = 500
    for i in range(0, len(keys_today), CH):
        chunk = keys_today[i:i + CH]
        res = sb.table("sessions").select("session_key").in_("session_key", chunk).execute()
        existing.update(r["session_key"] for r in res.data)

    new_keys = [k for k in keys_today if k not in existing]
    new_rows = [r for r in rows if session_key(r) in set(new_keys)]
    print(f"當中 {len(new_keys)} 個係新 release。")

    # 3. Upsert 全部(on_conflict=session_key)。
    #    first_seen_at 唔喺 payload 入面,所以:
    #      - 新 row:DB default now() 填 first_seen_at
    #      - 舊 row:upsert 更新其他欄位 + last_seen_at,first_seen_at 保持不變
    db_rows = [to_db_row(r, now_iso) for r in rows]
    for i in range(0, len(db_rows), CH):
        sb.table("sessions").upsert(
            db_rows[i:i + CH], on_conflict="session_key"
        ).execute()

    # 4. 記錄今次跑批
    sb.table("scrape_runs").insert({
        "total_fetched": len(rows),
        "new_count": len(new_keys),
        "ok": True,
        "note": f"{len(existing)} existing",
    }).execute()

    # 5. 通知(第一階段:ntfy。第二階段會改成按用戶偏好 FCM)
    if new_rows:
        new_rows.sort(key=lambda r: (r.get("ssnStartDate", ""), r.get("ssnStartTime", "")))
        lines = [
            f"{r.get('venueName')} — {r.get('frmName')}  "
            f"{r.get('ssnStartDate')} {r.get('ssnStartTime')}-{r.get('ssnEndTime')}  "
            f"({(r.get('distName') or '').strip()})"
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
