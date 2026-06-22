import os
import json
import datetime
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent,
    JoinEvent, MemberJoinedEvent
)
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DATA_FILE = 'data.json'
TAIWAN_TZ = pytz.timezone('Asia/Taipei')

# ── 資料讀寫 ──────────────────────────────────────────────

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "records": {}, "groups": [], "exercise_start": {}}
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def today_str():
    return datetime.datetime.now(TAIWAN_TZ).strftime('%Y-%m-%d')

# ── 集點規則 ──────────────────────────────────────────────
# 體重記錄   +10
# 喝水記錄   +10
# 運動完成   +15
# 飲食完成   +10
# 四項全做   +15（額外獎勵）
# 當天第一位打卡 +5

POINT_RULES = {
    'weight': 10,
    'water': 10,
    'exercise': 15,
    'diet': 10,
    'all_bonus': 15,
    'first_bonus': 5,
}

def calc_points(record):
    pts = 0
    if record.get('weight') is not None:
        pts += POINT_RULES['weight']
    if record.get('water') is not None:
        pts += POINT_RULES['water']
    if record.get('exercise'):
        pts += POINT_RULES['exercise']
    if record.get('diet'):
        pts += POINT_RULES['diet']
    if (record.get('weight') is not None and
        record.get('water') is not None and
        record.get('exercise') and
        record.get('diet')):
        pts += POINT_RULES['all_bonus']
    return pts

# ── 使用者名稱 ──────────────────────────────────────────────

def get_display_name(user_id):
    data = load_data()
    return data['users'].get(user_id, {}).get('name', '成員')

def fetch_and_save_name(user_id, group_id=None):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            if group_id:
                profile = line_bot_api.get_group_member_profile(group_id, user_id)
            else:
                profile = line_bot_api.get_profile(user_id)
            data = load_data()
            if user_id not in data['users']:
                data['users'][user_id] = {}
            data['users'][user_id]['name'] = profile.display_name
            save_data(data)
            return profile.display_name
    except Exception:
        return '成員'

# ── 打卡邏輯 ──────────────────────────────────────────────

def get_today_record(user_id):
    data = load_data()
    today = today_str()
    return data['records'].get(today, {}).get(user_id, {})

def update_record(user_id, field, value, group_id=None):
    data = load_data()
    today = today_str()

    if today not in data['records']:
        data['records'][today] = {}
    if user_id not in data['records'][today]:
        data['records'][today][user_id] = {}

    # 檢查是否為當天第一位打卡
    is_first = len(data['records'][today]) == 1  # 已經新增自己了

    data['records'][today][user_id][field] = value

    # 重新計算點數
    rec = data['records'][today][user_id]
    pts = calc_points(rec)
    if is_first and pts > 0:
        pts += POINT_RULES['first_bonus']
        rec['first_checkin'] = True
    rec['points'] = pts

    # 確保 exercise_start 有記錄
    if field == 'exercise' and value and user_id not in data.get('exercise_start', {}):
        data.setdefault('exercise_start', {})[user_id] = today

    # 記錄群組
    if group_id and group_id not in data['groups']:
        data['groups'].append(group_id)

    save_data(data)
    return rec

def format_checkin_status(rec, name):
    weight = f"✅ 體重：{rec['weight']}kg" if rec.get('weight') is not None else "❌ 體重：未記錄"
    water = f"✅ 喝水：{rec['water']}ml" if rec.get('water') is not None else "❌ 喝水：未記錄"
    exercise = "✅ 運動：完成" if rec.get('exercise') else "❌ 運動：未完成"
    diet = "✅ 飲食：完成" if rec.get('diet') else "❌ 飲食：未完成"
    pts = rec.get('points', 0)
    bonus = " 🎉 四項全做！+15獎勵" if (rec.get('weight') is not None and rec.get('water') is not None and rec.get('exercise') and rec.get('diet')) else ""
    first = " 🥇 今日第一位打卡！+5獎勵" if rec.get('first_checkin') else ""
    return f"📋 {name} 今日打卡\n{weight}\n{water}\n{exercise}\n{diet}\n\n⭐ 今日點數：{pts}{bonus}{first}"

# ── 排行榜 ──────────────────────────────────────────────

def get_leaderboard():
    data = load_data()
    now = datetime.datetime.now(TAIWAN_TZ)

    # 計算本週開始（週一）
    week_start = (now - datetime.timedelta(days=now.weekday())).strftime('%Y-%m-%d')

    totals = {}
    streaks = {}
    for date_str, day_records in data['records'].items():
        for uid, rec in day_records.items():
            pts = rec.get('points', 0)
            totals[uid] = totals.get(uid, 0) + pts

    # 計算連續天數
    for uid in totals:
        streak = 0
        check_date = now
        while True:
            d = check_date.strftime('%Y-%m-%d')
            day_data = data['records'].get(d, {})
            if uid in day_data and day_data[uid].get('points', 0) > 0:
                streak += 1
                check_date -= datetime.timedelta(days=1)
            else:
                break
        streaks[uid] = streak

    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    medals = ['🥇', '🥈', '🥉']
    lines = ["🏆 健康排行榜\n"]
    for i, (uid, pts) in enumerate(ranked):
        name = data['users'].get(uid, {}).get('name', '成員')
        medal = medals[i] if i < 3 else f"{i+1}."
        streak = streaks.get(uid, 0)
        fire = f" 🔥{streak}天" if streak > 0 else ""
        lines.append(f"{medal} {name}　{pts}點{fire}")

    return "\n".join(lines)

def get_100day_progress(user_id):
    data = load_data()
    start = data.get('exercise_start', {}).get(user_id)
    if not start:
        return None
    start_date = datetime.datetime.strptime(start, '%Y-%m-%d').date()
    today = datetime.datetime.now(TAIWAN_TZ).date()

    # 計算有運動的天數
    exercise_days = sum(
        1 for date_str, day_records in data['records'].items()
        if user_id in day_records and day_records[user_id].get('exercise')
    )
    elapsed = (today - start_date).days + 1
    return exercise_days, elapsed

# ── Webhook 處理 ──────────────────────────────────────────────

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(JoinEvent)
def handle_join(event):
    group_id = event.source.group_id
    data = load_data()
    if group_id not in data['groups']:
        data['groups'].append(group_id)
        save_data(data)

    msg = ("👋 大家好！我是健康打卡Bot！\n\n"
           "📌 可以用以下指令打卡：\n"
           "• 體重 58.5　→ 記錄體重\n"
           "• 喝水 1800　→ 記錄喝水(ml)\n"
           "• 運動　→ 運動打卡\n"
           "• 飲食　→ 飲食打卡\n"
           "• 打卡　→ 查看今日狀況\n"
           "• 排行榜　→ 查看積分\n"
           "• 100天　→ 查看運動進度\n\n"
           "每天早上7點提醒，晚上9點催打卡 💪\n"
           "週日發布週排行榜 🏆")
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[TextMessage(text=msg)])
        )

@handler.add(MemberJoinedEvent)
def handle_member_join(event):
    for member in event.joined.members:
        if member.type == 'user':
            fetch_and_save_name(member.user_id, event.source.group_id)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    group_id = getattr(event.source, 'group_id', None)
    text = event.message.text.strip()

    # 嘗試取得名字
    data = load_data()
    if user_id not in data.get('users', {}):
        fetch_and_save_name(user_id, group_id)
    name = get_display_name(user_id)

    reply = None

    # 體重記錄：「體重 58.5」
    if text.startswith('體重'):
        parts = text.split()
        if len(parts) >= 2:
            try:
                kg = float(parts[1])
                rec = update_record(user_id, 'weight', kg, group_id)
                reply = f"✅ {name} 體重記錄：{kg}kg\n" + format_checkin_status(rec, name)
            except ValueError:
                reply = "請輸入正確格式，例如：體重 58.5"
        else:
            reply = "請輸入體重數值，例如：體重 58.5"

    # 喝水記錄：「喝水 1800」
    elif text.startswith('喝水'):
        parts = text.split()
        if len(parts) >= 2:
            try:
                ml = int(parts[1])
                rec = update_record(user_id, 'water', ml, group_id)
                reply = f"💧 {name} 喝水記錄：{ml}ml\n" + format_checkin_status(rec, name)
            except ValueError:
                reply = "請輸入正確格式，例如：喝水 1800"
        else:
            reply = "請輸入喝水量，例如：喝水 1800"

    # 運動打卡
    elif text in ['運動', '✅運動', '運動完成', '今天運動了']:
        rec = update_record(user_id, 'exercise', True, group_id)
        progress = get_100day_progress(user_id)
        progress_str = ""
        if progress:
            done, elapsed = progress
            progress_str = f"\n🏃 100天運動：第{elapsed}天，已完成{done}天"
        reply = f"🏃 {name} 運動打卡！\n" + format_checkin_status(rec, name) + progress_str

    # 飲食打卡
    elif text in ['飲食', '✅飲食', '飲食正常', '今天飲食正常']:
        rec = update_record(user_id, 'diet', True, group_id)
        reply = f"🥗 {name} 飲食打卡！\n" + format_checkin_status(rec, name)

    # 查看今日狀況
    elif text in ['打卡', '今日狀況', '我的打卡']:
        rec = get_today_record(user_id)
        if rec:
            reply = format_checkin_status(rec, name)
        else:
            reply = f"📋 {name} 今天還沒有任何記錄喔！\n\n可以輸入：\n• 體重 數字\n• 喝水 數字\n• 運動\n• 飲食"

    # 排行榜
    elif text in ['排行榜', '積分', '看排名']:
        reply = get_leaderboard()

    # 100天運動進度
    elif text in ['100天', '100天進度', '運動進度']:
        progress = get_100day_progress(user_id)
        if progress:
            done, elapsed = progress
            remaining = 100 - done
            pct = int(done / 100 * 100)
            bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
            reply = (f"🏃 {name} 的100天運動挑戰\n\n"
                     f"進度：[{bar}] {pct}%\n"
                     f"已運動：{done}天\n"
                     f"第幾天：第{elapsed}天\n"
                     f"還差：{remaining}天\n\n"
                     f"{'💪 繼續加油！' if done < 100 else '🎉 恭喜完成100天挑戰！'}")
        else:
            reply = f"📌 {name} 還沒開始記錄運動喔！\n輸入「運動」開始第一天！"

    # 說明
    elif text in ['說明', '指令', 'help', 'Help']:
        reply = ("📌 指令說明\n\n"
                 "體重 58.5　→ 記錄體重(kg)\n"
                 "喝水 1800　→ 記錄喝水(ml)\n"
                 "運動　→ 運動打卡\n"
                 "飲食　→ 飲食打卡\n"
                 "打卡　→ 今日狀況\n"
                 "排行榜　→ 積分排名\n"
                 "100天　→ 運動進度\n\n"
                 "⭐ 集點規則：\n"
                 "體重記錄 +10點\n"
                 "喝水記錄 +10點\n"
                 "運動完成 +15點\n"
                 "飲食完成 +10點\n"
                 "四項全做 +15點（獎勵）\n"
                 "當日第一打卡 +5點")

    if reply:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=event.reply_token,
                                    messages=[TextMessage(text=reply)])
            )

# ── 排程提醒 ──────────────────────────────────────────────

def send_morning_reminder():
    data = load_data()
    if not data['groups']:
        return
    today = today_str()
    now = datetime.datetime.now(TAIWAN_TZ)
    weekday_names = ['週一', '週二', '週三', '週四', '週五', '週六', '週日']
    weekday = weekday_names[now.weekday()]
    msg = (f"☀️ 早安！今天是 {today} {weekday}\n\n"
           f"📋 今日健康目標：\n"
           f"• 💪 完成運動\n"
           f"• 🥗 均衡飲食\n"
           f"• 💧 喝水 2000ml\n"
           f"• ⚖️ 記錄體重\n\n"
           f"開始打卡吧！輸入「說明」查看指令 🎯")
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for group_id in data['groups']:
            try:
                api.push_message(PushMessageRequest(to=group_id,
                                                    messages=[TextMessage(text=msg)]))
            except Exception as e:
                print(f"早安提醒失敗 {group_id}: {e}")

def send_evening_reminder():
    data = load_data()
    if not data['groups']:
        return
    today = today_str()
    today_records = data['records'].get(today, {})

    # 找出還沒完整打卡的成員
    incomplete = []
    for uid in data['users']:
        rec = today_records.get(uid, {})
        if rec.get('points', 0) < 50:  # 未完成四項
            incomplete.append(data['users'][uid].get('name', '成員'))

    if not incomplete:
        msg = "🎉 今天大家都完成打卡了！太厲害了！繼續保持 💪"
    else:
        names = '、'.join(incomplete)
        msg = (f"🌙 晚上好！還剩 1 小時打卡喔！\n\n"
               f"⏰ 還沒完成的：{names}\n\n"
               f"趕快打卡，別讓連續天數斷掉！🔥")

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for group_id in data['groups']:
            try:
                api.push_message(PushMessageRequest(to=group_id,
                                                    messages=[TextMessage(text=msg)]))
            except Exception as e:
                print(f"晚安提醒失敗 {group_id}: {e}")

def send_weekly_leaderboard():
    data = load_data()
    if not data['groups']:
        return
    lb = get_leaderboard()
    msg = f"📊 本週健康排行榜\n\n{lb}\n\n下週繼續加油！💪"
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for group_id in data['groups']:
            try:
                api.push_message(PushMessageRequest(to=group_id,
                                                    messages=[TextMessage(text=msg)]))
            except Exception as e:
                print(f"週排行榜失敗 {group_id}: {e}")

# ── 啟動 ──────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=TAIWAN_TZ)
scheduler.add_job(send_morning_reminder, 'cron', hour=7, minute=0)
scheduler.add_job(send_evening_reminder, 'cron', hour=21, minute=0)
scheduler.add_job(send_weekly_leaderboard, 'cron', day_of_week='sun', hour=20, minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
