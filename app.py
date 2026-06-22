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

DEFAULT_WATER_GOAL = 2000     # ml
DEFAULT_EXERCISE_GOAL = 15    # 分鐘

# ── 資料讀寫 ──────────────────────────────────────────────

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "records": {}, "groups": [], "goals": {},
                "setup_state": {}, "exercise_start": {}}
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    data.setdefault('goals', {})
    data.setdefault('setup_state', {})
    data.setdefault('exercise_start', {})
    return data

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def today_str():
    return datetime.datetime.now(TAIWAN_TZ).strftime('%Y-%m-%d')

# ── 集點規則 ──────────────────────────────────────────────
# 體重記錄        +10
# 喝水記錄        +10（達到目標再 +5）
# 運動記錄        +15（達到目標再 +5）
# 飲食自評(1~5分)  分數 x 2（最高+10）
# 四項全做        +15
# 當天第一位打卡  +5

POINT_RULES = {
    'weight': 10,
    'water_base': 10,
    'water_goal_bonus': 5,
    'exercise_base': 15,
    'exercise_goal_bonus': 5,
    'diet_per_star': 2,
    'all_bonus': 15,
    'first_bonus': 5,
}

def get_user_goals(data, user_id):
    g = data['goals'].get(user_id, {})
    return {
        'weight_goal': g.get('weight_goal'),
        'water_goal': g.get('water_goal', DEFAULT_WATER_GOAL),
        'exercise_goal': g.get('exercise_goal', DEFAULT_EXERCISE_GOAL),
    }

def calc_points(record, goals):
    pts = 0
    if record.get('weight') is not None:
        pts += POINT_RULES['weight']
    if record.get('water') is not None:
        pts += POINT_RULES['water_base']
        if record['water'] >= goals['water_goal']:
            pts += POINT_RULES['water_goal_bonus']
    if record.get('exercise_min') is not None:
        pts += POINT_RULES['exercise_base']
        if record['exercise_min'] >= goals['exercise_goal']:
            pts += POINT_RULES['exercise_goal_bonus']
    if record.get('diet') is not None:
        pts += record['diet'] * POINT_RULES['diet_per_star']
    if (record.get('weight') is not None and record.get('water') is not None
            and record.get('exercise_min') is not None and record.get('diet') is not None):
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

# ── 目標設定流程 ──────────────────────────────────────────────

def start_goal_setup(user_id):
    data = load_data()
    data['setup_state'][user_id] = {'stage': 'weight'}
    save_data(data)
    return ("🎯 開始設定你的100天目標！\n\n"
            "第 1 步／3：請輸入你的目標體重（kg）\n例如：60")

def handle_goal_setup_reply(user_id, text):
    data = load_data()
    state = data['setup_state'].get(user_id)
    if not state:
        return None

    stage = state['stage']
    data['goals'].setdefault(user_id, {})

    if stage == 'weight':
        try:
            w = float(text)
        except ValueError:
            return "請輸入數字喔，例如：60"
        data['goals'][user_id]['weight_goal'] = w
        state['stage'] = 'water'
        save_data(data)
        return (f"✅ 目標體重：{w}kg\n\n"
                f"第 2 步／3：每日喝水目標，預設 {DEFAULT_WATER_GOAL}ml\n"
                f"要更改請輸入數字(ml)，維持預設請輸入「預設」")

    elif stage == 'water':
        if text in ['預設', '預設值', 'skip', '跳過']:
            water_goal = DEFAULT_WATER_GOAL
        else:
            try:
                water_goal = int(text)
            except ValueError:
                return "請輸入數字(ml)，或輸入「預設」"
        data['goals'][user_id]['water_goal'] = water_goal
        state['stage'] = 'exercise'
        save_data(data)
        return (f"✅ 喝水目標：{water_goal}ml/天\n\n"
                f"第 3 步／3：每日運動目標，預設 {DEFAULT_EXERCISE_GOAL}分鐘\n"
                f"要更改請輸入數字(分鐘)，維持預設請輸入「預設」")

    elif stage == 'exercise':
        if text in ['預設', '預設值', 'skip', '跳過']:
            exercise_goal = DEFAULT_EXERCISE_GOAL
        else:
            try:
                exercise_goal = int(text)
            except ValueError:
                return "請輸入數字(分鐘)，或輸入「預設」"
        data['goals'][user_id]['exercise_goal'] = exercise_goal
        del data['setup_state'][user_id]
        if user_id not in data['exercise_start']:
            data['exercise_start'][user_id] = today_str()
        save_data(data)
        g = data['goals'][user_id]
        return (f"✅ 運動目標：{exercise_goal}分鐘/天\n\n"
                f"🎉 100天挑戰目標設定完成！\n"
                f"⚖️ 體重目標：{g['weight_goal']}kg\n"
                f"💧 喝水目標：{g['water_goal']}ml/天\n"
                f"🏃 運動目標：{g['exercise_goal']}分鐘/天\n\n"
                f"開始打卡吧！輸入「說明」查看打卡指令 💪")
    return None

# ── 打卡邏輯 ──────────────────────────────────────────────

def get_today_record(user_id):
    data = load_data()
    today = today_str()
    return data['records'].get(today, {}).get(user_id, {})

def update_record(user_id, updates, group_id=None):
    data = load_data()
    today = today_str()
    goals = get_user_goals(data, user_id)

    if today not in data['records']:
        data['records'][today] = {}
    if user_id not in data['records'][today]:
        data['records'][today][user_id] = {}

    is_first = len(data['records'][today]) == 1

    data['records'][today][user_id].update(updates)
    rec = data['records'][today][user_id]
    pts = calc_points(rec, goals)
    if is_first and pts > 0 and not rec.get('first_checkin'):
        pts += POINT_RULES['first_bonus']
        rec['first_checkin'] = True
    rec['points'] = pts

    if group_id and group_id not in data['groups']:
        data['groups'].append(group_id)

    save_data(data)
    return rec, goals

def format_checkin_status(rec, name, goals):
    lines = [f"📋 {name} 今日打卡"]

    if rec.get('weight') is not None:
        w = rec['weight']
        wg = goals.get('weight_goal')
        diff = f"（距目標還差 {abs(round(w - wg, 1))}kg）" if wg else ""
        lines.append(f"⚖️ 體重：{w}kg{diff}")
    else:
        lines.append("⚖️ 體重：未記錄")

    if rec.get('water') is not None:
        water = rec['water']
        wg = goals['water_goal']
        mark = "✅" if water >= wg else "⚠️"
        lines.append(f"💧 喝水：{water}ml / 目標{wg}ml {mark}")
    else:
        lines.append("💧 喝水：未記錄")

    if rec.get('exercise_min') is not None:
        em = rec['exercise_min']
        eg = goals['exercise_goal']
        etype = rec.get('exercise_type', '')
        mark = "✅" if em >= eg else "⚠️"
        type_str = f"（{etype}）" if etype else ""
        lines.append(f"🏃 運動：{em}分鐘{type_str} / 目標{eg}分鐘 {mark}")
    else:
        lines.append("🏃 運動：未記錄")

    if rec.get('diet') is not None:
        stars = '⭐' * rec['diet']
        lines.append(f"🥗 飲食自評：{rec['diet']}/5 {stars}")
    else:
        lines.append("🥗 飲食：未記錄")

    pts = rec.get('points', 0)
    lines.append(f"\n⭐ 今日點數：{pts}")

    if (rec.get('weight') is not None and rec.get('water') is not None
            and rec.get('exercise_min') is not None and rec.get('diet') is not None):
        lines.append("🎉 四項全做！+15獎勵")
    if rec.get('first_checkin'):
        lines.append("🥇 今日第一位打卡！+5獎勵")

    return "\n".join(lines)

# ── 排行榜 ──────────────────────────────────────────────

def get_leaderboard():
    data = load_data()
    now = datetime.datetime.now(TAIWAN_TZ)

    totals = {}
    streaks = {}
    for date_str, day_records in data['records'].items():
        for uid, rec in day_records.items():
            pts = rec.get('points', 0)
            totals[uid] = totals.get(uid, 0) + pts

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
    goals = get_user_goals(data, user_id)
    goal_min = goals['exercise_goal']

    exercise_days = sum(
        1 for date_str, day_records in data['records'].items()
        if user_id in day_records and day_records[user_id].get('exercise_min', 0) >= goal_min
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
           "🎯 開始前，請每位成員先設定自己的100天目標：\n"
           "輸入「設定目標」開始設定（體重目標、喝水目標、運動目標）\n\n"
           "📌 設定完成後可以用以下指令打卡：\n"
           "• 體重 58.5　→ 記錄體重\n"
           "• 喝水 1800　→ 記錄喝水(ml)\n"
           "• 運動 30 跑步　→ 記錄運動分鐘+項目\n"
           "• 飲食 4　→ 飲食自評(1~5分)\n"
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

    data = load_data()
    if user_id not in data.get('users', {}):
        fetch_and_save_name(user_id, group_id)
    name = get_display_name(user_id)

    reply = None

    # 目標設定流程進行中（優先處理）
    if user_id in data.get('setup_state', {}):
        reply = handle_goal_setup_reply(user_id, text)

    elif text in ['設定目標', '目標設定', '重新設定目標']:
        reply = start_goal_setup(user_id)

    elif text in ['我的目標', '目標']:
        g = data['goals'].get(user_id)
        if g:
            reply = (f"🎯 {name} 的目標\n"
                      f"⚖️ 體重：{g.get('weight_goal', '未設定')}kg\n"
                      f"💧 喝水：{g.get('water_goal', DEFAULT_WATER_GOAL)}ml/天\n"
                      f"🏃 運動：{g.get('exercise_goal', DEFAULT_EXERCISE_GOAL)}分鐘/天\n\n"
                      f"輸入「設定目標」可以重新設定")
        else:
            reply = f"{name} 還沒設定目標喔！輸入「設定目標」開始 🎯"

    elif text.startswith('體重'):
        parts = text.split()
        if len(parts) >= 2:
            try:
                kg = float(parts[1])
                rec, goals = update_record(user_id, {'weight': kg}, group_id)
                reply = format_checkin_status(rec, name, goals)
            except ValueError:
                reply = "請輸入正確格式，例如：體重 58.5"
        else:
            reply = "請輸入體重數值，例如：體重 58.5"

    elif text.startswith('喝水'):
        parts = text.split()
        if len(parts) >= 2:
            try:
                ml = int(parts[1])
                rec, goals = update_record(user_id, {'water': ml}, group_id)
                reply = format_checkin_status(rec, name, goals)
            except ValueError:
                reply = "請輸入正確格式，例如：喝水 1800"
        else:
            reply = "請輸入喝水量，例如：喝水 1800"

    elif text.startswith('運動'):
        parts = text.split(maxsplit=2)
        if len(parts) >= 2:
            try:
                minutes = int(parts[1])
                activity = parts[2] if len(parts) >= 3 else ''
                rec, goals = update_record(
                    user_id, {'exercise_min': minutes, 'exercise_type': activity}, group_id)
                progress = get_100day_progress(user_id)
                progress_str = ""
                if progress:
                    done, elapsed = progress
                    progress_str = f"\n\n🏆 100天運動挑戰：第{elapsed}天，已達標{done}天"
                reply = format_checkin_status(rec, name, goals) + progress_str
            except ValueError:
                reply = "請輸入正確格式，例如：運動 30 跑步"
        else:
            reply = "請輸入運動分鐘數，例如：運動 30 跑步"

    elif text.startswith('飲食'):
        parts = text.split()
        if len(parts) >= 2:
            try:
                rating = int(parts[1])
                if rating < 1 or rating > 5:
                    reply = "請輸入 1～5 的分數，例如：飲食 4"
                else:
                    rec, goals = update_record(user_id, {'diet': rating}, group_id)
                    reply = format_checkin_status(rec, name, goals)
            except ValueError:
                reply = "請輸入正確格式，例如：飲食 4（1~5分）"
        else:
            reply = "請輸入飲食自評分數(1~5)，例如：飲食 4"

    elif text in ['打卡', '今日狀況', '我的打卡']:
        rec = get_today_record(user_id)
        goals = get_user_goals(data, user_id)
        if rec:
            reply = format_checkin_status(rec, name, goals)
        else:
            reply = (f"📋 {name} 今天還沒有任何記錄喔！\n\n"
                      f"可以輸入：\n• 體重 數字\n• 喝水 數字\n• 運動 分鐘數 項目\n• 飲食 1~5分")

    elif text in ['排行榜', '積分', '看排名']:
        reply = get_leaderboard()

    elif text in ['100天', '100天進度', '運動進度']:
        progress = get_100day_progress(user_id)
        if progress:
            done, elapsed = progress
            remaining = max(0, 100 - done)
            pct = min(100, int(done / 100 * 100))
            bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
            reply = (f"🏃 {name} 的100天運動挑戰\n\n"
                     f"進度：[{bar}] {pct}%\n"
                     f"已達標：{done}天\n"
                     f"第幾天：第{elapsed}天\n"
                     f"還差：{remaining}天\n\n"
                     f"{'💪 繼續加油！' if done < 100 else '🎉 恭喜完成100天挑戰！'}")
        else:
            reply = f"📌 {name} 還沒開始記錄喔！輸入「設定目標」開始 100 天挑戰！"

    elif text in ['說明', '指令', 'help', 'Help']:
        reply = ("📌 指令說明\n\n"
                 "設定目標　→ 設定100天個人目標\n"
                 "我的目標　→ 查看目前目標\n"
                 "體重 58.5　→ 記錄體重(kg)\n"
                 "喝水 1800　→ 記錄喝水(ml)\n"
                 "運動 30 跑步　→ 記錄運動分鐘+項目\n"
                 "飲食 4　→ 飲食自評(1~5分)\n"
                 "打卡　→ 今日狀況\n"
                 "排行榜　→ 積分排名\n"
                 "100天　→ 運動達標進度\n\n"
                 "⭐ 集點規則：\n"
                 "體重記錄 +10\n"
                 "喝水記錄 +10（達標再+5）\n"
                 "運動記錄 +15（達標再+5）\n"
                 "飲食自評 2~10點（分數×2）\n"
                 "四項全做 +15\n"
                 "當日第一打卡 +5")

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
           f"📋 今日依照你的個人目標打卡：\n"
           f"• 💪 運動\n"
           f"• 🥗 飲食自評\n"
           f"• 💧 喝水\n"
           f"• ⚖️ 體重記錄\n\n"
           f"還沒設定目標的人輸入「設定目標」開始！🎯")
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

    incomplete = []
    for uid in data['users']:
        rec = today_records.get(uid, {})
        complete = (rec.get('weight') is not None and rec.get('water') is not None
                    and rec.get('exercise_min') is not None and rec.get('diet') is not None)
        if not complete:
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
