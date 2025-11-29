# server.py
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
    # Generate unique 6-char room code
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

    if username in rooms[room_code]:
        # Already in room (refresh case)
        join_room(room_code)
    else:
        rooms[room_code].append(username)
        join_room(room_code)

    host_name = rooms[room_code][0]
    emit('update_players', rooms[room_code], room=room_code)
    emit('join_result', {'success': True, 'host': host_name}, to=request.sid)

@socketio.on('start_game')
def handle_start_game(data):
    room_code = data.get('room_code')
    username = data.get('username')
    
    if room_code not in rooms or len(rooms[room_code]) != 2:
        return

    # Only allow the host (first player) to start
    if rooms[room_code][0] != username:
        return

    host_name = rooms[room_code][0]
    # Send room_code AND both players to the game
    emit('game_started', {
        'room_code': room_code,
        'host': host_name,
        'players': rooms[room_code]
    }, room=room_code)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)