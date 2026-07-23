"""
SmartPlay 監測 —— push 通知
============================
時間表(香港時間):
    01:00  康文署 update
    02:00  scrape_smartplay.yml 去 scrape
    ??:00  呢個 script push(每個鐘跑一次,只推俾設定咗嗰個鐘嘅用戶)
    07:00  開放搶場

即係用戶喺開放前幾個鐘就知有咩新場,有時間準備 —— 呢個先係個 app 嘅價值。

流程:
  1. 攞晒 enabled 嘅 watches
  2. 逐條 match sessions(日期 + 運動細項 + 地區 + 場地 + 時段)
  3. 只要「新」嘅 —— 即係:
       • session 係喺 watch 建立【之後】先出現(first_seen_at >= created_at)
       • 而且未 push 過(唔喺 watch_hits)
  4. Expo Push API 送通知(免費,唔使 Firebase)
  5. 記入 watch_hits(下次唔會再報同一個場)
  6. expire_watches() 清走用場日已經過咗嘅 watch

環境變數(GitHub Secrets):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY

參數:
  --hour N   當作而家係香港時間 N 點(測試用)
  --all      唔理用戶設定嘅時間,全部都 push(手動 trigger 用)
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
from supabase import create_client

HK = timezone(timedelta(hours=8))
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ─────────────────────────────────────────────────────────────
# 設施分類 —— 同 app 個 src/constants/smartplay.js 一致
# ⚠ 次序好重要:名有包含關係嘅要排前
#   (「人造草地美式足球場」含「足球」;「滑浪風帆」含「風帆」)
# ─────────────────────────────────────────────────────────────
TYPES = [
    # football
    ("fb_free",     ["免費足球"]),
    ("fb_us",       ["美式足球"]),
    ("fb_standard", ["標準足球場"]),
    ("fb_turf_sm",  ["小型人造草地足球"]),
    ("fb_turf",     ["人造草地足球"]),
    ("fb_7",        ["七人硬地足球"]),
    ("fb_5",        ["五人硬地足球"]),
    ("fb_small",    ["小型足球場"]),
    # tennis
    ("tn_free_prac", ["免費網球場練習場"]),
    ("tn_prac",      ["網球練習場"]),
    ("tn_hard",      ["硬地網球場"]),
    ("tn_court",     ["網球場"]),
    ("tn_main",      ["網球主場"]),
    # badminton
    ("bd_free", ["免費羽毛球"]),
    ("bd_out", ["戶外羽毛球"]),
    ("bd_ac",  ["羽毛球場 (空調)"]),
    ("bd_in",  ["羽毛球場"]),
    # basketball
    ("bk_free", ["免費籃球"]),
    ("bk_prac", ["籃球場練習場"]),
    ("bk_out",  ["戶外籃球"]),
    ("bk_ac",   ["籃球場 (空調)"]),
    ("bk_in",   ["籃球場"]),
    # table tennis
    ("tt_free",    ["免費乒乓球"]),
    ("tt_machine", ["乒乓球檯及發球機"]),
    ("tt_ac",      ["乒乓球檯 (空調)"]),
    ("tt_noac",    ["乒乓球檯 (無空調)"]),
    ("tt_other",   ["乒乓球檯"]),
    # squash
    ("sq_ac",   ["壁球場 (空調)"]),
    ("sq_in",   ["壁球場"]),
    ("sq_show", ["壁球表演場"]),
    # volleyball
    ("vb_beach_free", ["免費沙灘排球"]),
    ("vb_free",       ["免費排球"]),
    ("vb_beach", ["沙灘排球"]),
    ("vb_out",   ["戶外排球"]),
    ("vb_ac",    ["排球場 (空調)"]),
    ("vb_in",    ["排球場"]),
    # baseball
    ("bb_free", ["免費棒球"]),
    ("bb_prac", ["棒球練習場"]),
    ("bb",      ["棒球"]),
    # 其他陸上
    ("ar_free", ["免費箭藝"]),
    ("ar",      ["箭藝"]),
    ("pk_free", ["戶外匹克球場 (不收費)"]),
    ("pk_out",  ["匹克球"]),
    ("cl_free", ["免費攀登"]),
    ("cl_out",  ["攀登"]),
    ("gf_tee",  ["高爾夫"]),
    ("lb_in",   ["室內草地滾球"]),
    ("lb_out",  ["草地滾球"]),
    ("gb_free", ["免費門球場"]),
    ("gb",      ["門球"]),
    ("hb_beach",["沙灘手球"]),
    ("hb_free", ["免費手球"]),
    ("hb",      ["手球"]),
    ("nb_free", ["免費投球"]),
    ("nb",      ["投球"]),
    ("bl_us",    ["美式桌球"]),
    ("bl_uk",    ["英式桌球"]),
    ("bl_crown", ["克朗桌球"]),
    ("ck_free", ["免費板球"]),
    ("ck_prac", ["板球練習場"]),
    ("ck_hard", ["硬地板球"]),
    ("ck",      ["板球"]),
    ("hk_turf", ["人造草地曲棍球"]),
    ("hk",      ["曲棍球"]),
    ("rh",      ["滾軸曲棍球"]),
    ("rg",      ["橄欖球"]),
    ("kb",      ["健球"]),
    ("kf",      ["合球"]),
    ("db1",     ["閃避球"]),
    ("db2",     ["躲避盤"]),
    ("tb",      ["巧固球"]),
    ("dn",      ["舞蹈"]),
    ("mu",      ["多用途活動", "活動室"]),
    ("rn",      ["繩網"]),
    ("tc",      ["場地單車"]),
    ("amp",     ["露天劇場"]),
    # 水上(「滑浪」要排喺「風帆」前)
    ("sf", ["滑浪"]),
    ("ws", ["風帆"]),
    ("ca", ["獨木舟"]),
]

# 全套 sport_key -> 顯示名(同 app 個 SPORTS 一致;漏咗嘅話
# push 標題會出返個英文 raw key,好肉酸)
SPORT_LABEL = {
    "football": "⚽ 足球", "tennis": "🎾 網球", "badminton": "🏸 羽毛球",
    "basketball": "🏀 籃球", "tabletennis": "🏓 乒乓球", "squash": "🎯 壁球",
    "volleyball": "🏐 排球", "baseball": "⚾ 棒球",
    "archery": "🏹 箭藝", "pickleball": "🥒 匹克球", "climbing": "🧗 攀登牆",
    "golf": "⛳ 高爾夫球", "lawnbowls": "🎳 草地滾球", "gateball": "🥎 門球",
    "handball": "🤾 手球", "netball": "🏐 投球", "billiards": "🎱 桌球",
    "cricket": "🏏 板球", "hockey": "🏑 曲棍球", "rollerhockey": "🛼 滾軸曲棍球",
    "rugby": "🏉 橄欖球", "kinball": "🏐 健球", "korfball": "🏐 合球",
    "dodgeball": "🥏 躲避盤/閃避球", "tchoukball": "🏐 巧固球",
    "dance": "💃 舞蹈", "multi": "🏟️ 多用途活動", "ropenet": "🪢 繩網活動",
    "trackcycle": "🚴 場地單車", "amphitheatre": "🎭 露天劇場",
    "surfing": "🏄 滑浪風帆", "windsurf": "⛵ 風帆", "canoe": "🛶 獨木舟",
}

# 健身器材/健身室係「入場制」,冇人會監測;月票/套票係優惠組合產品。
HIDDEN = ["月票", "套票", "健身器材", "健身室"]


def type_of(fat_name: str):
    """一個 fat_name 對應邊個細項 key(第一個夾中嘅贏)。"""
    if not fat_name:
        return None
    for key, kws in TYPES:
        if any(k in fat_name for k in kws):
            return key
    return None


def is_hidden(fat_name: str) -> bool:
    return any(k in (fat_name or "") for k in HIDDEN)


def parse_ts(s):
    """Supabase timestamptz -> aware datetime。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
def matches(watch, s) -> bool:
    """一條 session 啱唔啱一條 watch?"""
    if not s.get("bookable"):
        return False
    if is_hidden(s.get("fat_name")):
        return False

    if s.get("ssn_date") not in (watch.get("play_dates") or []):
        return False

    hours = watch.get("hours") or []
    if hours and s.get("ssn_start") not in hours:
        return False

    dists = watch.get("dist_codes") or []
    if dists and s.get("dist_code") not in dists:
        return False

    venues = watch.get("venue_ids") or []
    if venues and s.get("venue_id") not in venues:
        return False

    tkeys = watch.get("type_keys") or []
    if tkeys and type_of(s.get("fat_name")) not in tkeys:
        return False

    return True


def send_expo(messages):
    """
    Expo Push API,一次最多 100 條。
    回傳 (成功數, 壞 token set, 成功索引 set) ——
    最後嗰個俾主流程知邊幾條 message 真係送成功,先好記入
    notifications_sent(「最近通知」清單淨係想顯示真係送咗嘅)。
    """
    ok = 0
    dead_tokens = set()
    sent_idx = set()

    for i in range(0, len(messages), 100):
        chunk = messages[i:i + 100]
        try:
            r = requests.post(
                EXPO_PUSH_URL,
                json=chunk,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            print(f"  ❌ Expo push 出錯: {e!r}")
            continue

        for j, (msg, res) in enumerate(zip(chunk, body.get("data", []))):
            if res.get("status") == "ok":
                ok += 1
                sent_idx.add(i + j)
            else:
                err = (res.get("details") or {}).get("error")
                print(f"  ⚠ push 失敗 {msg['to'][:24]}… : {res.get('message')}")
                if err == "DeviceNotRegistered":
                    dead_tokens.add(msg["to"])

    return ok, dead_tokens, sent_idx


# ─────────────────────────────────────────────────────────────
def main():
    now_hk = datetime.now(HK)

    # 而家係香港幾點(可以用 --hour 覆蓋嚟測試)
    hour = now_hk.hour
    push_all = "--all" in sys.argv
    if "--hour" in sys.argv:
        hour = int(sys.argv[sys.argv.index("--hour") + 1])

    print(f"=== push 通知 · 香港 {now_hk:%Y-%m-%d} {hour:02d} 點"
          f"{' (--all)' if push_all else ''} ===\n")

    # 1. enabled 嘅 watches
    watches = (sb.table("watches").select("*")
               .eq("enabled", True).execute().data or [])
    if not watches:
        print("冇 watch,收工。")
        expire()
        return
    print(f"{len(watches)} 條 watch")

    # 2. 用戶 push token + 佢哋揀咗幾點收
    uids = list({w["user_id"] for w in watches})
    users = (sb.table("app_users").select("id, push_token, push_hour")
             .in_("id", uids).execute().data or [])

    # ★ 只服務「而家啱啱係佢設定時間」嗰班人
    token_of = {}
    for u in users:
        if not u.get("push_token"):
            continue
        if push_all or (u.get("push_hour", 6) == hour):
            token_of[u["id"]] = u["push_token"]

    if not token_of:
        print(f"冇人揀咗 {hour:02d} 點收通知,收工。")
        expire()
        return
    print(f"{len(token_of)} 個用戶揀咗 {hour:02d} 點收")

    # 唔關呢個鐘事嘅 watch,今次唔使理
    watches = [w for w in watches if w["user_id"] in token_of]
    print(f"{len(watches)} 條 watch 要處理")

    # 3. 相關日期嘅 sessions(一次過攞,唔好逐條 watch 打 DB)
    all_dates = sorted({d for w in watches for d in (w.get("play_dates") or [])})
    if not all_dates:
        print("冇日期,收工。")
        expire()
        return

    sessions = []
    PAGE = 1000
    frm = 0
    while True:
        rows = (sb.table("sessions").select("*")
                .in_("ssn_date", all_dates)
                .eq("bookable", True)
                .range(frm, frm + PAGE - 1).execute().data or [])
        sessions.extend(rows)
        if len(rows) < PAGE:
            break
        frm += PAGE
    print(f"{len(sessions)} 條相關 session\n")

    # 4. 已 push 過嘅(避免重複報同一個場)
    wids = [w["id"] for w in watches]
    hits = (sb.table("watch_hits").select("watch_id, session_key")
            .in_("watch_id", wids).execute().data or [])
    pushed = defaultdict(set)
    for h in hits:
        pushed[h["watch_id"]].add(h["session_key"])

    # 5. 逐條 watch match
    messages = []
    new_hits = []
    notif_records = []   # 同 messages 一一對應,用嚟寫 notifications_sent

    for w in watches:
        token = token_of.get(w["user_id"])
        created = parse_ts(w.get("created_at"))
        already = pushed[w["id"]]

        fresh = []
        for s in sessions:
            if s["session_key"] in already:
                continue
            if not matches(w, s):
                continue
            # ★ 只要 watch 建立【之後】先出現嘅場 ——
            #   唔好一 save 就將舊 release 全部 push(app 卡片已經顯示緊)
            seen = parse_ts(s.get("first_seen_at"))
            if created and seen and seen < created:
                # 舊 release:記低當已處理,但唔 push
                new_hits.append({"watch_id": w["id"],
                                 "session_key": s["session_key"]})
                already.add(s["session_key"])
                continue
            fresh.append(s)

        if not fresh:
            continue

        sport = SPORT_LABEL.get(w.get("sport_key"), w.get("sport_key"))
        venues = sorted({s["venue_name"] for s in fresh})
        dates = sorted({s["ssn_date"] for s in fresh})

        title = f"{sport} · {len(fresh)} 個新場"
        head = "、".join(venues[:2])
        if len(venues) > 2:
            head += f" 等 {len(venues)} 個場"
        dpart = "、".join(d[5:].replace("-", "/") for d in dates[:3])
        body = f"{head} · {dpart} 有人退場,快啲去訂!"

        print(f"  📣 {title} -> {head}")

        messages.append({
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "data": {"watchId": w["id"], "count": len(fresh)},
        })
        notif_records.append({
            "user_id": w["user_id"],
            "watch_id": w["id"],
            "sport_key": w.get("sport_key"),
            "summary": body,
            "session_count": len(fresh),
        })

        for s in fresh:
            new_hits.append({"watch_id": w["id"],
                             "session_key": s["session_key"]})

    # 6. 送
    if messages:
        print(f"\n送緊 {len(messages)} 個通知…")
        ok, dead, sent_idx = send_expo(messages)
        print(f"成功 {ok} / {len(messages)}")
        for t in dead:
            print(f"  清走死 token {t[:24]}…")
            sb.table("app_users").update({"push_token": None}) \
              .eq("push_token", t).execute()

        # 記低「最近通知」——淨係記真係送成功嗰啲
        sent_records = [notif_records[i] for i in sent_idx]
        if sent_records:
            try:
                sb.table("notifications_sent").insert(sent_records).execute()
                print(f"記低 {len(sent_records)} 條「最近通知」")
            except Exception as e:
                print(f"  ⚠ 寫 notifications_sent 失敗(唔緊要): {e!r}")
    else:
        print("\n冇新場,唔使 push。")

    # 7. 記低(唔好重複 push)
    if new_hits:
        for i in range(0, len(new_hits), 500):
            sb.table("watch_hits").upsert(
                new_hits[i:i + 500],
                on_conflict="watch_id,session_key",
            ).execute()
        print(f"記低 {len(new_hits)} 條 hit")

    expire()


def expire():
    """用場日全部過咗嘅 watch,自動清走。"""
    try:
        r = sb.rpc("expire_watches").execute()
        n = r.data if isinstance(r.data, int) else 0
        print(f"\n清走 {n} 條過期 watch")
    except Exception as e:
        print(f"\n⚠ expire_watches 出錯: {e!r}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ 出錯: {e!r}")
        sys.exit(1)
