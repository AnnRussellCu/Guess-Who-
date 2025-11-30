from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

# { room_code: [player1, player2] }
rooms = {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/room')
def room():
    username = request.args.get('username')
    if not username:
        return "No username provided", 400
    return render_template('room.html', username=username)

@app.route('/play.html')
def game_page():
    room_code = request.args.get('room')
    username = request.args.get('username')
    if not room_code or not username:
        return "Missing parameters", 400
    return render_template('play.html', room_code=room_code, username=username)

@socketio.on('create_room')
def handle_create_room(data):
    username = data['username']
    while True:
        room_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if room_code not in rooms:
            break
    rooms[room_code] = [username]
    join_room(room_code)
    emit('room_created', room_code)
    emit('update_players', rooms[room_code], room=room_code)

@socketio.on('join_room_event')
def handle_join_room(data):
    username = data['username']
    room_code = data['room_code'].upper()

    if room_code not in rooms:
        emit('join_result', {'success': False, 'message': 'Room not found'})
        return

    if len(rooms[room_code]) >= 2:
        emit('join_result', {'success': False, 'message': 'Room is full'})
        return

    if username not in rooms[room_code]:
        rooms[room_code].append(username)
    join_room(room_code)

    emit('update_players', rooms[room_code], room=room_code)
    emit('join_result', {'success': True, 'host': rooms[room_code][0]}, to=request.sid)

@socketio.on('join_game_room')
def handle_join_game_room(data):
    room_code = data['room_code']
    username = data['username']
    join_room(room_code)
    if room_code in rooms:
        emit('update_players', rooms[room_code], room=room_code)

@socketio.on('start_game')
def handle_start_game(data):
    room_code = data.get('room_code')
    username = data.get('username')

    if room_code not in rooms or len(rooms[room_code]) != 2:
        return

    # Only host can start
    if rooms[room_code][0] != username:
        return

    # Notify both players that game starts
    emit('game_started', {
        'room_code': room_code,
        'players': rooms[room_code]
    }, room=room_code)

    # Immediately show choose screen for first player (or both)
    emit('your_turn', room=room_code)   # triggers choose screen

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, debug=True)