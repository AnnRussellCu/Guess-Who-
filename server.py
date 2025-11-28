from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random, string

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

players = {}
rooms = {}  # { room_code: [player_names] }
player_order = []

# Start page
@app.route('/')
def index():
    return render_template('index.html')


# Room page
@app.route('/room')
def room():
    username = request.args.get('username')
    if not username:
        return "No username provided", 400
    return render_template('room.html', username=username)


# Socket event for guesses
@socketio.on('guess')
def handle_guess(data):
    print(f"Player guessed: {data['guess']}")
    emit('response', {'message': f"Player guessed: {data['guess']}"}, broadcast=True)


# Create room
@socketio.on('create_room')
def handle_create_room(data):
    username = data['username']
    room_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    rooms[room_code] = [username]
    join_room(room_code)
    emit('room_created', room_code)  # send code back to creator
    emit('update_players', rooms[room_code], room=room_code)


# Join room
@socketio.on('join_room_event')
def handle_join_room(data):
    username = data['username']
    room_code = data['room_code']
    if room_code in rooms:
        if username not in rooms[room_code]:
            rooms[room_code].append(username)
        join_room(room_code)
        emit('update_players', rooms[room_code], room=room_code)
        emit('join_result', {'success': True})
    else:
        emit('join_result', {'success': False, 'message': 'Room not found'})


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)




