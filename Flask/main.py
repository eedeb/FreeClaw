from flask import Flask, render_template_string, abort, send_from_directory
import os

app = Flask(__name__)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), 'templates')
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')


@app.route('/<filename>')
def serve_template(filename):
    if not filename.endswith('.html'):
        abort(404)

    template_path = os.path.join(TEMPLATES_DIR, filename)

    if not os.path.isfile(template_path):
        abort(404)

    with open(template_path, 'r') as f:
        content = f.read()

    return render_template_string(content)


@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
