import json
import os
import sqlite3
import time
import uuid
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "ivr_data.db")
MENU_FILE = os.path.join(os.path.dirname(__file__), "menu_config.json")

# In-memory simulator session store: session_id -> { current_menu, history, started_at }
simulator_sessions = {}

def load_menu():
    if os.path.exists(MENU_FILE):
        with open(MENU_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_id TEXT,
            source TEXT,
            duration_seconds INTEGER,
            path_taken TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS voicemails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_id TEXT,
            menu_location TEXT,
            message_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Helper to generate standard Twilio TwiML XML
def build_twiml(say_text, gather_action=None, hangup=False, record_action=None):
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
    if gather_action:
        xml += f'  <Gather numDigits="1" action="{gather_action}" method="POST">\n'
        xml += f'    <Say voice="Polly.Joanna">{say_text}</Say>\n'
        xml += '  </Gather>\n'
        xml += '  <Say voice="Polly.Joanna">We did not receive any input. Goodbye.</Say>\n'
        xml += '  <Hangup/>\n'
    elif record_action:
        xml += f'  <Say voice="Polly.Joanna">{say_text}</Say>\n'
        xml += f'  <Record action="{record_action}" finishOnKey="#" maxLength="120" playBeep="true"/>\n'
    else:
        xml += f'  <Say voice="Polly.Joanna">{say_text}</Say>\n'
        if hangup:
            xml += '  <Hangup/>\n'
    xml += '</Response>'
    return xml


# ==========================================================
# 1. WEB SIMULATOR API (USED BY BROWSER DIALER)
# ==========================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config")
def get_config():
    return jsonify(load_menu())


@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    menu = load_menu()
    caller_number = request.json.get("caller_number", "Browser Caller")
    session_id = str(uuid.uuid4())
    
    prompt_text = f"{menu.get('welcome_message', '')} {menu.get('main', {}).get('prompt', '')}"
    
    simulator_sessions[session_id] = {
        "caller_id": caller_number,
        "current_menu": "main",
        "history": ["Call Started", "Main Menu"],
        "started_at": time.time()
    }
    
    return jsonify({
        "session_id": session_id,
        "speech": prompt_text,
        "expect_digits": True,
        "hangup": False,
        "is_voicemail": False
    })


@app.route("/api/sim/press", methods=["POST"])
def sim_press():
    data = request.json or {}
    session_id = data.get("session_id")
    digit = str(data.get("digit", "")).strip()
    
    if not session_id or session_id not in simulator_sessions:
        return jsonify({"error": "No active call session. Please start a call first."}), 400
        
    session = simulator_sessions[session_id]
    menu = load_menu()
    curr = session["current_menu"]
    
    # Resolve active menu block
    if curr == "main":
        menu_block = menu.get("main", {})
    else:
        menu_block = menu.get("submenus", {}).get(curr, {})
        
    options = menu_block.get("options", {})
    session["history"].append(f"Pressed [{digit}]")
    
    if digit not in options:
        return jsonify({
            "speech": f"Sorry, {digit} is not a valid choice. {menu_block.get('prompt', '')}",
            "expect_digits": True,
            "hangup": False,
            "is_voicemail": False
        })
        
    action_def = options[digit]
    action_type = action_def.get("action")
    
    if action_type == "repeat":
        return jsonify({
            "speech": menu_block.get("prompt", ""),
            "expect_digits": True,
            "hangup": False,
            "is_voicemail": False
        })
        
    elif action_type == "main_menu":
        session["current_menu"] = "main"
        session["history"].append("Main Menu")
        return jsonify({
            "speech": menu.get("main", {}).get("prompt", ""),
            "expect_digits": True,
            "hangup": False,
            "is_voicemail": False
        })
        
    elif action_type == "submenu":
        sub_id = action_def.get("submenu_id")
        session["current_menu"] = sub_id
        sub_block = menu.get("submenus", {}).get(sub_id, {})
        session["history"].append(f"Submenu: {sub_id}")
        return jsonify({
            "speech": sub_block.get("prompt", ""),
            "expect_digits": True,
            "hangup": False,
            "is_voicemail": False
        })
        
    elif action_type == "message":
        hangup = action_def.get("hangup", False)
        session["history"].append("Played Message")
        return jsonify({
            "speech": action_def.get("say", ""),
            "expect_digits": not hangup,
            "hangup": hangup,
            "is_voicemail": False
        })
        
    elif action_type == "voicemail":
        session["history"].append("Left Voicemail Prompt")
        return jsonify({
            "speech": action_def.get("say", "Please leave a message."),
            "expect_digits": False,
            "hangup": False,
            "is_voicemail": True,
            "menu_location": curr
        })
        
    return jsonify({"speech": "Goodbye.", "hangup": True})


@app.route("/api/sim/voicemail", methods=["POST"])
def sim_save_voicemail():
    data = request.json or {}
    session_id = data.get("session_id")
    caller_id = "Browser User"
    menu_loc = data.get("menu_location", "general")
    message_text = data.get("message_text", "(Simulated voice recording captured)")
    
    if session_id and session_id in simulator_sessions:
        caller_id = simulator_sessions[session_id].get("caller_id", caller_id)
        simulator_sessions[session_id]["history"].append(f"Voicemail Saved: {message_text[:25]}...")
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO voicemails (caller_id, menu_location, message_text)
        VALUES (?, ?, ?)
    """, (caller_id, menu_loc, message_text))
    conn.commit()
    conn.close()
    
    return jsonify({
        "status": "success",
        "speech": "Thank you. Your message has been saved. Goodbye!",
        "hangup": True
    })


@app.route("/api/sim/hangup", methods=["POST"])
def sim_hangup():
    session_id = request.json.get("session_id")
    if session_id in simulator_sessions:
        sess = simulator_sessions.pop(session_id)
        duration = int(time.time() - sess["started_at"])
        path_str = " -> ".join(sess["history"])
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO call_logs (caller_id, source, duration_seconds, path_taken)
            VALUES (?, ?, ?, ?)
        """, (sess["caller_id"], "Browser Simulator", duration, path_str))
        conn.commit()
        conn.close()
    return jsonify({"status": "call_ended"})


@app.route("/api/logs")
def get_logs():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    logs = c.execute("SELECT * FROM call_logs ORDER BY id DESC LIMIT 50").fetchall()
    voicemails = c.execute("SELECT * FROM voicemails ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    
    return jsonify({
        "call_logs": [dict(row) for row in logs],
        "voicemails": [dict(row) for row in voicemails]
    })


# ==========================================================
# 2. TWILIO COMPATIBLE WEBHOOK ENDPOINTS
# ==========================================================

@app.route("/twilio/voice", methods=["GET", "POST"])
def twilio_incoming():
    menu = load_menu()
    welcome = menu.get("welcome_message", "Welcome.")
    prompt = menu.get("main", {}).get("prompt", "")
    twiml = build_twiml(f"{welcome} {prompt}", gather_action="/twilio/handle-key?menu=main")
    return Response(twiml, mimetype="text/xml")


@app.route("/twilio/handle-key", methods=["POST"])
def twilio_handle_key():
    menu = load_menu()
    curr_menu = request.args.get("menu", "main")
    digit = request.form.get("Digits", "").strip()
    caller_number = request.form.get("From", "Unknown")

    if curr_menu == "main":
        menu_block = menu.get("main", {})
    else:
        menu_block = menu.get("submenus", {}).get(curr_menu, {})

    options = menu_block.get("options", {})

    if digit not in options:
        twiml = build_twiml(
            f"Option {digit} is not recognized. {menu_block.get('prompt', '')}",
            gather_action=f"/twilio/handle-key?menu={curr_menu}"
        )
        return Response(twiml, mimetype="text/xml")

    act = options[digit]
    action_type = act.get("action")

    if action_type == "repeat":
        twiml = build_twiml(menu_block.get("prompt", ""), gather_action=f"/twilio/handle-key?menu={curr_menu}")
        return Response(twiml, mimetype="text/xml")

    elif action_type == "main_menu":
        twiml = build_twiml(menu.get("main", {}).get("prompt", ""), gather_action="/twilio/handle-key?menu=main")
        return Response(twiml, mimetype="text/xml")

    elif action_type == "submenu":
        sub_id = act.get("submenu_id")
        sub_block = menu.get("submenus", {}).get(sub_id, {})
        twiml = build_twiml(sub_block.get("prompt", ""), gather_action=f"/twilio/handle-key?menu={sub_id}")
        return Response(twiml, mimetype="text/xml")

    elif action_type == "message":
        twiml = build_twiml(act.get("say", ""), hangup=act.get("hangup", True))
        return Response(twiml, mimetype="text/xml")

    elif action_type == "voicemail":
        twiml = build_twiml(act.get("say", "Please record your message."), record_action=f"/twilio/save-voicemail?menu={curr_menu}")
        return Response(twiml, mimetype="text/xml")

    twiml = build_twiml("Thank you for calling. Goodbye.", hangup=True)
    return Response(twiml, mimetype="text/xml")


@app.route("/twilio/save-voicemail", methods=["POST"])
def twilio_save_voicemail():
    caller = request.form.get("From", "Unknown Twilio Caller")
    recording_url = request.form.get("RecordingUrl", "Recording received")
    menu_loc = request.args.get("menu", "general")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO voicemails (caller_id, menu_location, message_text)
        VALUES (?, ?, ?)
    """, (caller, menu_loc, f"Audio Recording URL: {recording_url}"))
    conn.commit()
    conn.close()

    twiml = build_twiml("Your voicemail has been saved. Thank you, goodbye!", hangup=True)
    return Response(twiml, mimetype="text/xml")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"* Simple IVR running at http://127.0.0.1:{port}")
    print("* Open your browser to test the interactive phone simulator!")
    app.run(host="0.0.0.0", port=port, debug=True)
