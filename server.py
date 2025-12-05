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

# { room_code: username } to track current turn
current_turns = {}


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

@app.route('/choose.html')
def choose_page():
    room_code = request.args.get('room')
    username = request.args.get('username')
    if not room_code or not username:
        return "Missing parameters", 400
    return render_template('choose.html', room_code=room_code, username=username)

@app.route('/game.html')
def game_page():
    room_code = request.args.get('room')
    username = request.args.get('username')
    choice = request.args.get('choice')
    if not room_code or not username:
        return "Missing parameters", 400
    return render_template('game.html', room_code=room_code, username=username, choice=choice)

@app.route('/result.html')
def result_page():
    room_code = request.args.get('room')
    username = request.args.get('username')
    if not room_code or not username:
        return "Missing parameters", 400
    return render_template('result.html', room_code=room_code, username=username)


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

    # Redirect both players to choose.html
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
    
    # Emit ready count update
    socketio.emit('update_ready_count', {
        'ready_players': len(ready_players[room_code])
    }, room=room_code)
    
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

    # Set initial turn (first player)
    current_turns[room_code] = players[0]
    socketio.emit('turn_update', {
        'current_turn': current_turns[room_code]
    }, room=room_code)

    # Redirect both players to game.html with their choice
    for p in players:
        socketio.emit('redirect_to_gameplay', {
            'room_code': room_code,
            'username': p,
            'choice': player_choices[room_code][p]
        }, room=room_code)

    # Clean up timer reference
    with timer_lock:
        if room_code in active_timers:
            del active_timers[room_code]


# ---------------------
# GAMEPLAY - CHAT & TURNS
# ---------------------

@socketio.on('chat_message')
def handle_chat_message(data):
    room_code = data['room_code']
    username = data['username']
    message = data['message']
    
    emit('chat_message', {
        'username': username,
        'message': message
    }, room=room_code)


@socketio.on('make_guess')
def handle_make_guess(data):
    room_code = data['room_code']
    username = data['username']
    guessed_id = int(data['guessed_id'])
    
    if room_code not in player_choices or room_code not in rooms:
        return
    
    # Find opponent's choice
    opponent = None
    for player in rooms[room_code]:
        if player != username:
            opponent = player
            break
    
    if not opponent:
        return
    
    opponent_choice = player_choices[room_code][opponent]
    
    # Check if guess is correct
    if guessed_id == opponent_choice:
        # Winner!
        emit('guess_result', {
            'success': True,
            'correct_meme_name': get_meme_name(opponent_choice)
        }, to=request.sid)
        
        # Send game over to both
        socketio.emit('game_over', {
            'winner': username,
            'correct_meme_name': get_meme_name(opponent_choice),
            'result': 'win' if username == username else 'lose'
        }, room=room_code)
        
    else:
        # Wrong guess, switch turn
        emit('guess_result', {
            'success': False
        }, to=request.sid)
        
        current_turns[room_code] = opponent
        socketio.emit('turn_update', {
            'current_turn': current_turns[room_code]
        }, room=room_code)


# ---------------------
# LEAVE GAME
# ---------------------

@socketio.on('leave_game')
def handle_leave_game(data):
    room_code = data['room_code']
    username = data['username']
    
    if room_code in rooms and username in rooms[room_code]:
        rooms[room_code].remove(username)
        
        if len(rooms[room_code]) == 0:
            # Clean up room
            if room_code in rooms:
                del rooms[room_code]
            if room_code in player_choices:
                del player_choices[room_code]
            if room_code in ready_players:
                del ready_players[room_code]
            if room_code in current_turns:
                del current_turns[room_code]
        else:
            emit('update_players', rooms[room_code], room=room_code)


# ---------------------
# HELPER
# ---------------------

def get_meme_name(meme_id):
    memes = {
        1: "Doubter", 2: "Conspiracy Keanu", 3: "Mini Keanu", 4: "Eye Roll",
        5: "Guard", 6: "Pointing Glasses", 7: "Looking Guy", 8: "Borat Thumbs",
        9: "Confused Woman", 10: "Smirk", 11: "Roll Safe", 12: "Gandalf",
        13: "Sad Affleck", 14: "Tada Man", 15: "Confused Gandalf"
    }
    return memes.get(meme_id, "Unknown")


# ---------------------

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, debug=True)