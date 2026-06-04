from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session
import src.agent as agent

from dotenv import load_dotenv
import os
load_dotenv()
groq_key = os.getenv("API_KEY")
password  = os.getenv("FC_PASSWORD")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
location = BASE_DIR + "/../models/data.pth"

agent.reset(groq_key, location)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')
STATIC_DIR    = os.path.join(os.path.dirname(__file__), 'static')


def logged_in():
    return session.get("authenticated") is True


# ── AUTH ROUTES ──────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = False
    if request.method == 'POST':
        if request.form.get('password') == password:
            session.permanent = True
            session['authenticated'] = True
            return redirect(url_for('index'))
        else:
            error = True
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── MAIN ROUTES ──────────────────────────────────────────────

@app.route('/')
def index():
    if not logged_in():
        return redirect(url_for('login'))
    return render_template('index.html')


@app.route('/chat', methods=['POST'])
def chat():
    if not logged_in():
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    user_input = data.get('message', '').strip()
    if not user_input:
        return jsonify({'error': 'Empty message'}), 400

    try:
        if user_input.lower() == '/reset':
            agent.reset(groq_key, location)
            return jsonify({'response': 'Agent reset successfully'})
        elif user_input.lower() == '/startapi':
            os.system("sudo systemctl start FreeClawAPI.service")
            return jsonify({'response': 'API started successfully on port 8080'})
        elif user_input.lower() == '/stopapi':
            os.system("sudo systemctl stop FreeClawAPI.service")
            return jsonify({'response': 'API stopped successfully'})
        else:
            response = agent.agent(user_input=user_input)
            return jsonify({'response': response})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/agent/<path:text>')
def serve_template(text):
    if not logged_in():
        return redirect(url_for('login'))
    return render_template(f"{text}.html")


@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6767, debug=True)