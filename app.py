"""
BetLog — registo diário de apostas para a equipa.
- Login seguro (palavras-passe encriptadas).
- Cada utilizador regista operações; cada operação tem 2-3 pernas (casa+resultado+valor+odd).
- A app calcula o lucro/prejuízo: perna vencedora rende valor*odd; resto = 0.
- Cada pessoa vê só as suas operações; o admin vê tudo.
- SQLite por defeito (testar no PC). Para Railway: define DATABASE_URL (Postgres) e usa-se essa.
"""
import os
import sqlite3
from datetime import datetime, date
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   render_template, jsonify, abort)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "muda-isto-em-producao-betlog-2026")

# --- Base de dados: SQLite local, ou Postgres se DATABASE_URL existir ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith("postgres")

if USE_PG:
    import psycopg2
    import psycopg2.extras
    # normaliza esquema antigo do Railway
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

DB_PATH = os.environ.get("BETLOG_DB", os.path.join(os.path.dirname(__file__), "betlog.db"))


def get_db():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql):
    """Adapta os placeholders: SQLite usa ?, Postgres usa %s."""
    if USE_PG:
        return sql.replace("?", "%s")
    return sql


def init_db():
    conn = get_db()
    cur = conn.cursor()
    if USE_PG:
        cur.execute("""CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ops(
            id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL,
            op_date DATE NOT NULL, event TEXT, bet_type TEXT, notes TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS legs(
            id SERIAL PRIMARY KEY, op_id INTEGER NOT NULL,
            book TEXT, outcome TEXT, stake REAL, odd REAL, won INTEGER DEFAULT 0)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS deposits(
            id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL,
            dep_date DATE NOT NULL, book TEXT NOT NULL, amount REAL NOT NULL,
            notes TEXT, created_at TIMESTAMP DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS goals(
            id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, book TEXT NOT NULL,
            daily_target REAL NOT NULL DEFAULT 0,
            UNIQUE(user_id, book))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS books(
            id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL)""")
    else:
        cur.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ops(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            op_date TEXT NOT NULL, event TEXT, bet_type TEXT, notes TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS legs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, op_id INTEGER NOT NULL,
            book TEXT, outcome TEXT, stake REAL, odd REAL, won INTEGER DEFAULT 0)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS deposits(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            dep_date TEXT NOT NULL, book TEXT NOT NULL, amount REAL NOT NULL,
            notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS goals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, book TEXT NOT NULL,
            daily_target REAL NOT NULL DEFAULT 0,
            UNIQUE(user_id, book))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS books(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL)""")
    conn.commit()

    # migração suave: acrescenta 'status' se a tabela ops for antiga
    try:
        if USE_PG:
            cur.execute("ALTER TABLE ops ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'")
        else:
            cur.execute("PRAGMA table_info(ops)")
            cols = [r[1] for r in cur.fetchall()]
            if "status" not in cols:
                cur.execute("ALTER TABLE ops ADD COLUMN status TEXT DEFAULT 'pending'")
        conn.commit()
    except Exception:
        pass

    # cria um admin inicial se não houver utilizadores
    cur.execute("SELECT COUNT(*) FROM users")
    n = cur.fetchone()[0]
    if n == 0:
        admin_user = os.environ.get("ADMIN_USER", "admin")
        admin_pw = os.environ.get("ADMIN_PASS", "admin123")
        cur.execute(q("INSERT INTO users(username,pw_hash,is_admin) VALUES(?,?,1)"),
                    (admin_user, generate_password_hash(admin_pw)))
        conn.commit()
        print(f"[BetLog] Admin criado: utilizador='{admin_user}' palavra-passe='{admin_pw}' "
              "(muda depois de entrar!)")
    # semeia marcas iniciais se a tabela estiver vazia
    cur.execute("SELECT COUNT(*) FROM books")
    if cur.fetchone()[0] == 0:
        for b in ["bcgame", "betpanda", "madcasino", "freshbet", "CasaBet", "Weltbet"]:
            try:
                cur.execute(q("INSERT INTO books(name) VALUES(?)"), (b,))
            except Exception:
                pass
        conn.commit()
    cur.close()
    conn.close()


# --- Helpers de autenticação ---
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("SELECT id,username,is_admin FROM users WHERE id=?"), (uid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    if USE_PG:
        return {"id": row[0], "username": row[1], "is_admin": row[2]}
    return {"id": row["id"], "username": row["username"], "is_admin": row["is_admin"]}


def login_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("uid"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrap


def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        u = current_user()
        if not u or not u["is_admin"]:
            return abort(403)
        return f(*a, **k)
    return wrap


def op_profit(legs, status="resolved"):
    """lucro = soma(retornos) - soma(stakes). retorno da perna vencedora = stake*odd.
    Se a operação está 'pending' (ainda sem resultado), o lucro é None (não conta)."""
    staked = sum(float(l["stake"] or 0) for l in legs)
    if status == "pending":
        return None, round(staked, 2), None
    ret = sum(float(l["stake"] or 0) * float(l["odd"] or 0)
              for l in legs if l["won"])
    return round(ret - staked, 2), round(staked, 2), round(ret, 2)


# --- Rotas de páginas ---
@app.route("/")
def home():
    if session.get("uid"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form
        username = (data.get("username") or "").strip()
        pw = data.get("password") or ""
        conn = get_db()
        cur = conn.cursor()
        cur.execute(q("SELECT id,pw_hash FROM users WHERE username=?"), (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        ok = False
        if row:
            pw_hash = row[1] if USE_PG else row["pw_hash"]
            uid = row[0] if USE_PG else row["id"]
            if check_password_hash(pw_hash, pw):
                session["uid"] = uid
                ok = True
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Utilizador ou palavra-passe errados."}), 401
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=current_user())


# --- API ---
@app.route("/api/me")
@login_required
def api_me():
    return jsonify(current_user())


@app.route("/api/ops", methods=["GET"])
@login_required
def api_ops_list():
    u = current_user()
    conn = get_db()
    cur = conn.cursor()
    cols = "o.id,o.user_id,o.op_date,o.event,o.bet_type,o.notes,o.status,us.username"
    target = request.args.get("user_id")
    if u["is_admin"] and target:
        cur.execute(q(f"""SELECT {cols} FROM ops o JOIN users us ON us.id=o.user_id
                          WHERE o.user_id=? ORDER BY o.op_date DESC, o.id DESC"""), (target,))
    elif u["is_admin"]:
        cur.execute(q(f"""SELECT {cols} FROM ops o JOIN users us ON us.id=o.user_id
                          ORDER BY o.op_date DESC, o.id DESC"""))
    else:
        cur.execute(q(f"""SELECT {cols} FROM ops o JOIN users us ON us.id=o.user_id
                          WHERE o.user_id=? ORDER BY o.op_date DESC, o.id DESC"""), (u["id"],))
    ops = cur.fetchall()
    book_filter = (request.args.get("book") or "").strip()
    result = []
    for o in ops:
        oid, ouid, odate, oevent, otype, onotes, ostatus, ouser = (
            o[0], o[1], o[2], o[3], o[4], o[5], o[6], o[7])
        ostatus = ostatus or "pending"
        cur.execute(q("SELECT id,book,outcome,stake,odd,won FROM legs WHERE op_id=? ORDER BY id"), (oid,))
        legrows = cur.fetchall()
        legs = [{"id": l[0], "book": l[1], "outcome": l[2],
                 "stake": l[3], "odd": l[4], "won": l[5]} for l in legrows]
        # filtro por casa: só operações com essa casa, e só a perna dessa casa
        if book_filter:
            legs = [lg for lg in legs if (lg["book"] or "") == book_filter]
            if not legs:
                continue  # esta operação não tem a casa filtrada
        profit, staked, ret = op_profit(legs, ostatus)
        result.append({"id": oid, "user_id": ouid, "op_date": str(odate),
                       "event": oevent, "bet_type": otype, "notes": onotes,
                       "status": ostatus, "username": ouser,
                       "legs": legs, "profit": profit, "staked": staked, "returned": ret})
    cur.close()
    conn.close()
    return jsonify({"ops": result})


@app.route("/api/ops", methods=["POST"])
@login_required
def api_ops_create():
    u = current_user()
    data = request.get_json(silent=True) or {}
    op_date = data.get("op_date") or date.today().isoformat()
    event = (data.get("event") or "").strip()
    bet_type = (data.get("bet_type") or "").strip()
    notes = (data.get("notes") or "").strip()
    status = data.get("status") or "pending"
    if status not in ("pending", "resolved"):
        status = "pending"
    legs = data.get("legs") or []
    if not legs:
        return jsonify({"error": "Mete pelo menos uma perna (casa + valor + odd)."}), 400
    conn = get_db()
    cur = conn.cursor()
    if USE_PG:
        cur.execute(q("""INSERT INTO ops(user_id,op_date,event,bet_type,notes,status)
                         VALUES(?,?,?,?,?,?) RETURNING id"""),
                    (u["id"], op_date, event, bet_type, notes, status))
        op_id = cur.fetchone()[0]
    else:
        cur.execute(q("""INSERT INTO ops(user_id,op_date,event,bet_type,notes,status)
                         VALUES(?,?,?,?,?,?)"""),
                    (u["id"], op_date, event, bet_type, notes, status))
        op_id = cur.lastrowid
    for l in legs:
        cur.execute(q("""INSERT INTO legs(op_id,book,outcome,stake,odd,won)
                         VALUES(?,?,?,?,?,?)"""),
                    (op_id, (l.get("book") or "").strip(), (l.get("outcome") or "").strip(),
                     float(l.get("stake") or 0), float(l.get("odd") or 0),
                     1 if l.get("won") else 0))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True, "id": op_id})


@app.route("/api/ops/<int:op_id>", methods=["PUT"])
@login_required
def api_ops_update(op_id):
    u = current_user()
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("SELECT user_id FROM ops WHERE id=?"), (op_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({"error": "não existe"}), 404
    owner = row[0]
    if owner != u["id"] and not u["is_admin"]:
        cur.close(); conn.close()
        return jsonify({"error": "sem permissão"}), 403

    op_date = data.get("op_date") or date.today().isoformat()
    event = (data.get("event") or "").strip()
    bet_type = (data.get("bet_type") or "").strip()
    notes = (data.get("notes") or "").strip()
    status = data.get("status") or "pending"
    if status not in ("pending", "resolved"):
        status = "pending"
    legs = data.get("legs") or []
    if not legs:
        cur.close(); conn.close()
        return jsonify({"error": "Mete pelo menos uma perna."}), 400

    cur.execute(q("""UPDATE ops SET op_date=?,event=?,bet_type=?,notes=?,status=? WHERE id=?"""),
                (op_date, event, bet_type, notes, status, op_id))
    # substitui as pernas (apaga e recria — simples e fiável)
    cur.execute(q("DELETE FROM legs WHERE op_id=?"), (op_id,))
    for l in legs:
        cur.execute(q("""INSERT INTO legs(op_id,book,outcome,stake,odd,won)
                         VALUES(?,?,?,?,?,?)"""),
                    (op_id, (l.get("book") or "").strip(), (l.get("outcome") or "").strip(),
                     float(l.get("stake") or 0), float(l.get("odd") or 0),
                     1 if l.get("won") else 0))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/ops/<int:op_id>", methods=["DELETE"])
@login_required
def api_ops_delete(op_id):
    u = current_user()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("SELECT user_id FROM ops WHERE id=?"), (op_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({"error": "não existe"}), 404
    owner = row[0] if USE_PG else row[0]
    if owner != u["id"] and not u["is_admin"]:
        cur.close(); conn.close()
        return jsonify({"error": "sem permissão"}), 403
    cur.execute(q("DELETE FROM legs WHERE op_id=?"), (op_id,))
    cur.execute(q("DELETE FROM ops WHERE id=?"), (op_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/summary")
@login_required
def api_summary():
    """Totais. Admin: por pessoa + global. Utilizador: só os seus.
    Aceita ?book= para filtrar só por uma casa (conta só essa perna)."""
    u = current_user()
    book_filter = (request.args.get("book") or "").strip()
    conn = get_db()
    cur = conn.cursor()
    if u["is_admin"]:
        cur.execute(q("""SELECT o.id,o.user_id,us.username,o.status FROM ops o
                         JOIN users us ON us.id=o.user_id"""))
    else:
        cur.execute(q("""SELECT o.id,o.user_id,us.username,o.status FROM ops o
                         JOIN users us ON us.id=o.user_id WHERE o.user_id=?"""), (u["id"],))
    ops = cur.fetchall()
    per_user = {}
    total = {"profit": 0.0, "staked": 0.0, "ops": 0, "pending": 0}
    for o in ops:
        oid = o[0]
        uname = o[2]
        ostatus = o[3] or "pending"
        cur.execute(q("SELECT book,stake,odd,won FROM legs WHERE op_id=?"), (oid,))
        legrows = cur.fetchall()
        legs = [{"book": r[0], "stake": r[1], "odd": r[2], "won": r[3]} for r in legrows]
        # filtro por casa: conta só a perna dessa casa
        if book_filter:
            legs = [lg for lg in legs if (lg["book"] or "") == book_filter]
            if not legs:
                continue
        profit, staked, _ = op_profit(legs, ostatus)
        pu = per_user.setdefault(uname, {"profit": 0.0, "staked": 0.0, "ops": 0, "pending": 0})
        pu["staked"] = round(pu["staked"] + staked, 2)
        pu["ops"] += 1
        total["staked"] = round(total["staked"] + staked, 2)
        total["ops"] += 1
        if ostatus == "pending":
            pu["pending"] += 1
            total["pending"] += 1
        else:
            pu["profit"] = round(pu["profit"] + (profit or 0), 2)
            total["profit"] = round(total["profit"] + (profit or 0), 2)
    cur.close()
    conn.close()
    return jsonify({"per_user": per_user, "total": total, "is_admin": bool(u["is_admin"])})


@app.route("/api/by_book")
@login_required
def api_by_book():
    """Resumo POR CASA para um utilizador: depositado, apostado e lucro/prej.
    Admin pode pedir ?user_id=N; utilizador normal vê só o seu."""
    u = current_user()
    target = request.args.get("user_id", type=int)
    if u["is_admin"] and target:
        uid = target
    else:
        uid = u["id"]

    conn = get_db()
    cur = conn.cursor()
    books = {}  # nome da casa -> {deposited, staked, profit, ops, pending}

    def slot(name):
        return books.setdefault(name or "(sem casa)",
            {"deposited": 0.0, "staked": 0.0, "profit": 0.0, "ops": 0, "pending": 0})

    # 1) depositado por casa
    cur.execute(q("SELECT book, amount FROM deposits WHERE user_id=?"), (uid,))
    for bname, amount in cur.fetchall():
        slot(bname)["deposited"] = round(slot(bname)["deposited"] + float(amount or 0), 2)

    # 2) apostado e lucro por casa (cada perna pertence a uma casa)
    cur.execute(q("""SELECT o.id, o.status FROM ops o WHERE o.user_id=?"""), (uid,))
    ops = cur.fetchall()
    for oid, ostatus in ops:
        ostatus = ostatus or "pending"
        cur.execute(q("SELECT book,stake,odd,won FROM legs WHERE op_id=?"), (oid,))
        for bname, stake, odd, won in cur.fetchall():
            s = slot(bname)
            stake = float(stake or 0)
            s["staked"] = round(s["staked"] + stake, 2)
            s["ops"] += 1
            if ostatus == "pending":
                s["pending"] += 1
            else:
                # lucro desta perna: retorno (se ganhou) menos o stake
                ret = stake * float(odd or 0) if won else 0.0
                s["profit"] = round(s["profit"] + (ret - stake), 2)

    cur.close()
    conn.close()
    # transforma em lista ordenada por casa
    out = [{"book": k, **v} for k, v in books.items()]
    out.sort(key=lambda x: x["book"].lower())
    return jsonify({"by_book": out, "user_id": uid})


# --- Gestão de utilizadores (admin) ---
@app.route("/api/users", methods=["GET"])
@admin_required
def api_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,username,is_admin FROM users ORDER BY username")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    users = [{"id": r[0], "username": r[1], "is_admin": r[2]} for r in rows]
    return jsonify({"users": users})


@app.route("/api/users", methods=["POST"])
@admin_required
def api_users_create():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    pw = data.get("password") or ""
    if not username or not pw:
        return jsonify({"error": "Falta utilizador ou palavra-passe."}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(q("INSERT INTO users(username,pw_hash,is_admin) VALUES(?,?,0)"),
                    (username, generate_password_hash(pw)))
        conn.commit()
    except Exception:
        cur.close(); conn.close()
        return jsonify({"error": "Esse utilizador já existe."}), 400
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/password", methods=["POST"])
@login_required
def api_password():
    """Qualquer utilizador muda a sua própria palavra-passe."""
    u = current_user()
    data = request.get_json(silent=True) or {}
    new_pw = data.get("password") or ""
    if len(new_pw) < 4:
        return jsonify({"error": "Palavra-passe demasiado curta."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("UPDATE users SET pw_hash=? WHERE id=?"),
                (generate_password_hash(new_pw), u["id"]))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


# ---------- DEPÓSITOS ----------
@app.route("/api/deposits", methods=["GET"])
@login_required
def api_deposits_list():
    u = current_user()
    target = request.args.get("user_id")
    day = request.args.get("date")  # opcional: filtra por dia
    conn = get_db()
    cur = conn.cursor()
    base = """SELECT d.id,d.user_id,d.dep_date,d.book,d.amount,d.notes,us.username
              FROM deposits d JOIN users us ON us.id=d.user_id"""
    cond = []
    args = []
    if u["is_admin"] and target:
        cond.append("d.user_id=?"); args.append(target)
    elif not u["is_admin"]:
        cond.append("d.user_id=?"); args.append(u["id"])
    if day:
        cond.append("d.dep_date=?"); args.append(day)
    if cond:
        base += " WHERE " + " AND ".join(cond)
    base += " ORDER BY d.dep_date DESC, d.id DESC"
    cur.execute(q(base), tuple(args))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    deps = [{"id": r[0], "user_id": r[1], "dep_date": str(r[2]), "book": r[3],
             "amount": r[4], "notes": r[5], "username": r[6]} for r in rows]
    return jsonify({"deposits": deps})


@app.route("/api/deposits", methods=["POST"])
@login_required
def api_deposits_create():
    u = current_user()
    data = request.get_json(silent=True) or {}
    dep_date = data.get("dep_date") or date.today().isoformat()
    book = (data.get("book") or "").strip()
    notes = (data.get("notes") or "").strip()
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if not book or amount <= 0:
        return jsonify({"error": "Indica a marca e um valor maior que zero."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("""INSERT INTO deposits(user_id,dep_date,book,amount,notes)
                     VALUES(?,?,?,?,?)"""), (u["id"], dep_date, book, amount, notes))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/deposits/<int:dep_id>", methods=["DELETE"])
@login_required
def api_deposits_delete(dep_id):
    u = current_user()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("SELECT user_id FROM deposits WHERE id=?"), (dep_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({"error": "não existe"}), 404
    if row[0] != u["id"] and not u["is_admin"]:
        cur.close(); conn.close()
        return jsonify({"error": "sem permissão"}), 403
    cur.execute(q("DELETE FROM deposits WHERE id=?"), (dep_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


# ---------- METAS (admin define; todos veem as suas) ----------
@app.route("/api/goals", methods=["GET"])
@login_required
def api_goals_list():
    u = current_user()
    target = request.args.get("user_id")
    conn = get_db()
    cur = conn.cursor()
    if u["is_admin"] and target:
        cur.execute(q("""SELECT g.id,g.user_id,g.book,g.daily_target,us.username
                         FROM goals g JOIN users us ON us.id=g.user_id WHERE g.user_id=?
                         ORDER BY us.username,g.book"""), (target,))
    elif u["is_admin"]:
        cur.execute(q("""SELECT g.id,g.user_id,g.book,g.daily_target,us.username
                         FROM goals g JOIN users us ON us.id=g.user_id
                         ORDER BY us.username,g.book"""))
    else:
        cur.execute(q("""SELECT g.id,g.user_id,g.book,g.daily_target,us.username
                         FROM goals g JOIN users us ON us.id=g.user_id WHERE g.user_id=?
                         ORDER BY g.book"""), (u["id"],))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    goals = [{"id": r[0], "user_id": r[1], "book": r[2], "daily_target": r[3],
              "username": r[4]} for r in rows]
    return jsonify({"goals": goals})


@app.route("/api/goals", methods=["POST"])
@admin_required
def api_goals_set():
    """Admin define (ou atualiza) a meta diária de uma pessoa numa marca."""
    data = request.get_json(silent=True) or {}
    try:
        user_id = int(data.get("user_id"))
        target = float(data.get("daily_target") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Dados inválidos."}), 400
    book = (data.get("book") or "").strip()
    if not book:
        return jsonify({"error": "Indica a marca."}), 400
    conn = get_db()
    cur = conn.cursor()
    # upsert manual (compatível SQLite + Postgres)
    cur.execute(q("SELECT id FROM goals WHERE user_id=? AND book=?"), (user_id, book))
    row = cur.fetchone()
    if row:
        cur.execute(q("UPDATE goals SET daily_target=? WHERE id=?"), (target, row[0]))
    else:
        cur.execute(q("INSERT INTO goals(user_id,book,daily_target) VALUES(?,?,?)"),
                    (user_id, book, target))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/goals/<int:goal_id>", methods=["DELETE"])
@admin_required
def api_goals_delete(goal_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("DELETE FROM goals WHERE id=?"), (goal_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


# ---------- MARCAS (admin gere a lista; todos veem para escolher) ----------
@app.route("/api/books", methods=["GET"])
@login_required
def api_books_list():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id,name FROM books ORDER BY name")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({"books": [{"id": r[0], "name": r[1]} for r in rows]})


@app.route("/api/books", methods=["POST"])
@admin_required
def api_books_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Indica o nome da marca."}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(q("INSERT INTO books(name) VALUES(?)"), (name,))
        conn.commit()
    except Exception:
        cur.close(); conn.close()
        return jsonify({"error": "Essa marca já existe."}), 400
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/books/<int:book_id>", methods=["DELETE"])
@admin_required
def api_books_delete(book_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("DELETE FROM books WHERE id=?"), (book_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/progress")
@login_required
def api_progress():
    """Progresso de hoje: por pessoa e marca, depositado vs meta."""
    u = current_user()
    day = request.args.get("date") or date.today().isoformat()
    conn = get_db()
    cur = conn.cursor()
    # metas
    if u["is_admin"]:
        cur.execute(q("""SELECT g.user_id,us.username,g.book,g.daily_target
                         FROM goals g JOIN users us ON us.id=g.user_id"""))
    else:
        cur.execute(q("""SELECT g.user_id,us.username,g.book,g.daily_target
                         FROM goals g JOIN users us ON us.id=g.user_id WHERE g.user_id=?"""),
                    (u["id"],))
    goals = cur.fetchall()
    # depósitos do dia
    if u["is_admin"]:
        cur.execute(q("""SELECT user_id,book,SUM(amount) FROM deposits
                         WHERE dep_date=? GROUP BY user_id,book"""), (day,))
    else:
        cur.execute(q("""SELECT user_id,book,SUM(amount) FROM deposits
                         WHERE dep_date=? AND user_id=? GROUP BY user_id,book"""),
                    (day, u["id"]))
    dep_map = {(r[0], r[1]): float(r[2] or 0) for r in cur.fetchall()}
    cur.close()
    conn.close()
    items = []
    for g in goals:
        uid, uname, book, target = g[0], g[1], g[2], float(g[3] or 0)
        done = dep_map.get((uid, book), 0.0)
        items.append({"user_id": uid, "username": uname, "book": book,
                      "target": round(target, 2), "done": round(done, 2),
                      "pct": round((done / target * 100) if target > 0 else 0, 0),
                      "remaining": round(max(target - done, 0), 2)})
    items.sort(key=lambda x: (x["username"], x["book"]))
    return jsonify({"date": day, "progress": items, "is_admin": bool(u["is_admin"])})


# Cria as tabelas ao arrancar — funciona tanto com 'py app.py' como com gunicorn (Railway).
try:
    init_db()
except Exception as e:
    print("[BetLog] aviso ao iniciar a base de dados:", e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    app.run(host="0.0.0.0", port=port, debug=False)
