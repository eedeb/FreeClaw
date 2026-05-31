from flask import Flask, render_template_string, abort, send_from_directory, render_template, request, jsonify
import src.agent as agent

from dotenv import load_dotenv
import os
load_dotenv()
groq_key=os.getenv("API_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

location=BASE_DIR+"/../models/data.pth"

agent.reset(groq_key, location)

app = Flask(__name__)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    user_input = data.get('message', '').strip()
    if not user_input:
        return jsonify({'error': 'Empty message'}), 400
    try:
        if user_input.lower() == '/reset':
            agent.reset(groq_key, location)
            return jsonify({'response': 'Agent reset successfully'})
        else:
            response = agent.agent(user_input=user_input)
            return jsonify({'response': response})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/agent/<path:text>')
def serve_template(text):
    template_folder=app.template_folder or 'templates'
    file_path = os.path.join(template_folder, f"{text}.html")
    return render_template(f"{text}.html")

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6767, debug=True)