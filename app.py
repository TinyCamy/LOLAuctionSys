from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from uuid import uuid4

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ["DATABASE_PATH"]) if os.environ.get("DATABASE_PATH") else BASE_DIR / "lol_auction.db"
UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
RANKS = ["黑铁", "青铜", "白银", "黄金", "铂金", "翡翠", "钻石", "大师", "宗师", "王者"]
POSITIONS = ["上单", "打野", "中单", "下路", "辅助"]
GAME_FORMATS = ["一局决胜负", "三局两胜", "五局三胜", "七局四胜"]
CAPTAIN_SELECTIONS = ["指定", "推举", "随机"]
PLAYER_SELECTIONS = ["拍卖", "顺序"]
SERIES_RULES = {"一局决胜负": (1, 1), "三局两胜": (3, 2), "五局三胜": (5, 3), "七局四胜": (7, 4)}
ALLOWED_SCREENSHOT_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-this-secret")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: object | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with app.app_context():
        db = get_db()
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL UNIQUE,
                game_id TEXT NOT NULL,
                rank TEXT NOT NULL,
                primary_position TEXT NOT NULL,
                secondary_positions TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        player_columns = {column["name"] for column in db.execute("PRAGMA table_info(players)").fetchall()}
        if "is_bot" not in player_columns:
            db.execute("ALTER TABLE players ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS game_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                game_format TEXT NOT NULL,
                player_count INTEGER NOT NULL DEFAULT 10,
                captain_selection TEXT NOT NULL,
                player_selection TEXT NOT NULL,
                auction_budget_cents INTEGER NOT NULL DEFAULT 50000,
                rank_adjustment INTEGER NOT NULL DEFAULT 0 CHECK (rank_adjustment IN (0, 1)),
                password_hash TEXT,
                creator_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'PREPARING',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (creator_id) REFERENCES players(id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS room_members (
                room_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (room_id, player_id),
                FOREIGN KEY (room_id) REFERENCES game_rooms(id),
                FOREIGN KEY (player_id) REFERENCES players(id)
            )
            """
        )
        # 兼容已创建过的本地数据库。
        columns = {column["name"] for column in db.execute("PRAGMA table_info(game_rooms)").fetchall()}
        if "password_hash" not in columns:
            db.execute("ALTER TABLE game_rooms ADD COLUMN password_hash TEXT")
        if "selection_state" not in columns:
            db.execute("ALTER TABLE game_rooms ADD COLUMN selection_state TEXT")
        if "first_pick_team" not in columns:
            db.execute("ALTER TABLE game_rooms ADD COLUMN first_pick_team TEXT")
        group_columns = {column["name"] for column in db.execute("PRAGMA table_info(auction_groups)").fetchall()}
        if group_columns and "attempt_count" not in group_columns:
            db.execute("ALTER TABLE auction_groups ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1")
        if group_columns and "original_sequence" not in group_columns:
            db.execute("ALTER TABLE auction_groups ADD COLUMN original_sequence INTEGER")
            db.execute("UPDATE auction_groups SET original_sequence = sequence_number WHERE original_sequence IS NULL")
        db.execute(
            """CREATE TABLE IF NOT EXISTS room_teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                team_code TEXT NOT NULL CHECK (team_code IN ('A', 'B')),
                captain_id INTEGER,
                auction_budget_cents INTEGER NOT NULL,
                UNIQUE(room_id, team_code),
                FOREIGN KEY (room_id) REFERENCES game_rooms(id),
                FOREIGN KEY (captain_id) REFERENCES players(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS auction_bid_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL, attempt_number INTEGER NOT NULL,
                team_code TEXT NOT NULL CHECK (team_code IN ('A', 'B')), bid_cents INTEGER NOT NULL,
                result_note TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (group_id) REFERENCES auction_groups(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS team_members (
                room_id INTEGER NOT NULL, team_code TEXT NOT NULL CHECK (team_code IN ('A', 'B')),
                player_id INTEGER NOT NULL, member_role TEXT NOT NULL DEFAULT 'PLAYER',
                PRIMARY KEY (room_id, player_id),
                FOREIGN KEY (room_id) REFERENCES game_rooms(id), FOREIGN KEY (player_id) REFERENCES players(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS draft_rolls (
                room_id INTEGER NOT NULL, team_code TEXT NOT NULL CHECK (team_code IN ('A', 'B')),
                roll_value INTEGER NOT NULL, PRIMARY KEY (room_id, team_code),
                FOREIGN KEY (room_id) REFERENCES game_rooms(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS draft_picks (
                room_id INTEGER NOT NULL, pick_number INTEGER NOT NULL, team_code TEXT NOT NULL,
                player_id INTEGER NOT NULL, PRIMARY KEY (room_id, pick_number),
                UNIQUE(room_id, player_id), FOREIGN KEY (room_id) REFERENCES game_rooms(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS auction_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL, sequence_number INTEGER NOT NULL,
                revealed INTEGER NOT NULL DEFAULT 0, attempt_count INTEGER NOT NULL DEFAULT 1, original_sequence INTEGER NOT NULL, winner_team TEXT CHECK (winner_team IN ('A', 'B')),
                winner_choice_player_id INTEGER, tie_break_note TEXT, UNIQUE(room_id, sequence_number),
                FOREIGN KEY (room_id) REFERENCES game_rooms(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS auction_group_players (
                group_id INTEGER NOT NULL, player_id INTEGER NOT NULL, PRIMARY KEY (group_id, player_id),
                FOREIGN KEY (group_id) REFERENCES auction_groups(id), FOREIGN KEY (player_id) REFERENCES players(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS auction_bids (
                group_id INTEGER NOT NULL, team_code TEXT NOT NULL CHECK (team_code IN ('A', 'B')),
                bid_cents INTEGER NOT NULL, PRIMARY KEY (group_id, team_code), FOREIGN KEY (group_id) REFERENCES auction_groups(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS captain_votes (
                room_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                candidate_id INTEGER NOT NULL,
                PRIMARY KEY (room_id, voter_id, candidate_id),
                FOREIGN KEY (room_id) REFERENCES game_rooms(id),
                FOREIGN KEY (voter_id) REFERENCES players(id),
                FOREIGN KEY (candidate_id) REFERENCES players(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS game_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                winning_team TEXT CHECK (winning_team IN ('A', 'B')),
                screenshot_path TEXT,
                recorded_by INTEGER,
                recorded_at TEXT,
                UNIQUE(room_id, round_number),
                FOREIGN KEY (room_id) REFERENCES game_rooms(id),
                FOREIGN KEY (recorded_by) REFERENCES players(id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS player_round_stats (
                round_id INTEGER NOT NULL, player_id INTEGER NOT NULL,
                kills INTEGER NOT NULL DEFAULT 0 CHECK (kills >= 0),
                deaths INTEGER NOT NULL DEFAULT 0 CHECK (deaths >= 0),
                assists INTEGER NOT NULL DEFAULT 0 CHECK (assists >= 0),
                PRIMARY KEY (round_id, player_id),
                FOREIGN KEY (round_id) REFERENCES game_rounds(id), FOREIGN KEY (player_id) REFERENCES players(id)
            )"""
        )
        # 为升级前已存在的房间补齐按赛制应有的小局记录。
        for room in db.execute("SELECT id, game_format FROM game_rooms").fetchall():
            existing_rounds = db.execute("SELECT COUNT(*) AS count FROM game_rounds WHERE room_id = ?", (room["id"],)).fetchone()["count"]
            if existing_rounds == 0 and room["game_format"] in SERIES_RULES:
                round_count, _ = SERIES_RULES[room["game_format"]]
                db.executemany(
                    "INSERT INTO game_rounds (room_id, round_number) VALUES (?, ?)",
                    [(room["id"], number) for number in range(1, round_count + 1)],
                )
        db.commit()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "player_id" not in session:
            flash("请先登录。", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def validate_profile(form) -> tuple[dict[str, object] | None, str | None]:
    phone = form.get("phone", "").strip()
    game_id = form.get("game_id", "").strip()
    rank = form.get("rank", "")
    primary = form.get("primary_position", "")
    secondaries = form.getlist("secondary_positions")

    if not phone or not phone.replace("+", "", 1).replace("-", "").isdigit() or len(phone) < 8:
        return None, "请输入有效的手机号。"
    if not game_id:
        return None, "游戏 ID 不能为空。"
    if rank not in RANKS or primary not in POSITIONS:
        return None, "请选择有效的段位和主玩位置。"
    if len(secondaries) > 2 or len(set(secondaries)) != len(secondaries):
        return None, "备选位置最多选择两个，且不能重复。"
    if any(position not in POSITIONS for position in secondaries):
        return None, "备选位置无效。"
    if primary in secondaries:
        return None, "主玩位置不能同时作为备选位置。"
    return {
        "phone": phone,
        "game_id": game_id,
        "rank": rank,
        "primary_position": primary,
        "secondary_positions": ",".join(secondaries),
    }, None


def validate_room(form) -> tuple[dict[str, object] | None, str | None]:
    name = form.get("name", "").strip()
    game_format = form.get("game_format", "")
    captain_selection = form.get("captain_selection", "")
    player_selection = form.get("player_selection", "")
    try:
        player_count = int(form.get("player_count", "10"))
    except ValueError:
        return None, "比赛人数必须是整数。"
    try:
        budget = Decimal(form.get("auction_budget", "500")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None, "拍卖资金格式不正确。"
    raw_rank_adjustment = form.get("rank_adjustment", "0")
    room_password = form.get("room_password", "")

    if not name or len(name) > 80:
        return None, "请输入 1 至 80 个字符的比赛名称。"
    if game_format not in GAME_FORMATS or captain_selection not in CAPTAIN_SELECTIONS or player_selection not in PLAYER_SELECTIONS:
        return None, "请选择有效的比赛规则。"
    if not 4 <= player_count <= 100 or player_count % 2:
        return None, "比赛人数必须是 4 到 100 之间的偶数。"
    if budget < Decimal("0.01") or budget > Decimal("999999.99"):
        return None, "拍卖资金需在 0.01W 到 999999.99W 之间。"
    if raw_rank_adjustment not in {"0", "1"}:
        return None, "段位调整参数无效。"
    if len(room_password) > 64:
        return None, "房间密码不能超过 64 个字符。"
    return {
        "name": name,
        "game_format": game_format,
        "player_count": player_count,
        "captain_selection": captain_selection,
        "player_selection": player_selection,
        "auction_budget_cents": int(budget * 100),
        "rank_adjustment": int(raw_rank_adjustment),
        "password_hash": generate_password_hash(room_password) if room_password else None,
    }, None


def assign_captains(db: sqlite3.Connection, room, captain_a_id: int, captain_b_id: int) -> None:
    """保存两队队长；较低段位一方获得按段位差计算的资金补偿。"""
    captains = db.execute(
        "SELECT id, rank FROM players WHERE id IN (?, ?)", (captain_a_id, captain_b_id)
    ).fetchall()
    if len(captains) != 2:
        raise ValueError("队长信息无效。")
    ranks = {captain["id"]: RANKS.index(captain["rank"]) for captain in captains}
    budget_a = budget_b = room["auction_budget_cents"]
    if room["rank_adjustment"]:
        gap = abs(ranks[captain_a_id] - ranks[captain_b_id])
        if ranks[captain_a_id] < ranks[captain_b_id]:
            budget_a = budget_a * (100 + gap * 10) // 100
        elif ranks[captain_b_id] < ranks[captain_a_id]:
            budget_b = budget_b * (100 + gap * 10) // 100
    db.execute("UPDATE room_teams SET captain_id = ?, auction_budget_cents = ? WHERE room_id = ? AND team_code = 'A'", (captain_a_id, budget_a, room["id"]))
    db.execute("UPDATE room_teams SET captain_id = ?, auction_budget_cents = ? WHERE room_id = ? AND team_code = 'B'", (captain_b_id, budget_b, room["id"]))
    db.executemany(
        "INSERT OR REPLACE INTO team_members (room_id, team_code, player_id, member_role) VALUES (?, ?, ?, 'CAPTAIN')",
        [(room["id"], "A", captain_a_id), (room["id"], "B", captain_b_id)],
    )
    initialize_player_selection(db, room)


def team_capacity(room) -> int:
    return room["player_count"] // 2


def initialize_player_selection(db: sqlite3.Connection, room) -> None:
    if room["player_selection"] == "顺序":
        db.execute("UPDATE game_rooms SET status = 'PLAYER_SELECTION', selection_state = 'DRAFT_ROLLING' WHERE id = ?", (room["id"],))
        return
    captain_ids = [row["captain_id"] for row in db.execute("SELECT captain_id FROM room_teams WHERE room_id = ? ORDER BY team_code", (room["id"],)).fetchall()]
    player_ids = [row["player_id"] for row in db.execute("SELECT player_id FROM room_members WHERE room_id = ? AND player_id NOT IN (?, ?)", (room["id"], *captain_ids)).fetchall()]
    random.shuffle(player_ids)
    for sequence, start in enumerate(range(0, len(player_ids), 2), start=1):
        group = db.execute("INSERT INTO auction_groups (room_id, sequence_number, original_sequence) VALUES (?, ?, ?)", (room["id"], sequence, sequence))
        db.executemany("INSERT INTO auction_group_players (group_id, player_id) VALUES (?, ?)", [(group.lastrowid, player_id) for player_id in player_ids[start:start + 2]])
    db.execute("UPDATE game_rooms SET status = 'PLAYER_SELECTION', selection_state = 'AUCTION_BIDDING' WHERE id = ?", (room["id"],))
    auto_resolve_last_auction_group(db, room)


def current_auction_group(db: sqlite3.Connection, room_id: int):
    return db.execute("SELECT * FROM auction_groups WHERE room_id = ? AND winner_choice_player_id IS NULL ORDER BY sequence_number LIMIT 1", (room_id,)).fetchone()


def resolve_auction_group(db: sqlite3.Connection, group, force_remaining: bool = False) -> bool:
    bids = {row["team_code"]: row["bid_cents"] for row in db.execute("SELECT team_code, bid_cents FROM auction_bids WHERE group_id = ?", (group["id"],)).fetchall()}
    if len(bids) != 2:
        return False
    db.executemany(
        "INSERT INTO auction_bid_history (group_id, attempt_number, team_code, bid_cents) VALUES (?, ?, ?, ?)",
        [(group["id"], group["attempt_count"], team, bid) for team, bid in bids.items()],
    )
    if bids["A"] == bids["B"]:
        last_sequence = db.execute("SELECT MAX(sequence_number) AS value FROM auction_groups WHERE room_id = ?", (group["room_id"],)).fetchone()["value"]
        note = f"第 {group['attempt_count']} 次拍卖双方同价，已移至队列末尾进行下一次拍卖。"
        db.execute("UPDATE auction_bid_history SET result_note = ? WHERE group_id = ? AND attempt_number = ?", (note, group["id"], group["attempt_count"]))
        db.execute("DELETE FROM auction_bids WHERE group_id = ?", (group["id"],))
        db.execute("UPDATE auction_groups SET sequence_number = ?, attempt_count = attempt_count + 1, revealed = 0, winner_team = NULL, tie_break_note = ? WHERE id = ?", (last_sequence + 1, note, group["id"]))
        db.execute("UPDATE game_rooms SET selection_state = 'AUCTION_BIDDING' WHERE id = ?", (group["room_id"],))
        return True
    winner = "A" if bids["A"] > bids["B"] else "B"
    note = None
    db.execute("UPDATE room_teams SET auction_budget_cents = auction_budget_cents - ? WHERE room_id = ? AND team_code = ?", (bids[winner], group["room_id"], winner))
    db.execute("UPDATE auction_groups SET revealed = 1, winner_team = ?, tie_break_note = ? WHERE id = ?", (winner, note, group["id"]))
    db.execute("UPDATE game_rooms SET selection_state = 'AUCTION_PICKING' WHERE id = ?", (group["room_id"],))
    return False


def auto_resolve_last_auction_group(db: sqlite3.Connection, room) -> None:
    group = current_auction_group(db, room["id"])
    if group is None:
        return
    queue_last = db.execute("SELECT MAX(sequence_number) AS value FROM auction_groups WHERE room_id = ?", (room["id"],)).fetchone()["value"]
    if group["sequence_number"] != queue_last or group["revealed"]:
        return
    budgets = {row["team_code"]: row["auction_budget_cents"] for row in db.execute("SELECT team_code, auction_budget_cents FROM room_teams WHERE room_id = ?", (room["id"],)).fetchall()}
    db.executemany("INSERT OR REPLACE INTO auction_bids (group_id, team_code, bid_cents) VALUES (?, ?, ?)", [(group["id"], "A", budgets["A"]), (group["id"], "B", budgets["B"])])
    resolve_auction_group(db, group, force_remaining=True)


def finish_team_selection_if_full(db: sqlite3.Connection, room) -> bool:
    counts = {row["team_code"]: row["count"] for row in db.execute("SELECT team_code, COUNT(*) AS count FROM team_members WHERE room_id = ? GROUP BY team_code", (room["id"],)).fetchall()}
    if counts.get("A") == counts.get("B") == team_capacity(room):
        db.execute("UPDATE game_rooms SET status = 'TEAMS_READY', selection_state = 'COMPLETE' WHERE id = ?", (room["id"],))
        return True
    return False


def is_room_member(db: sqlite3.Connection, room_id: int, player_id: int) -> bool:
    return db.execute("SELECT 1 FROM room_members WHERE room_id = ? AND player_id = ?", (room_id, player_id)).fetchone() is not None


def get_active_room_for_player(db: sqlite3.Connection, player_id: int):
    return db.execute(
        """SELECT r.id, r.name, r.status FROM room_members m JOIN game_rooms r ON r.id = m.room_id
        WHERE m.player_id = ? AND r.status != 'FINISHED' ORDER BY r.created_at DESC LIMIT 1""",
        (player_id,),
    ).fetchone()


def can_record_result(db: sqlite3.Connection, room, player_id: int) -> bool:
    return room["creator_id"] == player_id


def parse_round_stats(form, player_ids: list[int]) -> tuple[list[tuple[int, int, int, int]] | None, str | None]:
    stats = []
    for player_id in player_ids:
        try:
            kills = int(form.get(f"kills_{player_id}", "0"))
            deaths = int(form.get(f"deaths_{player_id}", "0"))
            assists = int(form.get(f"assists_{player_id}", "0"))
        except ValueError:
            return None, "击杀、死亡和助攻必须为非负整数。"
        if min(kills, deaths, assists) < 0 or max(kills, deaths, assists) > 999:
            return None, "每项 KDA 数据需在 0 到 999 之间。"
        stats.append((player_id, kills, deaths, assists))
    return stats, None


def recalculate_match_status(db: sqlite3.Connection, room) -> None:
    score = {row["winning_team"]: row["count"] for row in db.execute("SELECT winning_team, COUNT(*) AS count FROM game_rounds WHERE room_id = ? AND winning_team IS NOT NULL GROUP BY winning_team", (room["id"],)).fetchall()}
    _, wins_needed = SERIES_RULES[room["game_format"]]
    status = "FINISHED" if max(score.get("A", 0), score.get("B", 0)) >= wins_needed else "IN_PROGRESS"
    db.execute("UPDATE game_rooms SET status = ? WHERE id = ?", (status, room["id"]))


def next_match_id(db: sqlite3.Connection) -> str:
    prefix = datetime.now().strftime("%Y%m%d")
    latest = db.execute(
        "SELECT match_id FROM game_rooms WHERE match_id LIKE ? ORDER BY match_id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    sequence = int(latest["match_id"][-5:]) + 1 if latest else 1
    if sequence > 99999:
        raise ValueError("今日比赛房间数量已达上限。")
    return f"{prefix}{sequence:05d}"


def get_room(db: sqlite3.Connection, room_id: int):
    return db.execute(
        """SELECT r.*, creator.game_id AS creator_game_id, COUNT(m.player_id) AS member_count
        FROM game_rooms r
        JOIN players creator ON creator.id = r.creator_id
        LEFT JOIN room_members m ON m.room_id = r.id
        WHERE r.id = ? GROUP BY r.id""",
        (room_id,),
    ).fetchone()


@app.route("/")
def index():
    if "player_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    all_rooms = db.execute(
        """SELECT r.*, p.game_id AS creator_game_id, COUNT(m.player_id) AS member_count,
        EXISTS(SELECT 1 FROM room_members own WHERE own.room_id = r.id AND own.player_id = ?) AS joined
        FROM game_rooms r JOIN players p ON p.id = r.creator_id
        LEFT JOIN room_members m ON m.room_id = r.id
        GROUP BY r.id ORDER BY r.created_at DESC""",
        (session["player_id"],),
    ).fetchall()
    rooms = [room for room in all_rooms if room["status"] != "FINISHED"]
    joined_rooms = [room for room in rooms if room["joined"]]
    return render_template("home.html", rooms=rooms, joined_rooms=joined_rooms, active_room=get_active_room_for_player(db, session["player_id"]))


@app.get("/my-matches")
@login_required
def my_matches():
    db = get_db()
    matches = db.execute(
        """SELECT r.*, p.game_id AS creator_game_id, COUNT(all_members.player_id) AS member_count
        FROM room_members mine
        JOIN game_rooms r ON r.id = mine.room_id
        JOIN players p ON p.id = r.creator_id
        LEFT JOIN room_members all_members ON all_members.room_id = r.id
        WHERE mine.player_id = ?
        GROUP BY r.id ORDER BY r.created_at DESC""",
        (session["player_id"],),
    ).fetchall()
    return render_template("my_matches.html", matches=matches)


@app.route("/register", methods=["GET", "POST"])
def register():
    if "player_id" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        profile, error = validate_profile(request.form)
        if error:
            flash(error, "error")
        else:
            db = get_db()
            try:
                db.execute(
                    """INSERT INTO players
                    (phone, game_id, rank, primary_position, secondary_positions, password_hash)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (*profile.values(), generate_password_hash(profile["game_id"])),
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash("该手机号已经注册，请直接登录。", "error")
            else:
                flash("注册成功！初始密码已设为你的游戏 ID，请登录。", "success")
                return redirect(url_for("login"))
    return render_template("register.html", ranks=RANKS, positions=POSITIONS)


@app.route("/login", methods=["GET", "POST"])
def login():
    if "player_id" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        player = get_db().execute("SELECT * FROM players WHERE phone = ?", (phone,)).fetchone()
        if player is None or not check_password_hash(player["password_hash"], password):
            flash("手机号或密码不正确。", "error")
        else:
            session.clear()
            session["player_id"] = player["id"]
            flash(f"欢迎回来，{player['game_id']}！", "success")
            return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db = get_db()
    player = db.execute("SELECT * FROM players WHERE id = ?", (session["player_id"],)).fetchone()
    if request.method == "POST":
        profile_data, error = validate_profile(request.form)
        if error:
            flash(error, "error")
        else:
            existing = db.execute("SELECT id FROM players WHERE phone = ? AND id != ?", (profile_data["phone"], player["id"])).fetchone()
            if existing:
                flash("该手机号已被其他玩家使用。", "error")
            else:
                db.execute(
                    """UPDATE players SET phone=?, game_id=?, rank=?, primary_position=?, secondary_positions=?
                    WHERE id=?""",
                    (*profile_data.values(), player["id"]),
                )
                db.commit()
                flash("个人信息已更新。", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html", player=player, ranks=RANKS, positions=POSITIONS)


@app.get("/logout")
@login_required
def logout():
    session.clear()
    flash("已退出登录。", "success")
    return redirect(url_for("login"))


@app.route("/rooms/create", methods=["GET", "POST"])
@login_required
def create_room():
    active_room = get_active_room_for_player(get_db(), session["player_id"])
    if active_room is not None:
        flash("你已有未结束的比赛房间，结束该比赛后才能创建新房间。", "error")
        return redirect(url_for("room_detail", room_id=active_room["id"]))
    if request.method == "POST":
        room_data, error = validate_room(request.form)
        if error:
            flash(error, "error")
        else:
            db = get_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                match_id = next_match_id(db)
                cursor = db.execute(
                    """INSERT INTO game_rooms
                    (match_id, name, game_format, player_count, captain_selection, player_selection,
                    auction_budget_cents, rank_adjustment, password_hash, creator_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (match_id, room_data["name"], room_data["game_format"], room_data["player_count"],
                     room_data["captain_selection"], room_data["player_selection"], room_data["auction_budget_cents"],
                     room_data["rank_adjustment"], room_data["password_hash"], session["player_id"]),
                )
                db.execute(
                    "INSERT INTO room_members (room_id, player_id) VALUES (?, ?)",
                    (cursor.lastrowid, session["player_id"]),
                )
                round_count, _ = SERIES_RULES[room_data["game_format"]]
                db.executemany(
                    "INSERT INTO game_rounds (room_id, round_number) VALUES (?, ?)",
                    [(cursor.lastrowid, number) for number in range(1, round_count + 1)],
                )
                db.commit()
            except (sqlite3.Error, ValueError):
                db.rollback()
                flash("创建比赛房间失败，请稍后重试。", "error")
            else:
                flash(f"比赛房间创建成功，房间 ID：{match_id}。", "success")
                return redirect(url_for("room_detail", room_id=cursor.lastrowid))
    return render_template(
        "create_room.html",
        game_formats=GAME_FORMATS,
        captain_selections=CAPTAIN_SELECTIONS,
        player_selections=PLAYER_SELECTIONS,
    )


@app.post("/rooms/join")
@login_required
def join_room_by_id():
    match_id = request.form.get("match_id", "").strip()
    room = get_db().execute("SELECT id FROM game_rooms WHERE match_id = ?", (match_id,)).fetchone()
    if room is None:
        flash("没有找到该比赛房间，请检查房间 ID。", "error")
        return redirect(url_for("index"))
    return process_room_join(room["id"], request.form.get("room_password", ""))


@app.route("/rooms/<int:room_id>/join", methods=["GET", "POST"])
@login_required
def join_room(room_id: int):
    if request.method == "GET":
        room = get_room(get_db(), room_id)
        if room is None:
            flash("比赛房间不存在。", "error")
            return redirect(url_for("index"))
        if is_room_member(get_db(), room_id, session["player_id"]):
            return redirect(url_for("room_detail", room_id=room_id))
        if get_active_room_for_player(get_db(), session["player_id"]) is not None:
            flash("你已参加一个未结束的比赛房间，不能加入其他房间。", "error")
            return redirect(url_for("index"))
        return render_template("join_room.html", room=room, password_required=bool(room["password_hash"]))
    return process_room_join(room_id, request.form.get("room_password", ""))


def process_room_join(room_id: int, room_password: str):
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        room = get_room(db, room_id)
        if room is None:
            flash("比赛房间不存在。", "error")
        elif db.execute("SELECT 1 FROM room_members WHERE room_id = ? AND player_id = ?", (room_id, session["player_id"])).fetchone():
            db.commit()
            return redirect(url_for("room_detail", room_id=room_id))
        elif get_active_room_for_player(db, session["player_id"]) is not None:
            flash("你已参加一个未结束的比赛房间，结束该比赛后才能加入其他房间。", "error")
        elif room["status"] != "PREPARING":
            flash("该比赛房间已开始后续流程，不能再加入。", "error")
        elif room["password_hash"] and not check_password_hash(room["password_hash"], room_password):
            flash("房间密码不正确。", "error")
        elif room["member_count"] >= room["player_count"]:
            flash("该比赛房间人数已满。", "error")
        else:
            db.execute("INSERT INTO room_members (room_id, player_id) VALUES (?, ?)", (room_id, session["player_id"]))
            db.commit()
            flash(f"已加入比赛房间「{room['name']}」。", "success")
            return redirect(url_for("room_detail", room_id=room_id))
        db.rollback()
    except sqlite3.Error:
        db.rollback()
        flash("加入比赛房间失败，请稍后重试。", "error")
    return redirect(url_for("index"))


@app.post("/rooms/<int:room_id>/captains/start")
@login_required
def start_captain_selection(room_id: int):
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        room = get_room(db, room_id)
        if room is None or room["creator_id"] != session["player_id"]:
            flash("只有房间创建者可以操作队长选择。", "error")
        elif room["status"] != "PREPARING":
            flash("当前不在准备阶段。", "error")
        elif room["member_count"] != room["player_count"]:
            flash("人数满员后才能进入队长选择阶段。", "error")
        else:
            db.executemany(
                "INSERT INTO room_teams (room_id, team_code, auction_budget_cents) VALUES (?, ?, ?)",
                [(room_id, "A", room["auction_budget_cents"]), (room_id, "B", room["auction_budget_cents"])],
            )
            db.execute("UPDATE game_rooms SET status = 'CAPTAIN_SELECTION' WHERE id = ?", (room_id,))
            if room["captain_selection"] == "随机":
                player_ids = [row["player_id"] for row in db.execute("SELECT player_id FROM room_members WHERE room_id = ?", (room_id,)).fetchall()]
                captain_a, captain_b = random.sample(player_ids, 2)
                assign_captains(db, room, captain_a, captain_b)
                flash("已随机建立 A、B 两队并选出两名队长。", "success")
            elif room["captain_selection"] == "推举":
                bot_ids = [row["player_id"] for row in db.execute("SELECT m.player_id FROM room_members m JOIN players p ON p.id = m.player_id WHERE m.room_id = ? AND p.is_bot = 1", (room_id,)).fetchall()]
                all_ids = [row["player_id"] for row in db.execute("SELECT player_id FROM room_members WHERE room_id = ?", (room_id,)).fetchall()]
                for bot_id in bot_ids:
                    db.executemany("INSERT INTO captain_votes (room_id, voter_id, candidate_id) VALUES (?, ?, ?)", [(room_id, bot_id, candidate) for candidate in random.sample(all_ids, 3)])
                flash("已进入队长选择阶段；虚拟队员已自动完成推举投票。", "success")
            else:
                flash("已进入队长选择阶段。", "success")
            db.commit()
            return redirect(url_for("room_detail", room_id=room_id))
        db.rollback()
    except sqlite3.Error:
        db.rollback()
        flash("无法进入队长选择阶段，请重试。", "error")
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/captains/specify")
@login_required
def specify_captains(room_id: int):
    db = get_db()
    selected = request.form.getlist("captain_ids")
    if len(selected) != 2 or selected[0] == selected[1] or not all(item.isdigit() for item in selected):
        flash("请选择两位不同的队长。", "error")
        return redirect(url_for("room_detail", room_id=room_id))
    room = get_room(db, room_id)
    if room is None or room["creator_id"] != session["player_id"] or room["status"] != "CAPTAIN_SELECTION" or room["captain_selection"] != "指定":
        flash("当前无法指定队长。", "error")
        return redirect(url_for("room_detail", room_id=room_id))
    captain_ids = [int(item) for item in selected]
    if not all(is_room_member(db, room_id, player_id) for player_id in captain_ids):
        flash("队长必须是本房间成员。", "error")
        return redirect(url_for("room_detail", room_id=room_id))
    assign_captains(db, room, *captain_ids)
    db.commit()
    flash("两位队长已指定，资金平衡已计算。", "success")
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/captains/vote")
@login_required
def vote_captains(room_id: int):
    db = get_db()
    selected = request.form.getlist("candidate_ids")
    room = get_room(db, room_id)
    if room is None or room["status"] != "CAPTAIN_SELECTION" or room["captain_selection"] != "推举" or not is_room_member(db, room_id, session["player_id"]):
        flash("当前无法投票。", "error")
    elif len(selected) != 3 or len(set(selected)) != 3 or not all(item.isdigit() and is_room_member(db, room_id, int(item)) for item in selected):
        flash("请向三位不同的房间成员各投一票。", "error")
    else:
        db.execute("DELETE FROM captain_votes WHERE room_id = ? AND voter_id = ?", (room_id, session["player_id"]))
        db.executemany("INSERT INTO captain_votes (room_id, voter_id, candidate_id) VALUES (?, ?, ?)", [(room_id, session["player_id"], int(item)) for item in selected])
        db.commit()
        flash("你的三票已提交，可在结果确认前重新投票。", "success")
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/captains/finalize-vote")
@login_required
def finalize_vote(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    if room is None or room["creator_id"] != session["player_id"] or room["status"] != "CAPTAIN_SELECTION" or room["captain_selection"] != "推举":
        flash("当前无法确认投票结果。", "error")
        return redirect(url_for("room_detail", room_id=room_id))
    voter_count = db.execute("SELECT COUNT(DISTINCT voter_id) AS count FROM captain_votes WHERE room_id = ?", (room_id,)).fetchone()["count"]
    if voter_count != room["member_count"]:
        flash("所有房间成员都提交三票后，才可确认队长。", "error")
        return redirect(url_for("room_detail", room_id=room_id))
    winners = db.execute(
        """SELECT v.candidate_id, COUNT(*) AS votes, p.game_id FROM captain_votes v JOIN players p ON p.id = v.candidate_id
        WHERE v.room_id = ? GROUP BY v.candidate_id ORDER BY votes DESC, p.game_id ASC LIMIT 2""",
        (room_id,),
    ).fetchall()
    if len(winners) < 2:
        flash("投票结果不足以选出两位队长。", "error")
        return redirect(url_for("room_detail", room_id=room_id))
    assign_captains(db, room, winners[0]["candidate_id"], winners[1]["candidate_id"])
    db.commit()
    flash("投票结果已确认，两位队长和资金平衡已生成。", "success")
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/start-match")
@login_required
def start_match(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    if room is None or room["creator_id"] != session["player_id"] or room["status"] != "TEAMS_READY":
        flash("需先完成队长与队员选择，且仅创建者可开始比赛。", "error")
    else:
        db.execute("UPDATE game_rooms SET status = 'IN_PROGRESS' WHERE id = ?", (room_id,))
        db.commit()
        flash("比赛已开始，请在每个小局结束后上传截图并录入胜方。", "success")
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/delete")
@login_required
def delete_room(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    if room is None or room["creator_id"] != session["player_id"]:
        flash("只有房间创建者可以删除比赛。", "error")
        return redirect(url_for("index"))
    screenshots = db.execute("SELECT screenshot_path FROM game_rounds WHERE room_id = ? AND screenshot_path IS NOT NULL", (room_id,)).fetchall()
    bot_ids = [row["player_id"] for row in db.execute("SELECT m.player_id FROM room_members m JOIN players p ON p.id = m.player_id WHERE m.room_id = ? AND p.is_bot = 1", (room_id,)).fetchall()]
    for table in ("captain_votes", "room_teams", "team_members", "draft_rolls", "draft_picks", "auction_bids", "auction_group_players", "auction_groups", "game_rounds", "room_members"):
        if table == "auction_bids":
            db.execute("DELETE FROM auction_bids WHERE group_id IN (SELECT id FROM auction_groups WHERE room_id = ?)", (room_id,))
        elif table == "auction_group_players":
            db.execute("DELETE FROM auction_group_players WHERE group_id IN (SELECT id FROM auction_groups WHERE room_id = ?)", (room_id,))
        else:
            db.execute(f"DELETE FROM {table} WHERE room_id = ?", (room_id,))
    db.execute("DELETE FROM game_rooms WHERE id = ?", (room_id,))
    if bot_ids:
        db.executemany("DELETE FROM players WHERE id = ? AND is_bot = 1", [(bot_id,) for bot_id in bot_ids])
    db.commit()
    for screenshot in screenshots:
        path = BASE_DIR / "static" / screenshot["screenshot_path"]
        if path.is_file():
            path.unlink()
    flash("比赛房间及其记录已删除。", "success")
    return redirect(url_for("index"))


@app.post("/rooms/<int:room_id>/fill-bots")
@login_required
def fill_room_with_bots(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    if room is None or room["creator_id"] != session["player_id"] or room["status"] != "PREPARING":
        flash("仅房间创建者可在准备阶段填充虚拟队员。", "error")
        return redirect(url_for("room_detail", room_id=room_id))
    missing = room["player_count"] - room["member_count"]
    if missing <= 0:
        flash("房间人数已满。", "error")
        return redirect(url_for("room_detail", room_id=room_id))
    for offset in range(1, missing + 1):
        number = room["member_count"] + offset
        game_id = f"虚拟队员-{room['match_id']}-{number:02d}"
        phone = f"BOT{room_id:05d}{number:03d}"
        cursor = db.execute(
            """INSERT INTO players (phone, game_id, rank, primary_position, secondary_positions, password_hash, is_bot)
            VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (phone, game_id, random.choice(RANKS), random.choice(POSITIONS), "", generate_password_hash(uuid4().hex)),
        )
        db.execute("INSERT INTO room_members (room_id, player_id) VALUES (?, ?)", (room_id, cursor.lastrowid))
    db.commit()
    flash(f"已补充 {missing} 名虚拟队员，房间现在满员。", "success")
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/leave")
@login_required
def leave_room(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    if room is None or not is_room_member(db, room_id, session["player_id"]):
        flash("你不在该比赛房间中。", "error")
    elif room["creator_id"] == session["player_id"]:
        flash("房间创建者不能退出，请在首页删除比赛房间。", "error")
    elif room["status"] != "PREPARING":
        flash("进入队长选择后，为保证流程完整，不能再退出房间。", "error")
    else:
        db.execute("DELETE FROM room_members WHERE room_id = ? AND player_id = ?", (room_id, session["player_id"]))
        db.commit()
        flash("你已退出比赛房间。", "success")
        return redirect(url_for("index"))
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/draft/roll")
@login_required
def roll_draft_dice(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    team = db.execute("SELECT team_code FROM room_teams WHERE room_id = ? AND captain_id = ?", (room_id, session["player_id"])).fetchone()
    if room is None or room["status"] != "PLAYER_SELECTION" or room["player_selection"] != "顺序" or room["selection_state"] != "DRAFT_ROLLING" or team is None:
        flash("当前无法投掷选人骰子。", "error")
    elif db.execute("SELECT 1 FROM draft_rolls WHERE room_id = ? AND team_code = ?", (room_id, team["team_code"])).fetchone():
        flash("你已经投过骰子，请等待另一位队长。", "error")
    else:
        value = random.randint(1, 6)
        db.execute("INSERT INTO draft_rolls (room_id, team_code, roll_value) VALUES (?, ?, ?)", (room_id, team["team_code"], value))
        rolls = db.execute("SELECT * FROM draft_rolls WHERE room_id = ?", (room_id,)).fetchall()
        if len(rolls) == 2 and rolls[0]["roll_value"] == rolls[1]["roll_value"]:
            db.execute("DELETE FROM draft_rolls WHERE room_id = ?", (room_id,))
            flash(f"双方都掷出 {value} 点，平局，请重新投掷。", "error")
        elif len(rolls) == 2:
            winner = max(rolls, key=lambda roll: roll["roll_value"])["team_code"]
            db.execute("UPDATE game_rooms SET selection_state = 'DRAFT_CHOOSE_ORDER' WHERE id = ?", (room_id,))
            flash(f"骰子已确定，{winner} 队队长请选择先选或后选。", "success")
        else:
            flash(f"你掷出了 {value} 点，请等待另一位队长。", "success")
        db.commit()
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/draft/choose-order")
@login_required
def choose_draft_order(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    choice = request.form.get("choice")
    rolls = {row["team_code"]: row["roll_value"] for row in db.execute("SELECT * FROM draft_rolls WHERE room_id = ?", (room_id,)).fetchall()}
    team = db.execute("SELECT team_code FROM room_teams WHERE room_id = ? AND captain_id = ?", (room_id, session["player_id"])).fetchone()
    if room is None or room["selection_state"] != "DRAFT_CHOOSE_ORDER" or team is None or len(rolls) != 2 or team["team_code"] != ("A" if rolls["A"] > rolls["B"] else "B") or choice not in {"first", "second"}:
        flash("当前无法决定选人顺序。", "error")
    else:
        first = team["team_code"] if choice == "first" else ("B" if team["team_code"] == "A" else "A")
        db.execute("UPDATE game_rooms SET selection_state = 'DRAFT_PICKING', first_pick_team = ? WHERE id = ?", (first, room_id))
        db.commit()
        flash(f"{first} 队获得首选，首轮只能选择一位队员。", "success")
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/draft/pick")
@login_required
def draft_pick(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    team_row = db.execute("SELECT team_code FROM room_teams WHERE room_id = ? AND captain_id = ?", (room_id, session["player_id"])).fetchone()
    selected = request.form.getlist("player_ids")
    counts = {row["team_code"]: row["count"] for row in db.execute("SELECT team_code, COUNT(*) AS count FROM team_members WHERE room_id = ? GROUP BY team_code", (room_id,)).fetchall()}
    if room is None or room["selection_state"] != "DRAFT_PICKING" or team_row is None:
        flash("当前无法选人。", "error")
    else:
        team = team_row["team_code"]
        pick_count = db.execute("SELECT COUNT(*) AS count FROM draft_picks WHERE room_id = ?", (room_id,)).fetchone()["count"]
        if pick_count == 0:
            active_team, expected = room["first_pick_team"], 1
        else:
            active_team = "A" if counts.get("A", 0) < counts.get("B", 0) else "B"
            expected = min(2, team_capacity(room) - counts.get(active_team, 0))
        available = {row["player_id"] for row in db.execute("SELECT player_id FROM room_members WHERE room_id = ? EXCEPT SELECT player_id FROM team_members WHERE room_id = ?", (room_id, room_id)).fetchall()}
        if team != active_team or len(selected) != expected or len(set(selected)) != expected or not all(item.isdigit() and int(item) in available for item in selected):
            flash("请选择当前轮次要求数量的未分队成员。", "error")
        else:
            db.executemany("INSERT INTO team_members (room_id, team_code, player_id) VALUES (?, ?, ?)", [(room_id, team, int(player_id)) for player_id in selected])
            db.executemany("INSERT INTO draft_picks (room_id, pick_number, team_code, player_id) VALUES (?, ?, ?, ?)", [(room_id, pick_count + offset, team, int(player_id)) for offset, player_id in enumerate(selected, 1)])
            finish_team_selection_if_full(db, room)
            db.commit()
            flash("选人已记录。", "success")
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/auction/bid")
@login_required
def auction_bid(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    team = db.execute("SELECT team_code, auction_budget_cents FROM room_teams WHERE room_id = ? AND captain_id = ?", (room_id, session["player_id"])).fetchone()
    group = current_auction_group(db, room_id) if room else None
    try:
        bid = Decimal(request.form.get("bid", "")).quantize(Decimal("0.01"))
        bid_cents = int(bid * 100)
    except (InvalidOperation, ValueError):
        bid_cents = -1
    if room is None or room["status"] != "PLAYER_SELECTION" or room["selection_state"] != "AUCTION_BIDDING" or team is None or group is None:
        flash("当前无法提交拍卖金额。", "error")
    elif bid_cents < 0 or bid_cents > team["auction_budget_cents"]:
        flash("竞拍金额必须在 0 到当前可用资金之间，精确到 0.01W。", "error")
    else:
        db.execute("INSERT OR REPLACE INTO auction_bids (group_id, team_code, bid_cents) VALUES (?, ?, ?)", (group["id"], team["team_code"], bid_cents))
        if db.execute("SELECT COUNT(*) AS count FROM auction_bids WHERE group_id = ?", (group["id"],)).fetchone()["count"] == 2:
            tied = resolve_auction_group(db, group)
            flash("双方同价，该组已移至末尾等待第二次拍卖。" if tied else "双方金额已提交，竞拍结果现已公开。", "success")
        else:
            flash("你的拍卖金额已提交，等待另一位队长。", "success")
        db.commit()
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/auction/choose")
@login_required
def auction_choose(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    group = current_auction_group(db, room_id) if room else None
    team = db.execute("SELECT team_code FROM room_teams WHERE room_id = ? AND captain_id = ?", (room_id, session["player_id"])).fetchone()
    selected = request.form.get("player_id", "")
    if room is None or room["selection_state"] != "AUCTION_PICKING" or group is None or team is None or group["winner_team"] != team["team_code"]:
        flash("当前没有可供你决定的竞拍组。", "error")
    else:
        candidates = [row["player_id"] for row in db.execute("SELECT player_id FROM auction_group_players WHERE group_id = ?", (group["id"],)).fetchall()]
        if not selected.isdigit() or int(selected) not in candidates:
            flash("请选择本组的一名玩家。", "error")
        else:
            winner_player = int(selected)
            other_player = next(player for player in candidates if player != winner_player)
            losing_team = "B" if team["team_code"] == "A" else "A"
            db.execute("INSERT INTO team_members (room_id, team_code, player_id) VALUES (?, ?, ?)", (room_id, team["team_code"], winner_player))
            db.execute("INSERT INTO team_members (room_id, team_code, player_id) VALUES (?, ?, ?)", (room_id, losing_team, other_player))
            db.execute("UPDATE auction_groups SET winner_choice_player_id = ? WHERE id = ?", (winner_player, group["id"]))
            if finish_team_selection_if_full(db, room):
                flash("最后一组选人完成，双方队伍已满员。", "success")
            else:
                db.execute("UPDATE game_rooms SET selection_state = 'AUCTION_BIDDING' WHERE id = ?", (room_id,))
                auto_resolve_last_auction_group(db, room)
                flash("本组队员已分配，进入下一组。", "success")
            db.commit()
    return redirect(url_for("room_detail", room_id=room_id))


@app.post("/rooms/<int:room_id>/rounds/<int:round_id>/record")
@login_required
def record_round(room_id: int, round_id: int):
    db = get_db()
    room = get_room(db, room_id)
    winner = request.form.get("winning_team", "")
    game_round = db.execute("SELECT * FROM game_rounds WHERE id = ? AND room_id = ?", (round_id, room_id)).fetchone()
    player_ids = [row["player_id"] for row in db.execute("SELECT player_id FROM team_members WHERE room_id = ?", (room_id,)).fetchall()]
    stats, stats_error = parse_round_stats(request.form, player_ids)
    if room is None or room["status"] != "IN_PROGRESS" or not can_record_result(db, room, session["player_id"]):
        flash("只有比赛创建者可在比赛进行中录入成绩。", "error")
    elif game_round is None or game_round["winning_team"] is not None or winner not in {"A", "B"}:
        flash("小局状态或胜方信息无效。", "error")
    elif game_round["round_number"] != db.execute("SELECT COUNT(*) AS count FROM game_rounds WHERE room_id = ? AND winning_team IS NOT NULL", (room_id,)).fetchone()["count"] + 1:
        flash("请按小局顺序录入比赛成绩。", "error")
    elif stats_error:
        flash(stats_error, "error")
    else:
        db.execute("UPDATE game_rounds SET winning_team = ?, recorded_by = ?, recorded_at = CURRENT_TIMESTAMP WHERE id = ?", (winner, session["player_id"], round_id))
        db.executemany("INSERT OR REPLACE INTO player_round_stats (round_id, player_id, kills, deaths, assists) VALUES (?, ?, ?, ?, ?)", [(round_id, *item) for item in stats])
        score = db.execute("SELECT winning_team, COUNT(*) AS count FROM game_rounds WHERE room_id = ? AND winning_team IS NOT NULL GROUP BY winning_team", (room_id,)).fetchall()
        wins = {row["winning_team"]: row["count"] for row in score}
        _, wins_needed = SERIES_RULES[room["game_format"]]
        if wins.get(winner, 0) >= wins_needed:
            db.execute("UPDATE game_rooms SET status = 'FINISHED' WHERE id = ?", (room_id,))
            flash(f"小局成绩已记录，{winner} 队达到胜利条件，比赛结束！", "success")
        else:
            flash("小局成绩已记录。", "success")
        db.commit()
    return redirect(url_for("room_results", room_id=room_id))


@app.post("/rooms/<int:room_id>/rounds/<int:round_id>/edit")
@login_required
def edit_round(room_id: int, round_id: int):
    db = get_db()
    room = get_room(db, room_id)
    game_round = db.execute("SELECT * FROM game_rounds WHERE id = ? AND room_id = ?", (round_id, room_id)).fetchone()
    player_ids = [row["player_id"] for row in db.execute("SELECT player_id FROM team_members WHERE room_id = ?", (room_id,)).fetchall()]
    stats, stats_error = parse_round_stats(request.form, player_ids)
    winner = request.form.get("winning_team", "")
    if room is None or game_round is None or room["creator_id"] != session["player_id"] or game_round["winning_team"] is None:
        flash("只有创建者可以修改已录入的小局。", "error")
    elif winner not in {"A", "B"} or stats_error:
        flash(stats_error or "请选择有效的获胜队伍。", "error")
    else:
        db.execute("UPDATE game_rounds SET winning_team = ? WHERE id = ?", (winner, round_id))
        db.executemany("INSERT OR REPLACE INTO player_round_stats (round_id, player_id, kills, deaths, assists) VALUES (?, ?, ?, ?, ?)", [(round_id, *item) for item in stats])
        recalculate_match_status(db, room)
        db.commit()
        flash("该局结果与所有选手 KDA 已更新。", "success")
    return redirect(url_for("room_results", room_id=room_id))


@app.get("/rooms/<int:room_id>")
@login_required
def room_detail(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    if room is None:
        flash("比赛房间不存在。", "error")
        return redirect(url_for("index"))
    if not is_room_member(db, room_id, session["player_id"]):
        flash("请先加入该比赛房间。", "error")
        return redirect(url_for("index"))
    if room["status"] == "FINISHED":
        return redirect(url_for("room_results", room_id=room_id))
    members = db.execute(
        "SELECT p.id, p.game_id, p.rank, p.primary_position, p.secondary_positions FROM room_members m "
        "JOIN players p ON p.id = m.player_id WHERE m.room_id = ? ORDER BY m.joined_at",
        (room_id,),
    ).fetchall()
    teams = db.execute(
        """SELECT t.*, p.game_id AS captain_game_id, p.rank AS captain_rank FROM room_teams t
        LEFT JOIN players p ON p.id = t.captain_id WHERE t.room_id = ? ORDER BY t.team_code""",
        (room_id,),
    ).fetchall()
    team_rosters = {"A": [], "B": []}
    for entry in db.execute("""SELECT tm.team_code, tm.member_role, p.game_id, p.rank FROM team_members tm
        JOIN players p ON p.id = tm.player_id WHERE tm.room_id = ? ORDER BY tm.member_role DESC, p.game_id""", (room_id,)).fetchall():
        team_rosters[entry["team_code"]].append(entry)
    available_players = [player for player in members if player["id"] not in {row["player_id"] for row in db.execute("SELECT player_id FROM team_members WHERE room_id = ?", (room_id,)).fetchall()}]
    rolls = {row["team_code"]: row["roll_value"] for row in db.execute("SELECT * FROM draft_rolls WHERE room_id = ?", (room_id,)).fetchall()}
    current_group = current_auction_group(db, room_id) if room["player_selection"] == "拍卖" else None
    group_players = []
    group_bids = {}
    own_team = db.execute("SELECT team_code FROM room_teams WHERE room_id = ? AND captain_id = ?", (room_id, session["player_id"])).fetchone()
    own_team_code = own_team["team_code"] if own_team else None
    own_bid = None
    if current_group:
        group_players = db.execute("SELECT p.id, p.game_id, p.rank, p.primary_position, p.secondary_positions FROM auction_group_players gp JOIN players p ON p.id = gp.player_id WHERE gp.group_id = ?", (current_group["id"],)).fetchall()
        group_bids = {row["team_code"]: row["bid_cents"] for row in db.execute("SELECT team_code, bid_cents FROM auction_bids WHERE group_id = ?", (current_group["id"],)).fetchall()}
        own_bid = group_bids.get(own_team_code)
    auction_groups = db.execute("SELECT * FROM auction_groups WHERE room_id = ? ORDER BY sequence_number", (room_id,)).fetchall()
    auction_history = db.execute("SELECT h.*, g.sequence_number FROM auction_bid_history h JOIN auction_groups g ON g.id = h.group_id WHERE g.room_id = ? ORDER BY h.id", (room_id,)).fetchall()
    auction_queue = []
    for group in auction_groups:
        group_players_detail = db.execute("SELECT p.game_id, p.rank, p.primary_position, p.secondary_positions FROM auction_group_players gp JOIN players p ON p.id = gp.player_id WHERE gp.group_id = ?", (group["id"],)).fetchall()
        group_history = [history for history in auction_history if history["group_id"] == group["id"]]
        display_group = dict(group)
        display_group["sequence_number"] = group["original_sequence"] or group["sequence_number"]
        auction_queue.append({"group": display_group, "players": group_players_detail, "history": group_history})
    if current_group:
        current_group = dict(current_group)
        current_group["sequence_number"] = current_group["original_sequence"] or current_group["sequence_number"]
    rounds = db.execute("SELECT * FROM game_rounds WHERE room_id = ? ORDER BY round_number", (room_id,)).fetchall()
    round_stats: dict[int, dict[int, sqlite3.Row]] = {}
    for stat in db.execute("SELECT * FROM player_round_stats WHERE round_id IN (SELECT id FROM game_rounds WHERE room_id = ?)", (room_id,)).fetchall():
        round_stats.setdefault(stat["round_id"], {})[stat["player_id"]] = stat
    scores = {row["winning_team"]: row["count"] for row in db.execute("SELECT winning_team, COUNT(*) AS count FROM game_rounds WHERE room_id = ? AND winning_team IS NOT NULL GROUP BY winning_team", (room_id,)).fetchall()}
    votes = db.execute(
        """SELECT v.candidate_id, COUNT(*) AS count FROM captain_votes v
        WHERE v.room_id = ? GROUP BY v.candidate_id""", (room_id,)
    ).fetchall()
    vote_counts = {row["candidate_id"]: row["count"] for row in votes}
    chosen_candidate_ids = {row["candidate_id"] for row in db.execute("SELECT candidate_id FROM captain_votes WHERE room_id = ? AND voter_id = ?", (room_id, session["player_id"])).fetchall()}
    voted_members = db.execute("SELECT COUNT(DISTINCT voter_id) AS count FROM captain_votes WHERE room_id = ?", (room_id,)).fetchone()["count"]
    next_round = next((round_item for round_item in rounds if round_item["winning_team"] is None), None)
    return render_template(
        "room_detail.html", room=room, members=members, teams=teams, rounds=rounds, scores=scores,
        wins_needed=SERIES_RULES[room["game_format"]][1], is_creator=room["creator_id"] == session["player_id"],
        can_record=can_record_result(db, room, session["player_id"]), vote_counts=vote_counts, team_rosters=team_rosters,
        chosen_candidate_ids=chosen_candidate_ids, voted_members=voted_members, next_round=next_round,
        team_capacity=team_capacity(room), available_players=available_players, rolls=rolls, own_team_code=own_team_code,
        current_group=current_group, group_players=group_players, group_bids=group_bids, own_bid=own_bid,
        auction_groups=auction_groups, auction_history=auction_history, auction_queue=auction_queue,
        round_stats=round_stats,
    )


@app.get("/rooms/<int:room_id>/results")
@login_required
def room_results(room_id: int):
    db = get_db()
    room = get_room(db, room_id)
    if room is None or not is_room_member(db, room_id, session["player_id"]):
        flash("请先加入该比赛房间。", "error")
        return redirect(url_for("index"))
    rosters = {"A": [], "B": []}
    for player in db.execute("""SELECT tm.team_code, p.id, p.game_id, p.rank, p.primary_position FROM team_members tm
        JOIN players p ON p.id = tm.player_id WHERE tm.room_id = ? ORDER BY tm.team_code, p.game_id""", (room_id,)).fetchall():
        rosters[player["team_code"]].append(player)
    rounds = db.execute("SELECT * FROM game_rounds WHERE room_id = ? ORDER BY round_number", (room_id,)).fetchall()
    stats = {}
    for stat in db.execute("SELECT * FROM player_round_stats WHERE round_id IN (SELECT id FROM game_rounds WHERE room_id = ?)", (room_id,)).fetchall():
        stats.setdefault(stat["round_id"], {})[stat["player_id"]] = stat
    scores = {row["winning_team"]: row["count"] for row in db.execute("SELECT winning_team, COUNT(*) AS count FROM game_rounds WHERE room_id = ? AND winning_team IS NOT NULL GROUP BY winning_team", (room_id,)).fetchall()}
    next_round = next((item for item in rounds if item["winning_team"] is None), None)
    edit_mode = request.args.get("edit") == "1" and room["creator_id"] == session["player_id"]
    recorded_rounds = [item for item in rounds if item["winning_team"] is not None]
    selected_edit_round_id = request.args.get("edit_round", type=int)
    if edit_mode and (selected_edit_round_id is None or selected_edit_round_id not in {item["id"] for item in recorded_rounds}):
        selected_edit_round_id = recorded_rounds[0]["id"] if recorded_rounds else None
    return render_template("room_results.html", room=room, rosters=rosters, rounds=rounds, stats=stats, scores=scores,
                           next_round=next_round, is_creator=room["creator_id"] == session["player_id"],
                           wins_needed=SERIES_RULES[room["game_format"]][1], edit_mode=edit_mode,
                           recorded_rounds=recorded_rounds, selected_edit_round_id=selected_edit_round_id)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
