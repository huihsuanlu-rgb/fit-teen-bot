import os
import datetime
import psycopg2
import psycopg2.extras
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
DATABASE_URL = os.environ.get('DATABASE_URL', '')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

TAIWAN_TZ = pytz.timezone('Asia/Taipei')
DEFAULT_WATER_GOAL = 2000
DEFAULT_EXERCISE_GOAL = 15

# ── 資料庫連線 ──────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT
                );
                CREATE TABLE IF NOT EXISTS groups_list (
                    group_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS goals (
                    user_id TEXT PRIMARY KEY,
                    weight_goal REAL,
                    water_goal INT DEFAULT 2000,
                    exercise_goal INT DEFAULT 15
                );
                CREATE TABLE IF NOT EXISTS setup_state (
                    user_id TEXT PRIMARY KEY,
                    stage TEXT
                );
                CREATE TABLE IF NOT EXISTS exercise_start (
                    user_id TEXT PRIMARY KEY,
                    start_date TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_records (
                    date TEXT,
                    user_id TEXT,
                    weight REAL,
                    water INT,
                    exercise_min INT,
                    exercise_type TEXT,
                    points INT DEFAULT 0,
                    first_checkin BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (date, user_id)
                );
            """)
        conn.commit()

# ── 集點規則 ──────────────────────────────────────────────
POINT_RULES = {
    'weight': 10,
    'water_base': 10,
    'water_goal_bonus': 5,
    'exercise_base': 15,
    'exercise_goal_bonus': 5,
    'all_bonus': 15,
    'first_bonus': 5,
}

def today_str():
    return datetime.datetime.now(TAIWAN_TZ).strftime('%Y-%m-%d')

def get_user_goals(user_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM goals WHERE user_id = %s", (user_id,))
            g = cur.fetchone()
    if g:
        return {
            'weight_goal': g['weight_goal'],
            'water_goal': g['water_goal'] or DEFAULT_WATER_GOAL,
            'exercise_goal': g['exercise_goal'] or DEFAULT_EXERCISE_GOAL,
        }
    return {'weight_goal': None, 'water_goal': DEFAULT_WATER_GOAL, 'exercise_goal': DEFAULT_EXERCISE_GOAL}

def calc_points(rec, goals):
    pts = 0
    if rec.get('weight') is not None:
        pts += POINT_RULES['weight']
    if rec.get('water') is not None:
        pts += POINT_RULES['water_base']
        if rec['water'] >= goals['water_goal']:
            pts += POINT_RULES['water_goal_bonus']
    if rec.get('exercise_min') is not None:
        pts += POINT_RULES['exercise_base']
        if rec['exercise_min'] >= goals['exercise_goal']:
            pts += POINT_RULES['exercise_goal_bonus']
    if (rec.get('weight') is not None and rec.get('water') is not None
            and rec.get('exercise_min') is not None):
        pts += POINT_RULES['all_bonus']
    return pts

# ── 使用者 ──────────────────────────────────────────────

def get_display_name(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    return row[0] if row else '成員'

def fetch_and_save_name(user_id, group_id=None):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            if group_id:
                profile = line_bot_api.get_group_member_profile(group_id, user_id)
            else:
                profile = line_bot_api.get_profile(user_id)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO users (user_id, name) VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name
                    """, (user_id, profile.display_name))
                conn.commit()
            return profile.display_name
    except Exception:
        return '成員'

def ensure_user(user_id, group_id=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
            exists = cur.fetchone()
    if not exists:
        fetch_and_save_name(user_id, group_id)

# ── 目標設定流程 ──────────────────────────────────────────────

def get_setup_stage(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stage FROM setup_state WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    return row[0] if row else None

def set_setup_stage(user_id, stage):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if stage is None:
                cur.execute("DELETE FROM setup_state WHERE user_id = %s", (user_id,))
            else:
                cur.execute("""
                    INSERT INTO setup_state (user_id, stage) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET stage = EXCLUDED.stage
                """, (user_id, stage))
        conn.commit()

def start_goal_setup(user_id):
    set_setup_stage(user_id, 'weight')
    return ("🎯 開始設定你的100天目標！\n\n"
            "第 1 步／3：請輸入你的目標體重（kg）\n例如：60")

def handle_goal_setup_reply(user_id, text):
    stage = get_setup_stage(user_id)
    if not stage:
        return None

    if stage == 'weight':
        try:
            w = float(text)
        except ValueError:
            return "請輸入數字喔，例如：60"
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO goals (user_id, weight_goal, water_goal, exercise_goal)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET weight_goal = EXCLUDED.weight_goal
                """, (user_id, w, DEFAULT_WATER_GOAL, DEFAULT_EXERCISE_GOAL))
            conn.commit()
        set_setup_stage(user_id, 'water')
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
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE goals SET water_goal = %s WHERE user_id = %s", (water_goal, user_id))
            conn.commit()
        set_setup_stage(user_id, 'exercise')
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
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE goals SET exercise_goal = %s WHERE user_id = %s", (exercise_goal, user_id))
                cur.execute("""
                    INSERT INTO exercise_start (user_id, start_date) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO NOTHING
                """, (user_id, today_str()))
            conn.commit()
        set_setup_stage(user_id, None)
        g = get_user_goals(user_id)
        return (f"✅ 運動目標：{exercise_goal}分鐘/天\n\n"
                f"🎉 100天挑戰目標設定完成！\n"
                f"⚖️ 體重目標：{g['weight_goal']}kg\n"
                f"💧 喝水目標：{g['water_goal']}ml/天\n"
                f"🏃 運動目標：{g['exercise_goal']}分鐘/天\n\n"
                f"開始打卡吧！輸入「說明」查看打卡指令 💪")
    return None

# ── 打卡邏輯 ──────────────────────────────────────────────

def get_today_record(user_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM daily_records WHERE date = %s AND user_id = %s",
                        (today_str(), user_id))
            row = cur.fetchone()
    return dict(row) if row else {}

def update_record(user_id, updates, group_id=None):
    today = today_str()
    goals = get_user_goals(user_id)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 確保記錄存在
            cur.execute("""
                INSERT INTO daily_records (date, user_id) VALUES (%s, %s)
                ON CONFLICT (date, user_id) DO NOTHING
            """, (today, user_id))

            # 更新各欄位
            for field, value in updates.items():
                cur.execute(f"UPDATE daily_records SET {field} = %s WHERE date = %s AND user_id = %s",
                            (value, today, user_id))

            # 檢查是否為今天第一位
            cur.execute("SELECT COUNT(*) FROM daily_records WHERE date = %s AND points > 0", (today,))
            checkin_count = cur.fetchone()[0]

            # 取得最新記錄計算點數
            cur.execute("SELECT * FROM daily_records WHERE date = %s AND user_id = %s", (today, user_id))
            rec = dict(cur.fetchone())
            pts = calc_points(rec, goals)

            is_first = checkin_count == 0 and pts > 0
            if is_first:
                pts += POINT_RULES['first_bonus']
                cur.execute("UPDATE daily_records SET first_checkin = TRUE WHERE date = %s AND user_id = %s",
                            (today, user_id))

            cur.execute("UPDATE daily_records SET points = %s WHERE date = %s AND user_id = %s",
                        (pts, today, user_id))

            # 記錄群組
            if group_id:
                cur.execute("""
                    INSERT INTO groups_list (group_id) VALUES (%s)
                    ON CONFLICT (group_id) DO NOTHING
                """, (group_id,))

        conn.commit()

    return get_today_record(user_id), goals

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
        etype = rec.get('exercise_type') or ''
        mark = "✅" if em >= eg else "⚠️"
        type_str = f"（{etype}）" if etype else ""
        lines.append(f"🏃 運動：{em}分鐘{type_str} / 目標{eg}分鐘 {mark}")
    else:
        lines.append("🏃 運動：未記錄")

    pts = rec.get('points', 0)
    lines.append(f"\n⭐ 今日點數：{pts}")

    if (rec.get('weight') is not None and rec.get('water') is not None
            and rec.get('exercise_min') is not None):
        lines.append("🎉 三項全做！+15獎勵")
    if rec.get('first_checkin'):
        lines.append("🥇 今日第一位打卡！+5獎勵")

    return "\n".join(lines)

# ── 排行榜 ──────────────────────────────────────────────

def get_leaderboard():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT u.name, SUM(r.points) as total_pts, r.user_id
                FROM daily_records r
                JOIN users u ON r.user_id = u.user_id
                GROUP BY r.user_id, u.name
                ORDER BY total_pts DESC
            """)
            rows = cur.fetchall()

    now = datetime.datetime.now(TAIWAN_TZ)
    medals = ['🥇', '🥈', '🥉']
    lines = ["🏆 健康排行榜\n"]
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        streak = get_streak(row['user_id'], now)
        fire = f" 🔥{streak}天" if streak > 0 else ""
        lines.append(f"{medal} {row['name']}　{row['total_pts']}點{fire}")
    return "\n".join(lines)

def get_streak(user_id, now):
    streak = 0
    check_date = now
    with get_conn() as conn:
        while True:
            d = check_date.strftime('%Y-%m-%d')
            with conn.cursor() as cur:
                cur.execute("SELECT points FROM daily_records WHERE date = %s AND user_id = %s", (d, user_id))
                row = cur.fetchone()
            if row and row[0] > 0:
                streak += 1
                check_date -= datetime.timedelta(days=1)
            else:
                break
    return streak

def get_100day_progress(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT start_date FROM exercise_start WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    if not row:
        return None
    start_date = datetime.datetime.strptime(row[0], '%Y-%m-%d').date()
    today = datetime.datetime.now(TAIWAN_TZ).date()
    goals = get_user_goals(user_id)
    goal_min = goals['exercise_goal']

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM daily_records
                WHERE user_id = %s AND exercise_min >= %s
            """, (user_id, goal_min))
            done = cur.fetchone()[0]

    elapsed = (today - start_date).days + 1
    return done, elapsed

# ── Webhook ──────────────────────────────────────────────

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
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO groups_list (group_id) VALUES (%s) ON CONFLICT DO NOTHING", (group_id,))
        conn.commit()

    msg = ("👋 大家好！我是健康打卡Bot！\n\n"
           "🎯 開始前，請每位成員先設定自己的100天目標：\n"
           "輸入「設定目標」開始設定\n\n"
           "📌 設定完成後可以用以下指令打卡：\n"
           "• 體重 58.5　→ 記錄體重\n"
           "• 喝水 1800　→ 記錄喝水(ml)\n"
           "• 運動 30 跑步　→ 記錄運動分鐘+項目\n"
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

    ensure_user(user_id, group_id)
    name = get_display_name(user_id)
    reply = None

    # 目標設定流程優先
    if get_setup_stage(user_id):
        reply = handle_goal_setup_reply(user_id, text)

    elif text in ['設定目標', '目標設定', '重新設定目標']:
        reply = start_goal_setup(user_id)

    elif text in ['我的目標', '目標']:
        g = get_user_goals(user_id)
        if g['weight_goal']:
            reply = (f"🎯 {name} 的目標\n"
                      f"⚖️ 體重：{g['weight_goal']}kg\n"
                      f"💧 喝水：{g['water_goal']}ml/天\n"
                      f"🏃 運動：{g['exercise_goal']}分鐘/天\n\n"
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
                rec, goals = update_record(user_id, {'exercise_min': minutes, 'exercise_type': activity}, group_id)
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

    elif text in ['打卡', '今日狀況', '我的打卡']:
        rec = get_today_record(user_id)
        goals = get_user_goals(user_id)
        if rec:
            reply = format_checkin_status(rec, name, goals)
        else:
            reply = (f"📋 {name} 今天還沒有任何記錄喔！\n\n"
                      f"可以輸入：\n• 體重 數字\n• 喝水 數字\n• 運動 分鐘數 項目")

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
                 "打卡　→ 今日狀況\n"
                 "排行榜　→ 積分排名\n"
                 "100天　→ 運動達標進度\n\n"
                 "⭐ 集點規則：\n"
                 "體重記錄 +10\n"
                 "喝水記錄 +10（達標再+5）\n"
                 "運動記錄 +15（達標再+5）\n"
                 "三項全做 +15\n"
                 "當日第一打卡 +5")

    if reply:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=event.reply_token,
                                    messages=[TextMessage(text=reply)])
            )

# ── 排程提醒 ──────────────────────────────────────────────

def get_all_groups():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM groups_list")
            return [row[0] for row in cur.fetchall()]

def get_all_users():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, name FROM users")
            return cur.fetchall()

def send_morning_reminder():
    groups = get_all_groups()
    if not groups:
        return
    now = datetime.datetime.now(TAIWAN_TZ)
    weekday_names = ['週一', '週二', '週三', '週四', '週五', '週六', '週日']
    weekday = weekday_names[now.weekday()]
    today = today_str()
    msg = (f"☀️ 早安！今天是 {today} {weekday}\n\n"
           f"📋 今日依照你的個人目標打卡：\n"
           f"• 💪 運動\n"
           f"• 💧 喝水\n"
           f"• ⚖️ 體重記錄\n\n"
           f"還沒設定目標的人輸入「設定目標」開始！🎯")
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for group_id in groups:
            try:
                api.push_message(PushMessageRequest(to=group_id, messages=[TextMessage(text=msg)]))
            except Exception as e:
                print(f"早安提醒失敗 {group_id}: {e}")

def send_evening_reminder():
    groups = get_all_groups()
    if not groups:
        return
    today = today_str()
    users = get_all_users()
    incomplete = []
    with get_conn() as conn:
        for uid, name in users:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT weight, water, exercise_min FROM daily_records
                    WHERE date = %s AND user_id = %s
                """, (today, uid))
                rec = cur.fetchone()
            if not rec or not all(rec):
                incomplete.append(name)

    if not incomplete:
        msg = "🎉 今天大家都完成打卡了！太厲害了！繼續保持 💪"
    else:
        names = '、'.join(incomplete)
        msg = (f"🌙 晚上好！還剩 1 小時打卡喔！\n\n"
               f"⏰ 還沒完成的：{names}\n\n"
               f"趕快打卡，別讓連續天數斷掉！🔥")

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for group_id in groups:
            try:
                api.push_message(PushMessageRequest(to=group_id, messages=[TextMessage(text=msg)]))
            except Exception as e:
                print(f"晚安提醒失敗 {group_id}: {e}")

def send_weekly_leaderboard():
    groups = get_all_groups()
    if not groups:
        return
    lb = get_leaderboard()
    msg = f"📊 本週健康排行榜\n\n{lb}\n\n下週繼續加油！💪"
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for group_id in groups:
            try:
                api.push_message(PushMessageRequest(to=group_id, messages=[TextMessage(text=msg)]))
            except Exception as e:
                print(f"週排行榜失敗 {group_id}: {e}")

# ── 啟動 ──────────────────────────────────────────────

init_db()

scheduler = BackgroundScheduler(timezone=TAIWAN_TZ)
scheduler.add_job(send_morning_reminder, 'cron', hour=7, minute=0)
scheduler.add_job(send_evening_reminder, 'cron', hour=21, minute=0)
scheduler.add_job(send_weekly_leaderboard, 'cron', day_of_week='sun', hour=20, minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
