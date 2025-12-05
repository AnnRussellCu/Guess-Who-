from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import random
import string
from threading import Timer, Lock

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

# { room_code: [player1, player2] }
rooms = {}

# { room_code: { username: chosen_meme_id } }
player_choices = {}

# { room_code: Timer } to track active timers
active_timers = {}
timer_lock = Lock()

# { room_code: set() } to track ready players
ready_players = {}


# ---------------------
# ROUTES
# ---------------------

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


# ---------------------
# ROOM CREATION / JOIN
# ---------------------

@socketio.on('create_room')
def handle_create_room(data):
    username = data['username']

    # Generate unique 6-char code
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


# ---------------------
# GAME START → CHOOSE PHASE
# ---------------------

@socketio.on('start_game')
def handle_start_game(data):
    room_code = data.get('room_code')
    username = data.get('username')

    # Ensure room is valid
    if room_code not in rooms or len(rooms[room_code]) != 2:
        return

    # Only host can start
    if rooms[room_code][0] != username:
        return

    # Redirect both players to play.html with their OWN username
    for player in rooms[room_code]:
        emit('redirect_to_game', {
            'room_code': room_code,
            'username': player
        }, room=room_code)


# ---------------------
# PLAYER READY
# ---------------------

@socketio.on('player_ready')
def handle_player_ready(data):
    room_code = data.get('room_code')
    username = data.get('username')
    
    if room_code not in rooms:
        return
    
    if room_code not in ready_players:
        ready_players[room_code] = set()
    ready_players[room_code].add(username)
    
    # Start choose phase only when BOTH players are ready
    if len(ready_players[room_code]) == len(rooms[room_code]) and room_code not in player_choices:
        player_choices[room_code] = {}
        socketio.emit('choose_phase_start', room=room_code)
        
        # Start 10s auto-timeout
        with timer_lock:
            if room_code in active_timers:
                active_timers[room_code].cancel()
            timer = Timer(10.0, finish_choose_phase, args=[room_code])
            active_timers[room_code] = timer
            timer.start()


# ---------------------
# PLAYER PICK
# ---------------------

@socketio.on('player_chose')
def player_chose(data):
    room = data['room_code']
    user = data['username']
    choice = int(data['choice'])

    if room not in player_choices:
        player_choices[room] = {}

    player_choices[room][user] = choice

    # If both players have chosen before 10s → finish early
    if room in rooms and len(player_choices[room]) == len(rooms[room]):
        with timer_lock:
            if room in active_timers:
                active_timers[room].cancel()
                del active_timers[room]
        finish_choose_phase(room)


# ---------------------
# FINISH CHOOSE PHASE
# ---------------------

def finish_choose_phase(room_code):
    if room_code not in rooms or room_code not in player_choices:
        return

    players = rooms[room_code]

    # Assign random selection for players who did NOT choose
    for p in players:
        if p not in player_choices[room_code]:
            player_choices[room_code][p] = random.randint(1, 15)

    # Send final choices to both clients
    socketio.emit('choices_finalized', {
        'choices': player_choices[room_code]
    }, room=room_code)

    # Higher choice wins
    winner = max(player_choices[room_code], key=lambda u: player_choices[room_code][u])
    
    for p in players:
        result = 'win' if p == winner else 'lose'
        socketio.emit('game_over', {
            'username': p,
            'room_code': room_code,
            'result': result
        }, room=room_code)
        
    # Clean up timer reference
    with timer_lock:
        if room_code in active_timers:
            del active_timers[room_code]

# ---------------------
# CHAT SYSTEM
# ---------------------

@socketio.on("send_chat")
def handle_chat(data):
    room = data["room"]
    username = data["username"]
    message = data["message"]

    # Broadcast to room
    emit("receive_chat", {
        "username": username,
        "message": message
    }, room=room)

# ---------------------


if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, debug=True)
