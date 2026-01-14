from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random, uuid, json, os

app = Flask(__name__, static_folder='static', template_folder='templates')
socketio = SocketIO(app, cors_allowed_origins="*")

POINTS = {"easy": 10, "medium": 25, "hard": 50}
rooms = {}

# ------------------ LOAD QUESTIONS.JSON ------------------

def load_questions():
    # forventer: /static/questions.json
    path = os.path.join(app.static_folder, "questions.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # Konverter til dit interne format: q/a/correct
    # raw: { "NBA": { "easy": [ {question, answers, correct}, ...], ... }, ... }
    converted = {}
    for cat_name, levels in raw.items():
        converted[cat_name] = {}
        for diff in ["easy", "medium", "hard"]:
            if diff not in levels:
                converted[cat_name][diff] = []
                continue

            arr = []
            for item in levels[diff]:
                # accepter både gamle og nye keys for sikkerhed
                q_text = item.get("q") or item.get("question")
                a_list = item.get("a") or item.get("answers")
                correct = item.get("correct")

                if not isinstance(q_text, str):
                    continue
                if not isinstance(a_list, list) or len(a_list) != 4:
                    continue
                if not isinstance(correct, int) or correct < 0 or correct > 3:
                    continue

                arr.append({"q": q_text, "a": a_list, "correct": correct})

            converted[cat_name][diff] = arr

    return converted

QUESTIONS = load_questions()
CATEGORIES = list(QUESTIONS.keys())

# ------------------ ROUTES ------------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/lobby")
def lobby():
    return render_template("lobby.html")

# ------------------ GAME STATE ------------------

def init_game():
    return {
        "players": {},
        "order": [],
        "round": 0,
        "current_picker": None,
        "picker_counts": {},
        "category": None,
        "category_options": [],
        "questions": {},       # pr spiller: {sid: {"q","a","correct"}}
        "answers": {},         # pr spiller: {sid: valgt answer index eller None}
        "difficulties": {},    # pr spiller: {sid: "easy"/"medium"/"hard"}
        "phase": "lobby"
    }

# ------------------ SOCKET EVENTS ------------------

@socketio.on("create_room")
def create_room(data=None):
    room = str(uuid.uuid4())[:6].upper()
    rooms[room] = init_game()
    emit("room_created", {"room": room})

@socketio.on("join_room")
def join(data):
    room, name = data["room"], data["name"]
    sid = request.sid

    if room not in rooms:
        emit("joined", {"error": "Room not found"})
        return

    g = rooms[room]
    g["players"][sid] = {"name": name, "score": 0}
    g["picker_counts"][sid] = 0

    join_room(room)

    emit("joined", {"room": room, "sid": sid})
    emit("player_list", {"players": [p["name"] for p in g["players"].values()]}, room=room)

@socketio.on("start_game")
def start_game(data):
    room = data["room"]
    if room not in rooms:
        return

    g = rooms[room]
    g["order"] = list(g["players"].keys())
    random.shuffle(g["order"])

    next_round(room)

def next_round(room):
    g = rooms[room]

    # Find næste spiller som ikke har været picker 2 gange endnu
    for sid in g["order"]:
        if g["picker_counts"][sid] < 2:
            g["round"] += 1
            g["current_picker"] = sid
            g["picker_counts"][sid] += 1

            g["category"] = None

            # Hvis du kun har 3 kategorier i JSON, virker sample stadig.
            # Hvis du har mindre end 3, tager vi bare alle.
            if len(CATEGORIES) >= 3:
                g["category_options"] = random.sample(CATEGORIES, 3)
            else:
                g["category_options"] = CATEGORIES[:]

            g["answers"] = {}
            g["questions"] = {}
            g["difficulties"] = {}

            emit("round_start", {
                "roundId": g["round"],
                "picker": g["players"][sid]["name"],
                "pickerSid": sid,
                "categories": g["category_options"]
            }, room=room)
            return

    end_game(room)

@socketio.on("choose_category")
def choose(data):
    room, cat = data["room"], data["category"]
    sid = request.sid

    if room not in rooms:
        return

    g = rooms[room]

    if sid != g["current_picker"]:
        return
    if cat not in g["category_options"]:
        return
    if cat not in QUESTIONS:
        return

    g["category"] = cat

    emit("category_chosen", {
        "category": g["category"],
        "roundId": g["round"]
    }, room=room)

@socketio.on("choose_difficulty")
def pick_diff(data):
    room, difficulty = data["room"], data["difficulty"]
    sid = request.sid

    if room not in rooms:
        return

    g = rooms[room]

    if g["category"] is None:
        return
    if difficulty not in ["easy", "medium", "hard"]:
        return

    g["difficulties"][sid] = difficulty

    pool = QUESTIONS[g["category"]].get(difficulty, [])
    if not pool:
        emit("question", {
            "question": "No questions available for this category/difficulty.",
            "answers": ["OK", "OK", "OK", "OK"],
            "roundId": g["round"]
        }, to=sid)
        return

    q = random.choice(pool)

    g["questions"][sid] = q
    g["answers"][sid] = None

    emit("question", {
        "question": q["q"],
        "answers": q["a"],
        "roundId": g["round"]
    }, to=sid)

@socketio.on("submit_answer")
def receive_answer(data):
    room = data["room"]
    sid = request.sid
    answer = data["answer"]

    if room not in rooms:
        return

    g = rooms[room]

    if sid not in g["questions"]:
        return

    q = g["questions"][sid]
    correct_index = q["correct"]
    correct = (answer == correct_index)

    g["answers"][sid] = answer

    difficulty = g["difficulties"].get(sid)
    if correct and difficulty in POINTS:
        g["players"][sid]["score"] += POINTS[difficulty]

    emit("answer_feedback", {
        "correct": correct,
        "correctIndex": correct_index
    }, to=sid)

    # Runden slutter først når ALLE spillere har svaret (og fået deres personlige feedback)
    if all(psid in g["answers"] and g["answers"][psid] is not None for psid in g["players"]):
        emit("round_end", {}, room=room)
        socketio.sleep(1)
        next_round(room)

def end_game(room):
    g = rooms[room]
    results = [{"name": p["name"], "score": p["score"]} for p in g["players"].values()]
    sorted_res = sorted(results, key=lambda r: -r["score"])
    winner = sorted_res[0]["name"] if sorted_res else "Ingen"
    emit("game_over", {"results": results, "winner": winner}, room=room)

if __name__ == "__main__":
    socketio.run(app, debug=True)
